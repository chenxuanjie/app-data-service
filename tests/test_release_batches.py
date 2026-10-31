"""Reservation races and cross-target history against the isolated PostgreSQL DB."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from test_public_releases import BASE, headers, publish
from test_public_releases import service as service_fixture

from app_data_service import release_models as m
from app_data_service.public_releases import transaction


@pytest.fixture(name="service")
def batch_service(db, tmp_path, monkeypatch):
    yield from service_fixture.__wrapped__(db, tmp_path, monkeypatch)


def reserve(client, key=None, **values):
    return client.post(
        BASE + "/batches",
        headers=headers(),
        json={
            "requestKey": key or uuid4().hex,
            "versionName": "9.0.2",
            "minimumBuild": 0,
            **values,
        },
    )


def test_retries_are_idempotent_and_parallel_requests_do_not_share_numbers(service):
    client, _ = service
    key = uuid4().hex
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: reserve(client, key), range(6)))
    assert all(r.status_code == 200 for r in responses)
    assert len({r.json()["batchId"] for r in responses}) == 1
    assert {r.json()["buildNumber"] for r in responses} == {9002}
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: reserve(client), range(6)))
    assert all(r.status_code == 200 for r in responses)
    assert {r.json()["buildNumber"] for r in responses} == set(range(9003, 9009))
    assert reserve(client, key, versionName="10.0.0").status_code == 409


def test_source_freeze_and_published_target_are_immutable(service):
    client, _ = service
    row = reserve(client).json()
    prefix = BASE + f"/batches/{row['batchId']}"
    target = {
        "component": "web",
        "platform": "linux",
        "architecture": "amd64",
        "channel": "nas",
        "status": "published",
        "kind": "image",
        "sha256": "c" * 64,
        "reference": "ghcr.io/example/web@sha256:" + "c" * 64,
        "sourceRevision": "b" * 40,
    }
    assert client.put(prefix + "/targets", headers=headers(), json=target).status_code == 409
    frozen = {"sourceRevision": "b" * 40, "dependencyRevisions": {"app_client": "d" * 40}}
    for _ in range(2):
        assert client.put(prefix + "/source", headers=headers(), json=frozen).status_code == 200
    assert (
        client.put(
            prefix + "/source", headers=headers(), json={"sourceRevision": "e" * 40}
        ).status_code
        == 409
    )
    for _ in range(2):
        assert client.put(prefix + "/targets", headers=headers(), json=target).status_code == 200
    retry = client.put(prefix + "/targets", headers=headers(), json=target | {"status": "built"})
    assert retry.status_code == 200 and retry.json()["status"] == "published"
    for change in (
        {"status": "failed", "sha256": None, "reference": None, "errorCode": "upload_failed"},
        {"sha256": "e" * 64, "reference": "ghcr.io/example/web@sha256:" + "e" * 64},
    ):
        assert (
            client.put(prefix + "/targets", headers=headers(), json=target | change).status_code
            == 409
        )
    records = client.get(prefix, headers=headers()).json()
    assert len(records["targets"]) == 1 and records["targets"][0]["sha256"] == "c" * 64


def test_installer_and_reused_image_share_batch_without_losing_original_source(service):
    client, _ = service
    publish(client)
    batch_id = client.release_batch_id
    target = {
        "component": "web-ai",
        "platform": "linux",
        "architecture": "amd64",
        "channel": "nas",
        "status": "published",
        "kind": "image",
        "sha256": "c" * 64,
        "reference": "ghcr.io/example/relay@sha256:" + "c" * 64,
        "sourceRevision": "b" * 40,
    }
    prefix = BASE + f"/batches/{batch_id}"
    assert client.put(prefix + "/targets", headers=headers(), json=target).status_code == 409
    assert (
        client.put(
            prefix + "/targets",
            headers=headers(),
            json=target | {"reused": True, "inputDigest": "d" * 64},
        ).status_code
        == 200
    )
    rows = client.get(prefix, headers=headers()).json()["targets"]
    assert {r["kind"] for r in rows} == {"installer", "image"}
    assert next(r for r in rows if r["kind"] == "image")["sourceRevision"] == "b" * 40
    assert reserve(client).json()["buildNumber"] == 9002


def test_failed_target_can_retry_without_changing_batch_or_other_targets(service):
    client, _ = service
    prefix = BASE + f"/batches/{client.release_batch_id}"
    failure = {
        "component": "client",
        "platform": "windows",
        "architecture": "x64",
        "channel": "stable",
        "kind": "installer",
        "status": "failed",
        "errorCode": "build_failed",
        "sourceRevision": "a" * 40,
    }
    assert client.put(prefix + "/targets", headers=headers(), json=failure).status_code == 200
    publish(client)
    result = client.get(prefix, headers=headers()).json()
    assert result["buildNumber"] == 9001
    assert {r["status"] for r in result["targets"]} == {"failed", "published"}


def test_batch_information_and_writes_require_app_machine_permission(db, service):
    client, _ = service
    prefix = BASE + f"/batches/{client.release_batch_id}"
    assert client.get(prefix).status_code == 403
    assert (
        client.post(
            BASE + "/batches",
            json={"requestKey": "x" * 32, "versionName": "9.0.2", "minimumBuild": 0},
        ).status_code
        == 403
    )
    with transaction(db.runtime, "zheri", publishing=True) as c:
        assert not c.execute(sa.select(m.batches)).all()
    with transaction(db.runtime, "board_game_agent") as c:
        assert not c.execute(sa.select(m.batches)).all()
    with pytest.raises(DBAPIError), transaction(db.runtime, "zheri", publishing=True) as c:
        c.execute(m.counters.insert().values(app_id="board_game_agent", last_build=99999))
