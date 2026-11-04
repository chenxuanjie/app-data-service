"""Authentication isolation across apps, shared NATs and verified proxy chains."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from test_sessions import authenticated as authenticated_fixture

from app_data_service import auth_limits
from app_data_service.api import create_app
from app_data_service.applications import request_application
from app_data_service.auth_limits import AuthQuota
from app_data_service.auth_routes import build_router
from app_data_service.client_address import TrustedClientAddress, trusted_proxy_networks


@pytest.mark.parametrize(
    "proxies", [["*"], ["0.0.0.0/0"], ["::/0"], ["proxy.test"], ["1.2.3.4:80"], ["127.0.0.1"] * 33]
)
def test_proxy_trust_rejects_wildcards_hostnames_and_ambiguous_addresses(proxies):
    with pytest.raises(ValueError):
        trusted_proxy_networks(proxies)


@pytest.mark.parametrize(
    "peer,forwarded,expected",
    [
        ("10.0.0.2", "203.0.113.1", "203.0.113.1"),
        ("10.0.0.3", "203.0.113.1", "10.0.0.3"),
        ("10.0.0.2", "192.0.2.9, 198.51.100.4, 10.0.0.1", "198.51.100.4"),
        ("10.0.0.2", "10.0.0.1", "10.0.0.2"),
        ("10.0.0.2", "unknown, 203.0.113.1", "10.0.0.2"),
        ("10.0.0.2", "203.0.113.1:80", "10.0.0.2"),
        ("10.0.0.2", "0.0.0.0", "10.0.0.2"),
        ("10.0.0.2", "2001:db8::123", "2001:db8::123"),
        ("::ffff:10.0.0.2", "::ffff:203.0.113.1", "203.0.113.1"),
        ("10.0.0.2", ",".join(["203.0.113.1"] * 17), "10.0.0.2"),
    ],
)
def test_only_trusted_hops_can_supply_client_address(peer, forwarded, expected):
    app = FastAPI()

    @app.get("/")
    async def who(request: Request):
        return {"client": request.client.host, "scheme": request.url.scheme}

    app.add_middleware(TrustedClientAddress, proxies=("10.0.0.2", "10.0.0.1"))
    with TestClient(app, client=(peer, 123)) as client:
        response = client.get(
            "/", headers={"X-Forwarded-For": forwarded, "X-Forwarded-Proto": "https"}
        )
        assert response.json() == {"client": expected, "scheme": "http"}
        response = client.get(
            "/", headers=[("X-Forwarded-For", "203.0.113.1"), ("X-Forwarded-For", "192.0.2.1")]
        )
        assert response.json()["client"] == peer


def test_quota_separates_apps_accounts_clients_and_expires_without_storing_account_names(
    monkeypatch,
):
    now = [0.0]
    quota = AuthQuota(clock=lambda: now[0])
    for index in range(10):
        quota.login("board_game_agent", str(index), "Alice")
    with pytest.raises(HTTPException) as denied:
        quota.login("board_game_agent", "new-client", " alice ")
    assert denied.value.status_code == 429
    quota.login("zheri", "new-client", "Alice")
    quota.login("board_game_agent", "new-client", "Bob")
    assert "Alice" not in repr(quota.applications) and "alice" not in repr(quota.applications)
    now[0] = 60
    quota.login("board_game_agent", "new-client", "Alice")
    monkeypatch.setattr(auth_limits, "MAX_KEYS_PER_APPLICATION", 3)
    quota.login("board_game_agent", "new-client", "Bob")
    with pytest.raises(HTTPException):
        quota.login("board_game_agent", "new-client", "Carol")
    # Full storage in one app neither evicts a live budget nor blocks another app.
    quota.login("other-app", "new-client", "Carol")


@pytest.fixture(name="authenticated")
def auth_fixture(db):
    yield from authenticated_fixture.__wrapped__(db)


def service(authenticated, db):
    _, _, authority, sessions = authenticated
    return create_app(
        db.runtime,
        authority,
        "https://api.test/powersync",
        sessions=sessions,
        trusted_proxies=("10.0.0.2",),
        application_settings={
            "zheri": {"browser_origin": "https://zheri.test"},
            "board_game_agent": {"browser_origin": "https://board.test"},
        },
    )


def test_eleven_users_behind_one_proxy_do_not_share_ten_attempts_across_apps(authenticated, db):
    app = service(authenticated, db)
    with TestClient(app, client=("10.0.0.2", 123)) as client:
        for index in range(11):
            app_id = "board_game_agent" if index < 10 else "zheri"
            response = client.post(
                f"/v1/apps/{app_id}/auth/login",
                json={"account": f"user-{index}", "password": "fixture"},
                headers={"X-Forwarded-For": f"203.0.113.{index + 1}"},
            )
            assert response.status_code == 200, response.text
        # Distinct users sharing a NAT can also log in without a ten-user ceiling.
        for index in range(11):
            response = client.post(
                "/v1/apps/board_game_agent/auth/login",
                json={"account": f"nat-{index}", "password": "fixture"},
                headers={"X-Forwarded-For": "198.51.100.1"},
            )
            assert response.status_code == 200, response.text


def test_account_limit_is_shared_by_aliases_and_client_changes_but_not_apps(authenticated, db):
    app = service(authenticated, db)
    with TestClient(app, client=("10.0.0.2", 123)) as client:
        for index in range(10):
            route = "/v1/auth/login" if index % 2 else "/v1/apps/zheri/auth/login"
            response = client.post(
                route,
                headers={
                    "X-App-ID": "zheri",
                    "X-Forwarded-For": f"203.0.113.{index + 1}",
                },
                json={"account": "alice", "password": "fixture"},
            )
            assert response.status_code == 200
        response = client.post(
            "/v1/apps/zheri/auth/login",
            headers={"X-Forwarded-For": "198.51.100.4"},
            json={"account": "Alice", "password": "fixture"},
        )
        assert response.status_code == 429 and response.headers["Retry-After"] == "60"
        response = client.post(
            "/v1/apps/board_game_agent/auth/login", json={"account": "alice", "password": "fixture"}
        )
        assert response.status_code == 200


def test_bootstrap_limit_is_per_application_and_verified_client(authenticated, db):
    app = service(authenticated, db)
    with TestClient(app, base_url="https://board.test", client=("10.0.0.2", 123)) as client:
        headers = {"Origin": "https://board.test", "X-Forwarded-For": "203.0.113.1"}
        for _ in range(30):
            assert (
                client.post(
                    "/v1/apps/board_game_agent/auth/browser/bootstrap", headers=headers, json={}
                ).status_code
                == 200
            )
        assert (
            client.post(
                "/v1/apps/board_game_agent/auth/browser/bootstrap", headers=headers, json={}
            ).status_code
            == 429
        )
        assert (
            client.post(
                "/v1/apps/board_game_agent/auth/browser/bootstrap",
                headers={**headers, "X-Forwarded-For": "203.0.113.2"},
                json={},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/v1/apps/zheri/auth/browser/bootstrap",
                headers={**headers, "Origin": "https://zheri.test"},
                json={},
            ).status_code
            == 200
        )


def test_untrusted_peer_cannot_rotate_forwarded_ips_to_escape_its_client_budget(
    authenticated, db, monkeypatch
):
    monkeypatch.setattr(auth_limits, "LOGIN_CLIENT_LIMIT", 2)
    app = service(authenticated, db)
    with TestClient(app, client=("10.0.0.3", 123)) as client:
        for index in range(3):
            result = client.post(
                "/v1/apps/board_game_agent/auth/login",
                headers={"X-Forwarded-For": f"203.0.113.{index + 1}"},
                json={"account": f"user-{index}", "password": "fixture"},
            )
            assert result.status_code == (200 if index < 2 else 429)


def test_dsm_concurrency_remains_global_across_applications():
    async def scenario():
        gate = asyncio.Event()
        active = 0

        async def login(*args):
            nonlocal active
            active += 1
            await gate.wait()
            return SimpleNamespace(context=None)

        runtime = SimpleNamespace(
            sessions=SimpleNamespace(
                provider=SimpleNamespace(login=login), create=lambda value, app: {"app_id": app}
            ),
            authority=None,
            browsers={},
            auth_quota=None,
            auth_capacity=None,
        )
        app = FastAPI()
        app.include_router(build_router(runtime))

        async def application(request: Request):
            return request.path_params["application_id"]

        app.dependency_overrides[request_application] = application
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            tasks = [
                asyncio.create_task(
                    client.post(
                        f"/v1/apps/app-{i}/auth/login",
                        json={"account": "alice", "password": "fixture"},
                    )
                )
                for i in range(4)
            ]
            try:
                async with asyncio.timeout(2):
                    while active < 4:
                        await asyncio.sleep(0)
                response = await client.post(
                    "/v1/apps/other-app/auth/login",
                    json={"account": "alice", "password": "fixture"},
                )
                assert response.status_code == 429 and active == 4
            finally:
                gate.set()
            assert all(result.status_code == 200 for result in await asyncio.gather(*tasks))
            assert (
                await client.post(
                    "/v1/apps/other-app/auth/login",
                    json={"account": "alice", "password": "fixture"},
                )
            ).status_code == 200

    asyncio.run(scenario())
