"""Public reads and scoped machine publication use an isolated real PostgreSQL DB."""

import hashlib
import json
import runpy
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from app_data_service import release_models as m
from app_data_service.api import create_app
from app_data_service.auth import TokenAuthority
from app_data_service.public_releases import transaction

BASE = "/v1/apps/board_game_agent/releases"
KEY = "test-release-publishing-machine-secret-only"
DATA = b"PK\x03\x04" + b"test-installer" * 50000
DIGEST = hashlib.sha256(DATA).hexdigest()


@pytest.fixture
def service(db, tmp_path, monkeypatch):
    monkeypatch.setenv(
        "APP_DATA_RELEASE_PUBLISH_KEY_HASHES",
        json.dumps(
            {
                "board_game_agent": hashlib.sha256(KEY.encode()).hexdigest(),
            }
        ),
    )
    with db.migrator.begin() as c:
        # This fixture owns the random test DB. FORCE RLS intentionally makes
        # ordinary DELETE ineffective; truncate only these two test tables.
        c.exec_driver_sql("TRUNCATE core.public_releases,core.release_blobs")
        c.exec_driver_sql("DELETE FROM core.publication_versions WHERE topic='release'")
    authority = TokenAuthority(
        rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        "https://release.test",
    )
    app = create_app(
        db.runtime,
        authority,
        "https://release.test/powersync/",
        application_settings={
            "board_game_agent": {"public_file_roots": {"releases": str(tmp_path)}},
        },
    )
    with TestClient(app) as client:
        yield client, tmp_path


def headers():
    return {"X-Release-Publish-Key": KEY}


def publication(build=9001):
    return {
        "schemaVersion": 1,
        "appId": "board_game_agent",
        "channel": "stable",
        "versionName": "9.0.1",
        "buildNumber": build,
        "minimumSupportedBuild": 1,
        "forceUpdate": False,
        "publishedAt": "2026-10-28T00:00:00Z",
        "releaseNotes": {"zh-CN": ["游客可升级"]},
        "artifacts": [
            {
                "platform": "android",
                "architecture": "arm64",
                "path": "test.apk",
                "size": len(DATA),
                "sha256": DIGEST,
                "mimeType": "application/vnd.android.package-archive",
            }
        ],
    }


def publish(client, body=None):
    uploaded = client.put(BASE + "/objects/" + DIGEST, content=DATA, headers=headers())
    assert uploaded.status_code == 200, uploaded.text
    result = client.post(BASE + "/publish", json=body or publication(), headers=headers())
    assert result.status_code == 200, result.text
    return result.json()


def test_anonymous_reads_only_published_verified_artifacts(service):
    client, _ = service
    query = BASE + "/manifest?platform=android&architecture=arm64"
    assert client.get(query).status_code == 404
    assert client.get(BASE + f"/artifacts/{DIGEST}.apk").status_code == 404
    manifest = publish(client)
    assert client.get(query).json() == manifest
    path = BASE + "/artifacts/" + manifest["artifacts"][0]["path"]
    response = client.get(path)
    assert response.content == DATA
    assert response.headers["etag"] == f'"{DIGEST}"'
    assert int(client.head(path).headers["content-length"]) == len(DATA)
    partial = client.get(path, headers={"Range": "bytes=5-12"})
    assert partial.status_code == 206 and partial.content == DATA[5:13]
    assert client.get(BASE + "/manifest?platform=windows&architecture=x64").status_code == 404
    assert (
        client.get(
            "/v1/apps/zheri/releases/manifest?platform=android&architecture=arm64"
        ).status_code
        == 503
    )


def test_unpublished_upload_is_private_and_publication_is_immutable(service):
    client, _ = service
    assert (
        client.put(BASE + "/objects/" + DIGEST, content=DATA, headers=headers()).status_code == 200
    )
    assert client.get(BASE + f"/artifacts/{DIGEST}.apk").status_code == 404
    first = publish(client)
    assert client.post(BASE + "/publish", json=publication(), headers=headers()).json() == first
    changed = publication()
    changed["releaseNotes"] = {"en": ["different"]}
    assert client.post(BASE + "/publish", json=changed, headers=headers()).status_code == 409
    assert (
        client.post(BASE + "/publish", json=publication(9000), headers=headers()).status_code == 409
    )


def test_no_account_or_wrong_key_can_publish(service):
    client, _ = service
    assert client.put(BASE + "/objects/" + DIGEST, content=b"x").status_code == 403
    assert client.post(BASE + "/publish", json=publication()).status_code == 403
    assert (
        client.put(
            "/v1/apps/zheri/releases/objects/" + DIGEST, content=b"x", headers=headers()
        ).status_code
        == 503
    )
    assert (
        client.put(BASE + "/objects/" + DIGEST, content=b"bad", headers=headers()).status_code
        == 422
    )
    assert client.post(BASE + "/publish", json=publication(), headers=headers()).status_code == 503


def test_database_scope_and_unauthorized_writes(db, service):
    client, _ = service
    publish(client)
    with transaction(db.runtime, "zheri") as c:
        assert not c.execute(sa.select(m.releases)).all()
    with transaction(db.runtime, "board_game_agent") as c:
        assert len(c.execute(sa.select(m.releases)).all()) == 1
        assert not c.execute(sa.select(m.blobs)).all()
    with pytest.raises(DBAPIError), transaction(db.runtime, "board_game_agent") as c:
        c.execute(m.blobs.insert().values(app_id="board_game_agent", sha256="a" * 64, size=1))


def test_file_corruption_and_invalid_targets_are_rejected(service):
    client, root = service
    result = publish(client)
    path = root / "board_game_agent" / "objects" / DIGEST[:2] / DIGEST
    path.write_bytes(b"changed")
    assert client.get(BASE + "/artifacts/" + result["artifacts"][0]["path"]).status_code == 503
    wrong = publication(9002)
    wrong["appId"] = "zheri"
    assert client.post(BASE + "/publish", json=wrong, headers=headers()).status_code == 422
    assert client.get(BASE + "/artifacts/%2e%2e").status_code == 404


def test_windows_and_android_have_independent_public_versions(service):
    client, _ = service
    android = publish(client)
    data = b"MZ" + b"windows-installer-fixture"
    digest = hashlib.sha256(data).hexdigest()
    assert (
        client.put(BASE + "/objects/" + digest, content=data, headers=headers()).status_code == 200
    )
    body = publication(7000)
    body["artifacts"] = [
        {
            "platform": "windows",
            "architecture": "x64",
            "path": "test-setup.exe",
            "sha256": digest,
            "size": len(data),
            "mimeType": "application/octet-stream",
        }
    ]
    result = client.post(BASE + "/publish", json=body, headers=headers())
    assert result.status_code == 200
    assert (
        client.get(BASE + "/manifest?platform=windows&architecture=x64").json()["buildNumber"]
        == 7000
    )
    assert client.get(BASE + "/manifest?platform=android&architecture=arm64").json() == android
    assert client.get(BASE + "/artifacts/" + digest + ".exe").content == data
    body["releaseRecord"] = {"artifacts": None}
    assert client.post(BASE + "/publish", json=body, headers=headers()).status_code == 422


def test_generic_any_apk_uses_the_same_contract_for_all_device_abis(service):
    client, _ = service
    body = publication()
    body["artifacts"][0]["architecture"] = "any"
    result = publish(client, body)
    assert result["artifacts"][0]["architecture"] == "any"
    for architecture in ("any", "arm64", "arm", "x64", "x86"):
        response = client.get(BASE + f"/manifest?platform=android&architecture={architecture}")
        assert response.status_code == 200, response.text
        assert response.json() == result
    assert client.get(BASE + "/manifest?platform=android&architecture=universal").status_code == 422
    body["artifacts"][0]["architecture"] = "universal"
    assert client.post(BASE + "/publish", json=body, headers=headers()).status_code == 422
    assert client.get(BASE + "/manifest?platform=windows&architecture=any").status_code == 422
    assert client.get(BASE + "/artifacts/" + DIGEST + ".apk").content == DATA


def test_forward_migration_normalizes_existing_target_without_rewriting_audit(service, db):
    client, _ = service
    publish(client)
    assert db.runtime.url.database.startswith("app_data_test_")
    with transaction(db.runtime, "board_game_agent") as c:
        row = dict(c.execute(sa.select(m.releases)).mappings().one())
    original_audit = publication(9002)
    original_audit["artifacts"][0]["architecture"] = "universal"
    with db.migrator.connect() as c:
        tx = c.begin()
        try:
            # Only conftest's random test DB; rollback restores all schema/data.
            c.exec_driver_sql("ALTER TABLE core.public_releases NO FORCE ROW LEVEL SECURITY")
            c.exec_driver_sql(
                "ALTER TABLE core.public_releases "
                "DROP CONSTRAINT public_releases_architecture_check"
            )
            c.exec_driver_sql(
                "ALTER TABLE core.public_releases "
                "ADD CONSTRAINT public_releases_architecture_check "
                "CHECK(architecture IN ('arm64','arm','x64','x86','universal'))"
            )
            c.execute(
                m.releases.insert().values(
                    **(
                        row
                        | {
                            "architecture": "universal",
                            "build_number": 9002,
                            "record": original_audit,
                        }
                    )
                )
            )
            c.exec_driver_sql("ALTER TABLE core.public_releases FORCE ROW LEVEL SECURITY")
            path = Path(__file__).parents[1] / "migrations/versions/0016_release_architecture.py"
            with Operations.context(MigrationContext.configure(c)):
                runpy.run_path(str(path))["upgrade"]()
            # FORCE RLS remains active for the non-runtime owner after migration.
            assert c.exec_driver_sql(
                "SELECT relforcerowsecurity FROM pg_class "
                "WHERE oid='core.public_releases'::regclass"
            ).scalar_one()
            # Inspect inside this same transaction; it is always rolled back.
            c.exec_driver_sql("ALTER TABLE core.public_releases NO FORCE ROW LEVEL SECURITY")
            migrated = (
                c.execute(sa.select(m.releases).where(m.releases.c.build_number == 9002))
                .mappings()
                .one()
            )
            assert migrated["architecture"] == "any" and migrated["record"] == original_audit
        finally:
            tx.rollback()
