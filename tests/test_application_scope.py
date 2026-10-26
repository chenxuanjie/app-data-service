"""Shared identity boundaries using real PostgreSQL and independent DSM sessions."""

import jwt
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError
from test_sessions import authenticated as authenticated_fixture

from app_data_service.api import create_app
from app_data_service.models import AppSession, Dataset, DatasetMember
from app_data_service.store import user_transaction


@pytest.fixture(name="authenticated")
def scoped_authenticated(db):
    for state in authenticated_fixture.__wrapped__(db):
        state[0].headers.pop("X-App-ID", None)
        yield state


def path(app, suffix):
    identifier = app["app_id"] if isinstance(app, dict) else app
    return f"/v1/apps/{identifier}{suffix}"


def login(client, app_id):
    response = client.post(
        path(app_id, "/auth/login"), json={"account": "alice", "password": "fixture"}
    )
    assert response.status_code == 200, response.text
    return response.json()


def headers(account):
    return {"X-App-ID": account["app_id"], "Authorization": "Bearer " + account["access_token"]}


def test_one_user_has_two_private_spaces_and_independent_logout(authenticated):
    client, _, authority, _ = authenticated
    board, zheri = login(client, "board_game_agent"), login(client, "zheri")
    assert board["user_id"] == zheri["user_id"] and board["dataset_id"] != zheri["dataset_id"]
    for account in (board, zheri):
        assert authority.claims(account["access_token"])["app_id"] == account["app_id"]
        result = client.get(path(account, "/datasets"), headers=headers(account)).json()["datasets"]
        assert [r["id"] for r in result] == [account["dataset_id"]]
    assert (
        client.post(
            path(board, "/auth/logout"),
            headers=headers(board),
            json={"refresh_token": board["refresh_token"]},
        ).status_code
        == 200
    )
    assert client.get(path(board, "/datasets"), headers=headers(board)).status_code == 401
    assert client.get(path(zheri, "/datasets"), headers=headers(zheri)).status_code == 200
    assert (
        client.post(
            path(zheri, "/auth/refresh"),
            headers=headers(zheri),
            json={"refresh_token": zheri["refresh_token"]},
        ).status_code
        == 200
    )


def test_board_login_does_not_create_a_zheri_ledger(authenticated, db):
    client, *_ = authenticated
    board = login(client, "board_game_agent")
    with user_transaction(db.runtime, board["user_id"], app_id="zheri") as c:
        assert c.scalar(sa.select(Dataset.id)) is None
    with user_transaction(db.runtime, board["user_id"], app_id="board_game_agent") as c:
        assert c.scalar(sa.select(Dataset.id)) == board["dataset_id"]


def test_forged_app_header_cannot_read_refresh_or_revoke_another_app(authenticated):
    client, *_ = authenticated
    zheri = login(client, "zheri")
    token_only = {"Authorization": "Bearer " + zheri["access_token"]}
    assert client.get(path("board_game_agent", "/datasets"), headers=token_only).status_code == 401
    for action in ("refresh", "logout"):
        assert (
            client.post(
                path("board_game_agent", f"/auth/{action}"),
                json={"refresh_token": zheri["refresh_token"]},
            ).status_code
            == 401
        )
    assert client.get(path(zheri, "/datasets"), headers=headers(zheri)).status_code == 200
    board = login(client, "board_game_agent")
    for suffix in (
        "/files/resources/private.txt",
        "/updates/manifest",
        "/conflicts?dataset_id=" + zheri["dataset_id"],
    ):
        assert client.get(path(board, suffix), headers=headers(board)).status_code == 404
    assert client.get(path(board, "/services"), headers=headers(board)).json()["file_areas"] == []
    assert (
        client.post(path(board, "/sync/upload"), headers=headers(board), json={}).status_code == 404
    )
    assert client.get("/v1/datasets", headers=headers(board)).status_code == 401


def test_rls_filters_same_users_other_app_and_resets_pooled_context(authenticated, db):
    client, *_ = authenticated
    zheri, board = login(client, "zheri"), login(client, "board_game_agent")
    for account in (zheri, board):
        with user_transaction(db.runtime, account["user_id"], app_id=account["app_id"]) as c:
            assert c.execute(sa.select(Dataset.id)).scalars().all() == [account["dataset_id"]]
            assert c.execute(sa.select(DatasetMember.dataset_id)).scalars().all() == [
                account["dataset_id"]
            ]
            rows = c.execute(sa.select(AppSession)).mappings().all()
            assert rows and all(r["app_id"] == account["app_id"] for r in rows)
    with db.runtime.begin() as c:
        c.execute(sa.text("SELECT set_config('app.user_id',:id,true)"), {"id": zheri["user_id"]})
        for table in (
            "core.datasets",
            "core.dataset_members",
            "core.app_sessions",
            "core.personal_spaces",
            "zheri.expenses",
        ):
            assert c.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar_one() == 0
        with pytest.raises(DBAPIError):
            c.exec_driver_sql("SELECT core.ensure_app_account('not scoped')")


def test_signed_sync_credentials_carry_application_not_client_parameters(authenticated):
    client, _, authority, _ = authenticated
    for app_id in ("zheri", "board_game_agent"):
        account = login(client, app_id)
        data = client.post(path(account, "/sync/credentials"), headers=headers(account)).json()
        claims = authority.claims(data["token"], audience="app-data-powersync")
        assert claims["app_id"] == app_id == data["app_id"] and claims["sub"] == account["user_id"]


def test_missing_unknown_and_unscoped_application_fail_closed(authenticated):
    client, _, authority, _ = authenticated
    account = login(client, "zheri")
    assert client.get(path("unknown", "/client-config")).status_code == 400
    assert (
        client.get(
            path("board_game_agent", "/client-config"), headers={"X-App-ID": "zheri"}
        ).status_code
        == 400
    )
    claims = authority.claims(account["access_token"])
    del claims["app_id"]
    token = jwt.encode(claims, authority.key, algorithm="RS256", headers={"kid": authority.kid})
    assert (
        client.get(
            path("zheri", "/datasets"), headers={"Authorization": "Bearer " + token}
        ).status_code
        == 401
    )


def test_browser_cookies_csrf_and_logout_are_application_scoped(authenticated, db):
    _, _, authority, manager = authenticated
    app = create_app(
        db.runtime,
        authority,
        "https://data.test/powersync",
        sessions=manager,
        application_settings={
            "zheri": {"browser_origin": "https://zheri.test"},
            "board_game_agent": {"browser_origin": "https://board.test"},
        },
    )
    with (
        TestClient(app, base_url="https://zheri.test") as z,
        TestClient(app, base_url="https://board.test") as b,
    ):
        for client, identifier, origin in (
            (z, "zheri", "https://zheri.test"),
            (b, "board_game_agent", "https://board.test"),
        ):
            client.headers["Origin"] = origin
            boot = client.post(path(identifier, "/auth/browser/bootstrap"), json={})
            client.headers["X-CSRF-Token"] = boot.json()["csrf_token"]
            assert (
                client.post(
                    path(identifier, "/auth/browser/login"),
                    json={"account": "alice", "password": "fixture"},
                ).status_code
                == 200
            )
        assert set(z.cookies) == {"__Host-app-session-zheri"}
        assert set(b.cookies) == {"__Host-app-session-board_game_agent"}
        b.cookies.set("__Host-app-session-zheri", z.cookies["__Host-app-session-zheri"])
        assert b.post(path("zheri", "/auth/browser/restore"), json={}).status_code == 403
        assert b.post(path("board_game_agent", "/auth/browser/logout"), json={}).status_code == 200
        assert z.post(path("zheri", "/auth/browser/restore"), json={}).status_code == 200


@pytest.mark.parametrize("origin", ["https://shared.test", "https://SHARED.test:443/"])
def test_shared_browser_origin_is_rejected(authenticated, db, origin):
    _, _, authority, manager = authenticated
    with pytest.raises(ValueError, match="own browser origin"):
        create_app(
            db.runtime,
            authority,
            "https://data.test/powersync",
            sessions=manager,
            application_settings={
                "zheri": {"browser_origin": "https://shared.test"},
                "board_game_agent": {"browser_origin": origin},
            },
        )
