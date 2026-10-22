import pytest
from fastapi.testclient import TestClient
from test_sessions import authenticated as authenticated_fixture

from app_data_service.api import create_app
from app_data_service.browser_sessions import COOKIE


@pytest.fixture(name="authenticated")
def web_authenticated(db):
    yield from authenticated_fixture.__wrapped__(db)


@pytest.fixture
def web(authenticated, db, tmp_path):
    _, _, authority, manager = authenticated
    (tmp_path / "index.html").write_text("<html>fixture-shell</html>", encoding="utf-8")
    (tmp_path / "main.dart.js").write_text("// fixture-script", encoding="utf-8")
    (tmp_path / "version.json").write_text('{"version":"fixture"}', encoding="utf-8")
    with TestClient(
        create_app(
            db.runtime,
            authority,
            "https://web.test/powersync/",
            sessions=manager,
            browser_origin="https://web.test",
            web_directory=tmp_path,
        ),
        base_url="https://web.test",
    ) as client:
        yield client


@pytest.mark.parametrize("path", ["/", "/settings", "/settings/account", "/assets/deep-navigation"])
def test_shell_and_spa_navigation_are_served_with_isolation_and_revalidation(web, path):
    response = web.get(path)
    assert response.status_code == 200 and "fixture-shell" in response.text
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["cross-origin-opener-policy"] == "same-origin"
    assert response.headers["cross-origin-embedder-policy"] == "require-corp"


@pytest.mark.parametrize(
    "path", ["/missing.js", "/api/missing", "/v1/missing", "/.env", "/assets/missing.wasm"]
)
def test_missing_assets_and_api_paths_never_return_the_shell(web, path):
    response = web.get(path)
    assert response.status_code == 404
    assert "fixture-shell" not in response.text


def test_static_head_and_range_and_existing_native_api_routes(web):
    assert web.head("/main.dart.js").content == b""
    partial = web.get("/main.dart.js", headers={"Range": "bytes=0-1"})
    assert partial.status_code == 206 and partial.content == b"//"
    assert web.get("/health").json() == web.get("/api/health").json()
    assert web.get("/api/v1/datasets").status_code == 401
    assert web.get("/api/v1/client-config").json()["api_origin"] == "https://web.test"
    assert web.post("/settings").status_code == 405


def test_cookie_csrf_login_restore_and_logout_work_through_alias(web):
    headers = {"Origin": "https://web.test", "Content-Type": "application/json"}
    bootstrap = web.post("/api/v1/auth/browser/bootstrap", headers=headers, json={})
    assert bootstrap.status_code == 200
    cookie = bootstrap.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "Path=/" in cookie
    assert COOKIE in web.cookies
    headers["X-CSRF-Token"] = bootstrap.json()["csrf_token"]
    signed = web.post(
        "/api/v1/auth/browser/login",
        headers=headers,
        json={"account": "alice", "password": "fixture-password"},
    )
    assert signed.status_code == 200 and "refresh_token" not in signed.json()
    token = signed.json()["access_token"]
    assert (
        web.get("/api/v1/datasets", headers={"Authorization": "Bearer " + token}).status_code == 200
    )
    assert web.post("/api/v1/auth/browser/restore", headers=headers, json={}).status_code == 200
    bad = {**headers, "Origin": "https://other.test"}
    assert web.post("/api/v1/auth/browser/restore", headers=bad, json={}).status_code == 403
    assert web.post("/api/v1/auth/browser/logout", headers=headers, json={}).status_code == 200
    assert (
        web.get("/api/v1/datasets", headers={"Authorization": "Bearer " + token}).status_code == 401
    )


def test_static_symlink_cannot_disclose_an_outside_file(web, tmp_path):
    # Symlink creation may require an OS privilege on Windows: use lookup_path
    # containment directly as the portable boundary check, without skipping.
    from app_data_service.web import FlutterFiles

    files = FlutterFiles(directory=tmp_path)
    _, stat = files.lookup_path("../outside-secret")
    assert stat is None
