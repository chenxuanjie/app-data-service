"""Third-app acceptance: install a module and migration, reuse the unchanged platform."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives import serialization
from fastapi.testclient import TestClient
from notes_application import definition as notes_definition
from notes_application import install
from sqlalchemy.exc import DBAPIError
from test_sessions import authenticated as authenticated_fixture

from app_data_service.api import create_app
from app_data_service.applications import ApplicationDefinition, ApplicationRegistry
from app_data_service.cli import inspect_database
from app_data_service.dependencies import application_router
from app_data_service.models import App
from app_data_service.modules.registry import installed_applications
from app_data_service.registration import register_application
from app_data_service.sessions import SessionManager
from app_data_service.store import user_transaction


@pytest.fixture(scope="module")
def installed_notes(db):
    with db.migrator.begin() as c:
        install(c)
    registry = ApplicationRegistry((*installed_applications().definitions, notes_definition))
    yield registry
    # This fixture owns only its module in the generated local test database.
    assert db.migrator.url.database.startswith("app_data_test_")
    with db.migrator.begin() as c:
        c.exec_driver_sql("DROP SCHEMA reading_notes CASCADE")
        c.exec_driver_sql("DELETE FROM core.personal_spaces WHERE app_id='reading_notes'")
        c.exec_driver_sql("DELETE FROM core.app_sessions WHERE app_id='reading_notes'")
        c.exec_driver_sql("DELETE FROM core.browser_sessions WHERE app_id='reading_notes'")
        c.exec_driver_sql(
            "DELETE FROM core.dataset_members WHERE dataset_id IN "
            "(SELECT id FROM core.datasets WHERE app_id='reading_notes')"
        )
        c.exec_driver_sql("DELETE FROM core.datasets WHERE app_id='reading_notes'")
        c.exec_driver_sql("DELETE FROM core.apps WHERE id='reading_notes'")


@pytest.fixture(name="authenticated")
def accounts(db):
    yield from authenticated_fixture.__wrapped__(db)


@pytest.fixture
def third_app(db, authenticated, installed_notes):
    _, dsm, authority, old_manager = authenticated
    key = authority.key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    manager = SessionManager(
        db.runtime, authority, old_manager.provider, key, applications=installed_notes
    )
    with TestClient(
        create_app(
            db.runtime,
            authority,
            "https://data.test/powersync",
            sessions=manager,
            applications=installed_notes,
        )
    ) as client:
        yield client, dsm, manager


def route(application, suffix):
    return f"/v1/apps/{application}{suffix}"


def login(client, app, user="alice"):
    response = client.post(route(app, "/auth/login"), json={"account": user, "password": "fixture"})
    assert response.status_code == 200, response.text
    return response.json()


def auth(account):
    return {"Authorization": "Bearer " + account["access_token"]}


def test_third_app_uses_shared_login_space_business_and_independent_logout(third_app):
    client, _, manager = third_app
    accounts = {
        name: login(client, name) for name in ("zheri", "board_game_agent", "reading_notes")
    }
    assert len({a["user_id"] for a in accounts.values()}) == 1
    assert len({a["dataset_id"] for a in accounts.values()}) == 3
    note = accounts["reading_notes"]
    again = login(client, "reading_notes")
    assert note["dataset_id"] == again["dataset_id"]
    body = {
        "id": str(uuid4()),
        "dataset_id": note["dataset_id"],
        "title": "三体",
        "body": "个人笔记",
    }
    response = client.post(route("reading_notes", "/notes"), headers=auth(note), json=body)
    assert response.status_code == 200, response.text
    assert (
        client.get(route("reading_notes", "/notes"), headers=auth(note)).json()["notes"][0]["title"]
        == "三体"
    )
    assert client.get(route("reading_notes", "/capabilities"), headers=auth(note)).json()[
        "entities"
    ] == ["notes"]
    for name, account in accounts.items():
        visible = client.get(route(name, "/datasets"), headers=auth(account)).json()["datasets"]
        assert [v["id"] for v in visible] == [account["dataset_id"]]
    assert (
        client.post(
            route("reading_notes", "/auth/refresh"), json={"refresh_token": note["refresh_token"]}
        ).status_code
        == 200
    )
    assert (
        client.post(
            route("reading_notes", "/auth/logout"), json={"refresh_token": note["refresh_token"]}
        ).status_code
        == 200
    )
    assert client.get(route("reading_notes", "/notes"), headers=auth(note)).status_code == 401
    for name in ("zheri", "board_game_agent"):
        assert manager.verify_access(accounts[name]["access_token"], name) == note["user_id"]


def test_third_app_cross_user_cross_app_and_direct_database_isolation(third_app, db):
    client, _, _ = third_app
    notes, bob, board = (
        login(client, "reading_notes"),
        login(client, "reading_notes", "bob"),
        login(client, "board_game_agent"),
    )
    value = {
        "id": str(uuid4()),
        "dataset_id": notes["dataset_id"],
        "title": "私有",
        "body": "不可越界",
    }
    assert (
        client.post(route("reading_notes", "/notes"), json=value, headers=auth(notes)).status_code
        == 200
    )
    assert client.get(route("reading_notes", "/notes"), headers=auth(bob)).json()["notes"] == []
    assert client.get(route("reading_notes", "/notes"), headers=auth(board)).status_code == 401
    assert (
        client.post(
            route("reading_notes", "/notes"),
            json={**value, "id": str(uuid4()), "dataset_id": board["dataset_id"]},
            headers=auth(notes),
        ).status_code
        == 404
    )
    assert (
        client.post(
            route("zheri", "/auth/refresh"), json={"refresh_token": notes["refresh_token"]}
        ).status_code
        == 401
    )
    with user_transaction(db.runtime, notes["user_id"], app_id="board_game_agent") as c:
        assert c.exec_driver_sql("SELECT count(*) FROM reading_notes.notes").scalar_one() == 0
        with pytest.raises(DBAPIError):
            c.execute(
                sa.text("INSERT INTO reading_notes.notes VALUES(:id,:dataset,'bad','bad')"),
                {"id": uuid4(), "dataset": notes["dataset_id"]},
            )
    with db.runtime.begin() as c, pytest.raises(DBAPIError):
        c.exec_driver_sql("CREATE TABLE reading_notes.illegal(id TEXT)")


def test_registry_migration_checks_and_app_disabling(third_app, db, installed_notes):
    client, _, manager = third_app
    note = login(client, "reading_notes")
    assert client.get(route("reading_notes", "/about")).status_code == 200
    with db.migrator.begin() as c:
        c.execute(sa.update(App).where(App.id == "reading_notes").values(enabled=False))
    try:
        assert client.get(route("reading_notes", "/about")).status_code == 403
        assert client.get(route("reading_notes", "/notes"), headers=auth(note)).status_code == 403
        assert (
            client.post(
                route("reading_notes", "/auth/login"),
                json={"account": "alice", "password": "fixture"},
            ).status_code
            == 403
        )
        with user_transaction(db.runtime, note["user_id"], app_id="reading_notes") as c:
            assert c.exec_driver_sql("SELECT count(*) FROM core.datasets").scalar_one() == 0
    finally:
        with db.migrator.begin() as c:
            c.execute(sa.update(App).where(App.id == "reading_notes").values(enabled=True))
    missing = ApplicationDefinition(id="missing_app", name="未注册", schema="missing_app")
    extended = ApplicationRegistry((*installed_notes.definitions, missing))
    with pytest.raises(ValueError, match="requires migration"):
        extended.validate_database(db.runtime)
    with pytest.raises(ValueError, match="module is not installed"):
        installed_notes.get("missing_app")
    with db.runtime.begin() as c, pytest.raises(DBAPIError):
        register_application(
            c, ApplicationDefinition(id="illegal_module", name="越权", schema="reading_notes")
        )


def test_registry_settings_are_module_scoped_and_sync_streams_composed(installed_notes):
    with pytest.raises(ValueError, match="APP_DATA_APPLICATIONS"):
        installed_notes.settings_from_environment({"APP_DATA_BROWSER_ORIGIN": "https://old.test"})
    settings = installed_notes.settings_from_environment(
        {
            "APP_DATA_APPLICATIONS": json.dumps(
                {
                    "reading_notes": {
                        "browser_origin": "https://notes.test",
                        "file_roots": {"attachments": "/approved/notes"},
                    }
                }
            )
        }
    )
    assert settings["reading_notes"]["file_roots"] == {"attachments": "/approved/notes"}
    for invalid in (
        {"unknown": {}},
        {"reading_notes": {"file_roots": {"other": "/other"}}},
        {"reading_notes": {"import_path": "untrusted.code"}},
    ):
        with pytest.raises(ValueError):
            installed_notes.validate_settings(invalid)
    streams = installed_notes.sync_config()["streams"]
    assert "reading_notes_private" in streams and "private_expenses" in streams
    assert "auth.parameter('app_id')" in streams["reading_notes_private"]["query"]
    with pytest.raises(ValueError):
        ApplicationRegistry((notes_definition, notes_definition))


def test_inspection_and_registration_follow_installed_modules(db, installed_notes):
    result = inspect_database(db.urls["migrator"], applications=installed_notes)
    assert "reading_notes.notes" in result["publication"]
    with db.migrator.begin() as c:
        register_application(c, notes_definition)
        register_application(c, notes_definition)
        c.execute(
            sa.update(App).where(App.id == "reading_notes").values(personal_space_name="drift")
        )
    try:
        with pytest.raises(ValueError, match="requires migration"):
            installed_notes.validate_database(db.runtime)
        with db.migrator.begin() as c, pytest.raises(ValueError, match="explicit migration"):
            register_application(c, notes_definition)
    finally:
        with db.migrator.begin() as c:
            c.execute(
                sa.update(App)
                .where(App.id == "reading_notes")
                .values(personal_space_name="个人笔记")
            )


def test_concurrent_module_registration_is_idempotent(db):
    definition = ApplicationDefinition(
        id="registration_test", name="并发注册测试", schema="registration_test"
    )
    with db.migrator.begin() as c:
        c.exec_driver_sql("CREATE SCHEMA registration_test")
        c.exec_driver_sql("REVOKE ALL ON SCHEMA registration_test FROM PUBLIC")

    def register():
        with db.migrator.begin() as c:
            register_application(c, definition)

    try:
        with ThreadPoolExecutor(2) as workers:
            tasks = [workers.submit(register) for _ in range(2)]
            for task in tasks:
                task.result(timeout=15)
        with db.migrator.connect() as c:
            assert (
                c.scalar(sa.select(sa.func.count()).select_from(App).where(App.id == definition.id))
                == 1
            )
    finally:
        assert db.migrator.url.database.startswith("app_data_test_")
        with db.migrator.begin() as c:
            c.exec_driver_sql("DELETE FROM core.apps WHERE id='registration_test'")
            c.exec_driver_sql("DROP SCHEMA registration_test")


def test_misbound_module_routes_are_rejected_at_startup(authenticated, db, installed_notes):
    def wrong_router(runtime):
        router = application_router("board_game_agent")
        router.add_api_route("/v1/apps/reading_notes/wrong", lambda: {"wrong": True})
        return router

    bad = replace(notes_definition, router_factory=wrong_router)
    registry = ApplicationRegistry((*installed_applications().definitions, bad))
    _, _, authority, _ = authenticated
    with pytest.raises(ValueError, match="bound to its application"):
        create_app(db.runtime, authority, "https://data.test/powersync", applications=registry)


def test_account_only_module_does_not_create_a_personal_dataset(third_app, db, installed_notes):
    _, _, original = third_app
    definition = ApplicationDefinition(
        id="status_only", name="身份功能测试", schema="status_only", personal_data=False
    )
    with db.migrator.begin() as c:
        c.exec_driver_sql("CREATE SCHEMA status_only")
        c.exec_driver_sql("REVOKE ALL ON SCHEMA status_only FROM PUBLIC")
        register_application(c, definition)
    registry = ApplicationRegistry((*installed_notes.definitions, definition))
    key = original.authority.key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    manager = SessionManager(
        db.runtime, original.authority, original.provider, key, applications=registry
    )
    try:
        app = create_app(
            db.runtime, original.authority, "https://data.test/powersync", sessions=manager
        )
        with TestClient(app) as client:
            account = login(client, "status_only")
            assert account["dataset_id"] is None and account["dataset_kind"] is None
            assert client.get(route("status_only", "/datasets"), headers=auth(account)).json() == {
                "datasets": []
            }
            assert (
                client.post(
                    route("status_only", "/auth/refresh"),
                    json={"refresh_token": account["refresh_token"]},
                ).status_code
                == 200
            )
    finally:
        assert db.migrator.url.database.startswith("app_data_test_")
        with db.migrator.begin() as c:
            c.exec_driver_sql("DELETE FROM core.app_sessions WHERE app_id='status_only'")
            c.exec_driver_sql("DELETE FROM core.apps WHERE id='status_only'")
            c.exec_driver_sql("DROP SCHEMA status_only")


def test_third_app_declared_files_and_updates_use_the_same_authenticated_gateway(
    third_app, db, installed_notes
):
    original, dsm, manager = third_app
    provider_transport = manager.provider.transport
    seen = []
    manifest = {
        "schemaVersion": 1,
        "appId": "reading_notes",
        "channel": "stable",
        "versionName": "1.0.0",
        "buildNumber": 1,
        "artifacts": [
            {
                "platform": "android",
                "architecture": "arm64",
                "path": "notes.apk",
                "size": 4,
                "sha256": "a" * 64,
            }
        ],
    }

    def transport(request):
        values = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if values.get("query") == "SYNO.FileStation.Download":
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "SYNO.FileStation.Download": {
                            "path": "entry.cgi",
                            "minVersion": 1,
                            "maxVersion": 2,
                        }
                    },
                },
            )
        if values.get("api") == "SYNO.FileStation.Download":
            assert request.headers["Cookie"].removeprefix("id=") in dsm.sessions
            name = json.loads(values["path"])[0]
            seen.append(name)
            assert name.startswith(("/approved/notes/", "/approved/notes-updates/"))
            return httpx.Response(
                200,
                content=json.dumps(manifest).encode()
                if name.endswith("manifest.json")
                else b"note",
            )
        return dsm.request(request)

    manager.provider.transport = httpx.MockTransport(transport)
    try:
        app = create_app(
            db.runtime,
            manager.authority,
            "https://data.test/powersync",
            sessions=manager,
            applications=installed_notes,
            application_settings={
                "reading_notes": {
                    "file_roots": {
                        "attachments": "/approved/notes",
                        "updates": "/approved/notes-updates",
                    }
                }
            },
        )
        with TestClient(app) as client:
            account = login(client, "reading_notes")
            zheri = login(original, "zheri")
            assert (
                client.get(
                    route("reading_notes", "/files/attachments/document.txt"), headers=auth(account)
                ).content
                == b"note"
            )
            assert (
                client.get(
                    route("reading_notes", "/updates/manifest"), headers=auth(account)
                ).json()
                == manifest
            )
            count = len(seen)
            assert (
                client.get(
                    route("reading_notes", "/files/attachments/document.txt"), headers=auth(zheri)
                ).status_code
                == 401
            )
            assert len(seen) == count
            manifest["appId"] = "zheri"
            assert (
                client.get(
                    route("reading_notes", "/updates/manifest"), headers=auth(account)
                ).status_code
                == 503
            )
    finally:
        manager.provider.transport = provider_transport


def test_third_app_browser_cookie_and_csrf_are_independent(third_app, db, installed_notes):
    _, _, manager = third_app
    app = create_app(
        db.runtime,
        manager.authority,
        "https://data.test/powersync",
        sessions=manager,
        applications=installed_notes,
        application_settings={
            "reading_notes": {"browser_origin": "https://notes.test"},
            "zheri": {"browser_origin": "https://zheri.test"},
        },
    )
    with (
        TestClient(app, base_url="https://notes.test") as notes,
        TestClient(app, base_url="https://zheri.test") as zheri,
    ):
        for client, identifier, origin in (
            (notes, "reading_notes", "https://notes.test"),
            (zheri, "zheri", "https://zheri.test"),
        ):
            client.headers["Origin"] = origin
            data = client.post(route(identifier, "/auth/browser/bootstrap"), json={}).json()
            client.headers["X-CSRF-Token"] = data["csrf_token"]
            assert (
                client.post(
                    route(identifier, "/auth/browser/login"),
                    json={"account": "alice", "password": "fixture"},
                ).status_code
                == 200
            )
        assert set(notes.cookies) == {"__Host-app-session-reading_notes"}
        assert (
            notes.post(
                route("reading_notes", "/auth/browser/restore"),
                json={},
                headers={"X-CSRF-Token": zheri.headers["X-CSRF-Token"]},
            ).status_code
            == 403
        )
        assert (
            notes.post(route("reading_notes", "/auth/browser/logout"), json={}).status_code == 200
        )
        assert zheri.post(route("zheri", "/auth/browser/restore"), json={}).status_code == 200
