import hashlib
import json
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError
from test_sessions import authenticated as authenticated_fixture

from app_data_service.api import create_app
from app_data_service.modules.board_game_agent.models import favorites
from app_data_service.store import user_transaction

BASE = "/v1/apps/board_game_agent"
STAMP = "2026-10-26T00:00:00Z"


@pytest.fixture(name="authenticated")
def accounts(db):
    yield from authenticated_fixture.__wrapped__(db)


def login(client, name="alice", app="board_game_agent"):
    r = client.post(
        f"/v1/apps/{app}/auth/login",
        headers={"X-App-ID": app},
        json={"account": name, "password": "fixture"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def auth(account):
    return {"Authorization": "Bearer " + account["access_token"], "X-App-ID": "board_game_agent"}


def upload(
    client,
    account,
    table,
    record,
    *,
    identifier=None,
    record_id=None,
    dataset=None,
    changed_fields=None,
):
    return client.post(
        BASE + "/sync/upload",
        headers=auth(account),
        json={
            "dataset_id": dataset or account["dataset_id"],
            "mutations": [
                {
                    "id": identifier or str(uuid4()),
                    "entity": table,
                    "record_id": record_id or record["id"],
                    "record": record,
                    "changed_fields": changed_fields,
                }
            ],
        },
    )


def test_typed_private_records_idempotency_and_application_isolation(authenticated, db):
    client, _, _, _ = authenticated
    a, b = login(client), login(client, "bob")
    z = login(client, app="zheri")
    record = {"id": "cabo", "game_slug": "cabo", "created_at": STAMP}
    key = str(uuid4())
    first = upload(client, a, "favorites", record, identifier=key)
    assert first.status_code == 200, first.text
    assert upload(client, a, "favorites", record, identifier=key).json() == first.json()
    assert (
        upload(
            client, a, "favorites", {**record, "created_at": "2026-10-25T00:00:00Z"}, identifier=key
        ).status_code
        == 409
    )
    assert upload(client, b, "favorites", record, dataset=a["dataset_id"]).status_code == 404
    assert upload(client, z, "favorites", record).status_code == 401
    assert (
        upload(client, a, "preferences", {"id": "profile", "api_key": "not-cloud"}).status_code
        == 422
    )
    assert (
        upload(client, a, "preferences", {"id": "profile", "nickname": "测试玩家"}).status_code
        == 200
    )
    for actor, expected in ((a, 1), (b, 0)):
        with user_transaction(db.runtime, actor["user_id"], app_id="board_game_agent") as c:
            assert len(c.execute(sa.select(favorites)).all()) == expected
    with user_transaction(db.runtime, a["user_id"], app_id="zheri") as c:
        assert c.execute(sa.select(favorites)).all() == []
    with pytest.raises(DBAPIError):
        with user_transaction(db.runtime, b["user_id"], app_id="board_game_agent") as c:
            c.execute(favorites.insert().values(dataset_id=a["dataset_id"], **record))


def test_conversation_messages_deletion_and_public_statistics(authenticated):
    client, _, _, _ = authenticated
    a = login(client)
    assert (
        upload(
            client,
            a,
            "conversations",
            {
                "id": "conversation:test",
                "title": "规则",
                "scope": "game",
                "game_id": "cabo",
                "created_at": STAMP,
                "modified_at": STAMP,
            },
        ).status_code
        == 200
    )
    message = {
        "id": "conversation:test|m1",
        "conversation_id": "conversation:test",
        "role": "assistant",
        "content": "回答",
        "created_at": STAMP,
    }
    assert upload(client, a, "messages", message).status_code == 200
    assert upload(client, a, "messages", {**message, "state": "streaming"}).status_code == 422
    assert (
        upload(
            client, a, "favorites", {"id": "cabo", "game_slug": "cabo", "created_at": STAMP}
        ).status_code
        == 200
    )
    assert (
        upload(
            client, a, "votes", {"id": "autumn|cabo", "campaign": "autumn", "game_slug": "cabo"}
        ).status_code
        == 200
    )
    counts = client.get(BASE + "/community/statistics", headers={"X-App-ID": "board_game_agent"})
    assert counts.status_code == 200
    assert counts.json()["favorites"]["cabo"] >= 1
    assert a["user_id"] not in counts.text and a["dataset_id"] not in counts.text
    assert upload(client, a, "favorites", None, record_id="cabo").status_code == 200


def test_community_actor_ownership_replay_and_ratings(authenticated):
    client, _, _, _ = authenticated
    a, b = login(client), login(client, "bob")
    game = "test-" + uuid4().hex
    identifier = "c" + uuid4().hex
    command = {
        "id": str(uuid4()),
        "action": "publish",
        "game_slug": game,
        "comment_id": identifier,
        "body": "规则不错",
        "rating": 8,
    }

    def send(who, value):
        return client.post(BASE + "/community/commands", headers=auth(who), json=value)

    first = send(a, command)
    assert first.status_code == 200, first.text
    assert send(a, command).json() == first.json()
    assert send(a, {**command, "body": "changed"}).status_code == 409
    assert (
        send(
            b, {"id": str(uuid4()), "action": "delete", "game_slug": game, "comment_id": identifier}
        ).status_code
        == 403
    )
    assert (
        send(
            b,
            {
                "id": str(uuid4()),
                "action": "like",
                "game_slug": game,
                "comment_id": identifier,
                "active": True,
            },
        ).status_code
        == 200
    )
    public = client.get(
        BASE + "/community/comments/" + game, headers={"X-App-ID": "board_game_agent"}
    )
    assert public.status_code == 200
    result = public.json()
    assert len(result["comments"]) == 1 and result["comments"][0]["likes"] == 1
    assert a["user_id"] not in public.text and "user_id" not in public.text
    assert result["ratings"]["average"] == 8
    assert (
        send(
            a, {"id": str(uuid4()), "action": "delete", "game_slug": game, "comment_id": identifier}
        ).status_code
        == 200
    )
    assert (
        client.get(
            BASE + "/community/comments/" + game, headers={"X-App-ID": "board_game_agent"}
        ).json()["ratings"]["count"]
        == 0
    )


def test_publication_is_anonymous_allowlisted_and_integrity_checked(db, tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    from app_data_service.auth import TokenAuthority

    source, output = tmp_path / "source", tmp_path / "published"
    (source / "assets").mkdir(parents=True)
    (source / "assets/catalog.json").write_text('{"games": []}', encoding="utf-8")
    (source / "assets/rules.pdf").write_bytes(b"%PDF-fixture")
    # Existing client protocol stays readable until its separate catalog cutover.
    # Its historical fixture is not a second production publishing implementation.
    import shutil

    shutil.copytree(source, output)
    files = {
        p.relative_to(source).as_posix(): {
            "size": p.stat().st_size,
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in (source / "assets").iterdir()
    }
    (output / "publication.json").write_text(json.dumps({"version": 1, "files": files}))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    app = create_app(
        db.runtime,
        TokenAuthority(key, "http://testserver"),
        "http://testserver/powersync",
        application_settings={"board_game_agent": {"public_file_roots": {"library": str(output)}}},
    )
    with TestClient(app) as client:
        assert client.get(BASE + "/library").status_code == 200
        uri = BASE + "/library/files/assets/rules.pdf"
        response = client.get(uri)
        assert response.status_code == 200 and response.content == b"%PDF-fixture"
        assert client.head(uri).content == b""
        assert client.get(uri, headers={"Range": "bytes=0-3"}).content == b"%PDF"
        assert (
            client.get(uri, headers={"If-None-Match": response.headers["etag"]}).status_code == 304
        )
        assert client.get(BASE + "/library/files/publication.json").status_code == 404
        (output / "assets/rules.pdf").write_bytes(b"%PDF-changed")
        assert client.get(uri).status_code == 503
    assert json.loads((output / "publication.json").read_text())["version"] == 1


def test_field_preferences_merge_without_resetting_another_devices_field(authenticated, db):
    from app_data_service.modules.board_game_agent.models import preferences

    client, _, _, _ = authenticated
    account = login(client)
    assert (
        upload(
            client,
            account,
            "preferences",
            {"id": "profile", "nickname": "Alice"},
            changed_fields=["nickname"],
        ).status_code
        == 200
    )
    # Offline device still sees the old nickname. Only its changed field wins.
    assert (
        upload(
            client,
            account,
            "preferences",
            {"id": "profile", "nickname": "", "voice_reply_enabled": True},
            changed_fields=["voice_reply_enabled"],
        ).status_code
        == 200
    )
    with user_transaction(db.runtime, account["user_id"], app_id="board_game_agent") as c:
        row = (
            c.execute(
                sa.select(preferences).where(preferences.c.dataset_id == account["dataset_id"])
            )
            .mappings()
            .one()
        )
        assert row["nickname"] == "Alice" and row["voice_reply_enabled"]
    with user_transaction(db.runtime, account["user_id"], app_id="zheri") as c:
        assert c.scalar(sa.text("SELECT board_game_agent.public_counts()")) is None
    credential = client.post(BASE + "/sync/credentials", headers=auth(account)).json()
    assert credential["endpoint"].endswith("/powersync/")


def test_pagination_uses_timestamp_and_id_without_losing_tied_comments(authenticated, db):
    from app_data_service.modules.board_game_agent.models import comments

    client, _, _, _ = authenticated
    account = login(client)
    game = "cursor-" + uuid4().hex
    with user_transaction(db.runtime, account["user_id"], app_id="board_game_agent") as c:
        for _ in range(3):
            c.execute(
                comments.insert().values(
                    id="c" + uuid4().hex,
                    user_id=account["user_id"],
                    game_slug=game,
                    author_name="Alice",
                    body="same timestamp",
                    created_at=STAMP,
                )
            )
    seen = []
    cursor = None
    for _ in range(3):
        response = client.get(
            BASE + "/community/comments/" + game,
            params={"limit": 1, **({"cursor": cursor} if cursor else {})},
            headers={"X-App-ID": "board_game_agent"},
        )
        assert response.status_code == 200
        data = response.json()
        seen.append(data["comments"][0]["id"])
        cursor = data["next_cursor"]
    assert len(set(seen)) == 3 and cursor is None
