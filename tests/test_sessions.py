import hashlib
from urllib.parse import parse_qs
from uuid import uuid4

import httpx
import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from app_data_service.api import create_app
from app_data_service.auth import API_AUDIENCE, TokenAuthority
from app_data_service.dsm import DsmProvider
from app_data_service.models import AppSession, User
from app_data_service.sessions import SessionManager
from app_data_service.store import user_transaction


@pytest.fixture
def authenticated(db):
    class Dsm:
        error = None
        unavailable = False
        wrong_uid = False
        invalid_path = False
        sessions = {}
        calls = []
        seed = uuid4().int % 100000000 + 10000

        def request(self, request):
            self.calls.append(request)
            assert request.method == "POST" and not request.url.query
            values = {key: value[0] for key, value in parse_qs(request.content.decode()).items()}
            if self.unavailable:
                raise httpx.ConnectError("not exposed: fixture-secret", request=request)
            if values["api"] == "SYNO.API.Info":
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "data": {
                            "SYNO.API.Auth": {
                                "path": "../unsafe.cgi" if self.invalid_path else "entry.cgi",
                                "minVersion": 1,
                                "maxVersion": 7,
                            },
                            "SYNO.FileStation.Info": {
                                "path": "entry.cgi",
                                "minVersion": 1,
                                "maxVersion": 2,
                            },
                        },
                    },
                )
            if values["method"] == "login":
                if self.error:
                    return httpx.Response(
                        200, json={"success": False, "error": {"code": self.error}}
                    )
                uid = self.seed + (values["account"] == "bob")
                sid = "fixture-sid-" + str(uuid4())
                self.sessions[sid] = uid
                return httpx.Response(
                    200, json={"success": True, "data": {"sid": sid, "synotoken": "fixture-syno"}}
                )
            assert "_sid" not in values and "SynoToken" not in values
            assert request.headers["X-SYNO-TOKEN"] == "fixture-syno"
            sid = request.headers["Cookie"].removeprefix("id=")
            if values["method"] == "logout":
                self.sessions.pop(sid, None)
                return httpx.Response(200, json={"success": True})
            uid = self.sessions.get(sid)
            return httpx.Response(
                200, json={"success": True, "data": {"uid": uid + 1 if self.wrong_uid else uid}}
            )

    fixture = Dsm()
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    authority = TokenAuthority(private, "https://data.test")
    provider = DsmProvider(
        "https://dsm.test:5001",
        identity_origin="https://old-dsm.test:5001",
        transport=httpx.MockTransport(fixture.request),
    )
    manager = SessionManager(db.runtime, authority, provider, private)
    with TestClient(
        create_app(db.runtime, authority, "https://data.test/powersync", sessions=manager),
        headers={"X-App-ID": "zheri"},
    ) as client:
        yield client, fixture, authority, manager


def login(client, name="alice"):
    response = client.post(
        "/v1/auth/login", json={"account": name, "password": "fixture-password", "otp": "123456"}
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_verified_uid_provisions_same_personal_dataset_and_private_session(authenticated, db):
    client, dsm, authority, manager = authenticated
    a, second, b = login(client), login(client), login(client, "bob")
    expected = hashlib.sha256(
        f"synology-direct-v1|https://old-dsm.test:5001|{dsm.seed}".encode()
    ).hexdigest()
    assert a["user_id"] == expected and a["dataset_id"] == "personal-" + expected
    assert a["dataset_id"] == second["dataset_id"] != b["dataset_id"]
    assert not {"sid", "synotoken", "password"} & a.keys()
    client.headers["Authorization"] = "Bearer " + a["access_token"]
    assert client.get("/v1/datasets").json() == {
        "datasets": [{"id": a["dataset_id"], "name": "个人账本", "kind": "personal"}]
    }
    assert client.post("/v1/sync/credentials").status_code == 200
    with user_transaction(db.runtime, expected, app_id="zheri") as c:
        row = (
            c.execute(sa.select(AppSession).where(AppSession.user_id == expected))
            .mappings()
            .first()
        )
        assert "fixture-sid" not in row["dsm_context"]
        assert a["refresh_token"] != row["refresh_hash"]
    with user_transaction(db.runtime, b["user_id"], app_id="zheri") as c:
        assert c.scalar(sa.select(AppSession.id).where(AppSession.user_id == expected)) is None
    with pytest.raises(DBAPIError):
        with db.replication.begin() as c:
            c.execute(sa.select(AppSession))
    # A valid signature with no server session is not accepted in DSM mode.
    client.headers["Authorization"] = "Bearer " + authority.issue(
        expected, API_AUDIENCE, app_id="zheri"
    )
    assert client.get("/v1/datasets").status_code == 401


def test_refresh_verifies_upstream_and_logout_revokes_immediately(authenticated):
    client, dsm, _, _ = authenticated
    account = login(client)
    body = {"refresh_token": account["refresh_token"]}
    response = client.post("/v1/auth/refresh", json=body)
    assert response.status_code == 200 and response.json()["user_id"] == account["user_id"]
    dsm.wrong_uid = True
    assert client.post("/v1/auth/refresh", json=body).status_code == 401
    dsm.wrong_uid = False
    dsm.unavailable = True
    assert client.post("/v1/auth/refresh", json=body).status_code == 503
    # App logout works even when upstream DSM is unreachable.
    assert client.post("/v1/auth/logout", json=body).status_code == 200
    client.headers["Authorization"] = "Bearer " + account["access_token"]
    assert client.get("/v1/datasets").status_code == 401
    assert client.post("/v1/auth/refresh", json=body).status_code == 401


def test_existing_primary_space_login_keeps_dataset_identity(authenticated, db):
    client, dsm, _, _ = authenticated
    actor = hashlib.sha256(
        f"synology-direct-v1|https://old-dsm.test:5001|{dsm.seed}".encode()
    ).hexdigest()
    dataset = "test-" + actor
    with db.migrator.begin() as c:
        c.execute(
            sa.text("INSERT INTO core.users(id,display_name) VALUES(:actor,'旧账号')"),
            {"actor": actor},
        )
        c.execute(
            sa.text("""INSERT INTO core.datasets(id,app_id,owner_id,name,kind)
            VALUES(:id,'zheri',:actor,'个人账本','personal')"""),
            {"id": dataset, "actor": actor},
        )
        c.execute(
            sa.text("INSERT INTO core.dataset_members(dataset_id,user_id) VALUES(:id,:actor)"),
            {"id": dataset, "actor": actor},
        )
        c.execute(
            sa.text(
                "INSERT INTO core.personal_spaces(app_id,user_id,dataset_id) "
                "VALUES('zheri',:actor,:id)"
            ),
            {"actor": actor, "id": dataset},
        )
    first = login(client)
    assert first["dataset_id"] == dataset == "test-" + actor
    assert first["dataset_kind"] == "personal"
    client.headers["Authorization"] = "Bearer " + first["access_token"]
    second = login(client)
    assert second["dataset_id"] == dataset and second["dataset_kind"] == "personal"
    assert client.get("/v1/datasets").json() == {
        "datasets": [{"id": dataset, "name": "个人账本", "kind": "personal"}]
    }


@pytest.mark.parametrize(
    "code,detail,status",
    [
        (400, "invalid_credentials", 401),
        (403, "otp_required", 401),
        (404, "invalid_otp", 401),
        (407, "rate_limited", 429),
        (410, "password_change_required", 403),
    ],
)
def test_dsm_errors_do_not_issue_sessions_or_expose_secrets(authenticated, code, detail, status):
    client, dsm, _, _ = authenticated
    dsm.error = code
    response = client.post(
        "/v1/auth/login", json={"account": "alice", "password": "fixture-password"}
    )
    assert response.status_code == status and response.json() == {"detail": detail}
    assert not dsm.sessions


def test_unsafe_discovery_rejected_before_password_is_sent(authenticated):
    client, dsm, _, _ = authenticated
    dsm.invalid_path = True
    response = client.post(
        "/v1/auth/login", json={"account": "alice", "password": "fixture-password"}
    )
    assert response.status_code == 503 and len(dsm.calls) == 1


def test_total_login_deadline_is_preserved_and_reported_as_timeout(authenticated, monkeypatch):
    import asyncio

    from app_data_service import auth_routes

    client, dsm, _, manager = authenticated

    async def stalled_login(*args):
        await asyncio.sleep(1)
        raise AssertionError("The total deadline must cancel this operation")

    monkeypatch.setattr(manager.provider, "login", stalled_login)
    monkeypatch.setattr(auth_routes, "LOGIN_TIMEOUT_SECONDS", 0.05)
    result = client.post(
        "/v1/auth/login", json={"account": "alice", "password": "fixture-password"}
    )
    assert result.status_code == 504
    assert result.json() == {"detail": "provider_timeout"}
    assert not dsm.sessions


def test_account_creation_function_requires_valid_context_and_no_direct_grants(db):
    with pytest.raises(DBAPIError):
        with db.runtime.begin() as c:
            c.exec_driver_sql("SELECT core.ensure_app_account('invalid')")
    with pytest.raises(DBAPIError):
        with user_transaction(db.runtime, "alice", app_id="zheri") as c:
            c.execute(sa.insert(User).values(id="illegal", display_name="illegal"))
