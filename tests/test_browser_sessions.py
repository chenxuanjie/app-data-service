import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.exc import TimeoutError as PoolTimeout
from test_sessions import authenticated as authenticated_fixture

from app_data_service.api import create_app
from app_data_service.browser_sessions import BrowserSessions
from app_data_service.store import user_transaction

COOKIE = "__Host-app-session-zheri"


@pytest.fixture(name="authenticated")
def browser_authenticated(db):
    yield from authenticated_fixture.__wrapped__(db)


@pytest.fixture
def browser(authenticated, db):
    _, dsm, authority, manager = authenticated
    app = create_app(
        db.runtime,
        authority,
        "https://web.test/api/powersync",
        sessions=manager,
        application_settings={"zheri": {"browser_origin": "https://web.test"}},
    )
    with TestClient(
        app,
        base_url="https://web.test",
        headers={"X-App-ID": "zheri", "Origin": "https://web.test"},
    ) as client:
        yield client, dsm, manager, app


def bootstrap(client):
    result = client.post("/v1/auth/browser/bootstrap", json={})
    assert result.status_code == 200, result.text
    client.headers["X-CSRF-Token"] = result.json()["csrf_token"]
    return result


def browser_login(client):
    result = client.post(
        "/v1/auth/browser/login", json={"account": "alice", "password": "fixture-password"}
    )
    assert result.status_code == 200, result.text
    return result.json()


def test_cookie_restore_survives_client_and_api_restart_without_js_refresh_token(browser):
    client, _, manager, app = browser
    response = bootstrap(client)
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert "Domain=" not in cookie and "Path=/" in cookie
    account = browser_login(client)
    assert "refresh_token" not in account
    assert "fixture-password" not in str(account)
    opaque = client.cookies[COOKIE]
    assert len(opaque) == 43 and "." not in opaque
    csrf = client.headers["X-CSRF-Token"]
    # New process helper, same server key; no process-local session or CSRF cache.
    replacement = BrowserSessions(manager, "https://web.test", "zheri")
    assert replacement.csrf(opaque) == csrf
    with TestClient(
        app,
        base_url="https://web.test",
        headers={"X-App-ID": "zheri", "Origin": "https://web.test"},
    ) as reopened:
        reopened.cookies.update(client.cookies)
        bootstrap(reopened)
        restored = reopened.post("/v1/auth/browser/restore", json={})
        assert restored.status_code == 200
        assert restored.json()["user_id"] == account["user_id"]
        assert set(restored.json()) == {"user_id", "access_token", "app_id"}
        assert restored.headers["cache-control"] == "no-store"


def test_cookie_csrf_origin_and_body_format_are_required(browser):
    client, _, _, _ = browser
    assert (
        client.post(
            "/v1/auth/browser/bootstrap", json={}, headers={"Origin": "https://attacker.test"}
        ).status_code
        == 403
    )
    assert client.post("/v1/auth/browser/bootstrap", content="{}").status_code == 415
    bootstrap(client)
    assert (
        client.post(
            "/v1/auth/browser/restore", json={}, headers={"X-CSRF-Token": "bad"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/v1/auth/browser/restore", json={}, headers={"Origin": "https://attacker.test"}
        ).status_code
        == 403
    )
    browser_login(client)
    assert (
        client.post(
            "/v1/auth/browser/login", json={"account": "alice", "password": "x"}
        ).status_code
        == 409
    )


def test_logout_revokes_browser_and_its_api_token_without_other_session_loss(
    browser, authenticated
):
    client, _, manager, _ = browser
    native, _, _, _ = authenticated
    native_session = native.post(
        "/v1/auth/login", json={"account": "alice", "password": "x"}
    ).json()
    bootstrap(client)
    account = browser_login(client)
    old_cookies = dict(client.cookies)
    csrf = client.headers["X-CSRF-Token"]
    assert client.post("/v1/auth/browser/logout", json={}).status_code == 200
    client.headers["Authorization"] = "Bearer " + account["access_token"]
    assert client.get("/v1/datasets").status_code == 401
    assert manager.verify_access(native_session["access_token"], "zheri") == account["user_id"]
    client.cookies.update(old_cookies)
    client.headers["X-CSRF-Token"] = csrf
    assert client.post("/v1/auth/browser/restore", json={}).status_code == 401


def test_logout_while_dsm_login_is_pending_cannot_revive_browser(browser):
    client, dsm, manager, _ = browser
    bootstrap(client)
    entered, release = Event(), Event()
    original = manager.provider.login

    async def delayed(*args):
        entered.set()
        assert await asyncio.to_thread(release.wait, 5)
        return await original(*args)

    manager.provider.login = delayed
    with ThreadPoolExecutor(1) as pool:
        pending = pool.submit(
            client.post, "/v1/auth/browser/login", json={"account": "alice", "password": "x"}
        )
        assert entered.wait(5)
        try:
            assert client.post("/v1/auth/browser/logout", json={}).status_code == 200
        finally:
            release.set()
        assert pending.result(timeout=10).status_code == 401
    assert dsm.sessions == {}


def test_browser_rows_are_hash_scoped_and_encrypted(browser, db):
    client, _, _, _ = browser
    bootstrap(client)
    browser_login(client)
    opaque = client.cookies[COOKIE]
    digest = hashlib.sha256(opaque.encode()).hexdigest()
    with db.runtime.begin() as c:
        assert c.execute(sa.text("SELECT id_hash FROM core.browser_sessions")).all() == []
        c.execute(sa.text("SELECT set_config('app.browser_id', :id, true)"), {"id": digest})
        assert c.execute(sa.text("SELECT * FROM core.browser_sessions")).all() == []
        c.exec_driver_sql("SELECT set_config('app.app_id','zheri',true)")
        row = c.execute(sa.text("SELECT * FROM core.browser_sessions")).mappings().one()
        assert "fixture" not in row["refresh_ciphertext"]
        assert opaque not in str(row)
    with user_transaction(db.runtime, "alice", app_id="zheri") as c:
        assert c.execute(sa.text("SELECT * FROM core.browser_sessions")).all() == []


def test_failed_app_revocation_keeps_browser_credential_for_retry(browser):
    client, _, manager, _ = browser
    bootstrap(client)
    browser_login(client)
    original = manager.revoke

    def unavailable(*args):
        raise PoolTimeout("fixture private diagnostic")

    manager.revoke = unavailable
    try:
        response = client.post("/v1/auth/browser/logout", json={})
        assert response.status_code == 503
        assert "fixture" not in response.text
        assert client.post("/v1/auth/browser/restore", json={}).status_code == 200
    finally:
        manager.revoke = original
    assert client.post("/v1/auth/browser/logout", json={}).status_code == 200
