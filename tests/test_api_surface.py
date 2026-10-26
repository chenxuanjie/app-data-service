import pytest
from fastapi.testclient import TestClient
from test_sessions import authenticated as authenticated_fixture

from app_data_service.api import create_app

COOKIE = "__Host-app-session-zheri"


@pytest.fixture(name="authenticated")
def web_authenticated(db):
    yield from authenticated_fixture.__wrapped__(db)


@pytest.fixture
def web(authenticated, db):
    _, _, authority, manager = authenticated
    with TestClient(
        create_app(
            db.runtime,
            authority,
            "https://web.test/powersync/",
            sessions=manager,
            application_settings={"zheri": {"browser_origin": "https://web.test"}},
        ),
        base_url="https://web.test",
        headers={"X-App-ID": "zheri"},
    ) as client:
        yield client


@pytest.mark.parametrize(
    "path",
    ["/", "/index.html", "/settings/account", "/main.dart.js", "/.env", "/api/v1/client-config"],
)
def test_api_runtime_does_not_serve_pages_or_frontend_aliases(web, path):
    assert web.get(path).status_code == 404


def test_cookie_csrf_login_restore_and_logout_without_static_frontend(web):
    headers = {"Origin": "https://web.test", "Content-Type": "application/json"}
    bootstrap = web.post("/v1/auth/browser/bootstrap", headers=headers, json={})
    assert bootstrap.status_code == 200
    cookie = bootstrap.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "Path=/" in cookie
    assert COOKIE in web.cookies
    headers["X-CSRF-Token"] = bootstrap.json()["csrf_token"]
    signed = web.post(
        "/v1/auth/browser/login",
        headers=headers,
        json={"account": "alice", "password": "fixture-password"},
    )
    assert signed.status_code == 200 and "refresh_token" not in signed.json()
    token = signed.json()["access_token"]
    assert web.get("/v1/datasets", headers={"Authorization": "Bearer " + token}).status_code == 200
    assert web.post("/v1/auth/browser/restore", headers=headers, json={}).status_code == 200
    bad = {**headers, "Origin": "https://other.test"}
    assert web.post("/v1/auth/browser/restore", headers=bad, json={}).status_code == 403
    assert web.post("/v1/auth/browser/logout", headers=headers, json={}).status_code == 200
    assert web.get("/v1/datasets", headers={"Authorization": "Bearer " + token}).status_code == 401
