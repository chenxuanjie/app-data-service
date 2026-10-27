from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from test_board_game_business import BASE, STAMP, auth, login, upload
from test_sessions import authenticated as authenticated_fixture

from app_data_service.auth import SYNC_AUDIENCE
from app_data_service.modules.board_game_agent.models import public_changes
from app_data_service.store import user_transaction


@pytest.fixture(name="authenticated")
def accounts(db):
    yield from authenticated_fixture.__wrapped__(db)


def version(db, game):
    with db.migrator.connect() as c:
        row = (
            c.execute(sa.select(public_changes).where(public_changes.c.id == game)).mappings().one()
        )
        return row["statistics_version"], row["comments_version"]


def test_public_versions_are_transactional_idempotent_and_read_only(authenticated, db):
    client, _, _, _ = authenticated
    actor = login(client)
    game = "realtime-" + uuid4().hex
    record = {"id": game, "game_slug": game, "created_at": STAMP}
    mutation = str(uuid4())
    assert upload(client, actor, "favorites", record, identifier=mutation).status_code == 200
    assert version(db, game) == (1, 0)
    assert upload(client, actor, "favorites", record, identifier=mutation).status_code == 200
    assert version(db, game) == (1, 0)
    assert upload(client, actor, "favorites", None, record_id=game).status_code == 200
    assert version(db, game) == (2, 0)
    with pytest.raises(RuntimeError, match="rollback"):
        with db.migrator.begin() as c:
            c.exec_driver_sql(f"SELECT board_game_agent.touch_public_game('{game}',true)")
            raise RuntimeError("rollback")
    assert version(db, game) == (2, 0)
    with user_transaction(db.runtime, "public-reader", app_id="board_game_agent") as c:
        row = (
            c.execute(sa.select(public_changes).where(public_changes.c.id == game)).mappings().one()
        )
        assert set(row) == {"id", "statistics_version", "comments_version"}
    with pytest.raises(DBAPIError):
        with user_transaction(db.runtime, actor["user_id"], app_id="board_game_agent") as c:
            c.execute(public_changes.update().values(statistics_version=999))
    with user_transaction(db.runtime, actor["user_id"], app_id="zheri") as c:
        assert c.execute(sa.select(public_changes)).all() == []


def test_comment_publish_like_reply_delete_invalidate_only_their_game(authenticated, db):
    client, _, _, _ = authenticated
    a, b = login(client), login(client, "bob")
    game = "comments-" + uuid4().hex
    comment = "c" + uuid4().hex

    def command(actor, action, **values):
        payload = dict(id=str(uuid4()), action=action, game_slug=game, comment_id=comment, **values)
        response = client.post(BASE + "/community/commands", headers=auth(actor), json=payload)
        assert response.status_code == 200, response.text
        return payload

    command(a, "publish", body="Public comment", rating=8)
    assert version(db, game) == (0, 1)
    payload = command(b, "like", active=True)
    assert version(db, game) == (0, 2)
    assert (
        client.post(BASE + "/community/commands", headers=auth(b), json=payload).status_code == 200
    )
    assert version(db, game) == (0, 2)
    command(b, "like", active=False)
    assert version(db, game) == (0, 3)
    reply = "c" + uuid4().hex
    payload = dict(
        id=reply,
        action="publish",
        game_slug=game,
        comment_id=reply,
        body="Reply",
        parent_id=comment,
    )
    assert (
        client.post(BASE + "/community/commands", headers=auth(b), json=payload).status_code == 200
    )
    assert version(db, game) == (0, 4)
    command(a, "delete")
    assert version(db, game) == (0, 5)


def test_anonymous_credential_is_sync_only_and_cannot_write(authenticated):
    client, _, authority, _ = authenticated
    response = client.post(
        BASE + "/community/sync-credentials", headers={"X-App-ID": "board_game_agent"}, json={}
    )
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    data = response.json()
    claims = authority.claims(data["token"], audience=SYNC_AUDIENCE)
    assert claims["sync_scope"] == data["sync_scope"] == "public"
    assert claims["sub"] == "public:board_game_agent"
    assert claims["app_id"] == "board_game_agent"
    assert claims["exp"] - claims["iat"] == 300
    headers = {"Authorization": "Bearer " + data["token"], "X-App-ID": "board_game_agent"}
    assert client.get(BASE + "/datasets", headers=headers).status_code == 401
    assert client.post(BASE + "/sync/upload", headers=headers, json={}).status_code == 401
