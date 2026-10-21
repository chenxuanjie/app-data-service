from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from app_data_service.api import create_app
from app_data_service.auth import API_AUDIENCE, SYNC_AUDIENCE, TokenAuthority
from app_data_service.models import Expense, ExpenseConflict, MutationReceipt
from app_data_service.store import user_transaction


@pytest.fixture(scope="session")
def authority():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return TokenAuthority(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        "local-api-test",
    )


@pytest.fixture
def api(db, authority):
    with TestClient(create_app(db.runtime, authority, "http://127.0.0.1:18090")) as client:
        client.headers["Authorization"] = "Bearer " + authority.issue("alice", API_AUDIENCE)
        yield client


def mutation(record_id=None, *, version=0, predecessor=None, kind="put", **changes):
    record_id = record_id or f"api-{uuid4()}"
    return {
        "mutation_id": str(uuid4()),
        "record_id": record_id,
        "base_version": version,
        "predecessor": predecessor,
        "kind": kind,
        "record": None
        if kind == "delete"
        else {
            "id": record_id,
            "name": "套餐",
            "amount": "59.10",
            "cycle": "monthly",
            "category": "通信",
            "subcategory": "手机",
            **changes,
        },
    }


def upload(*mutations, client_id=None, dataset="alice-data"):
    return {
        "dataset_id": dataset,
        "client_id": client_id or str(uuid4()),
        "mutations": list(mutations),
    }


def post(api, body):
    response = api.post("/v1/sync/upload", json=body)
    assert response.status_code == 200, response.text
    return response.json()["results"]


def test_authentication_and_distinct_audiences(api, authority):
    assert api.get("/.well-known/jwks.json").json() == {"keys": [authority.jwk]}
    assert not {"d", "p", "q"} & authority.jwk.keys()
    assert api.get("/v1/datasets").json()["datasets"] == [{"id": "alice-data", "name": "甲"}]
    credentials = api.post("/v1/sync/credentials").json()
    claims = jwt.decode(
        credentials["token"],
        authority.key.public_key(),
        algorithms=["RS256"],
        audience=SYNC_AUDIENCE,
        issuer=authority.issuer,
    )
    assert claims["sub"] == "alice"
    assert claims["exp"] - claims["iat"] == 300
    api.headers["Authorization"] = "Bearer " + credentials["token"]
    assert api.get("/v1/datasets").status_code == 401
    api.headers.pop("Authorization")
    assert api.post("/v1/sync/upload", json=upload(mutation())).status_code == 401


@pytest.mark.parametrize(
    "changes",
    [
        {"exp": datetime.now(UTC) - timedelta(seconds=1)},
        {"iss": "wrong"},
        {"sub": ""},
        {"aud": "wrong"},
        {"iat": datetime.now(UTC) + timedelta(minutes=1)},
        {"exp": datetime.now(UTC) + timedelta(hours=2)},
    ],
)
def test_invalid_tokens(api, authority, changes):
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": "alice",
            "iss": authority.issuer,
            "aud": API_AUDIENCE,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            **changes,
        },
        authority.key,
        algorithm="RS256",
        headers={"kid": authority.kid},
    )
    api.headers["Authorization"] = "Bearer " + token
    assert api.get("/v1/datasets").status_code == 401


@pytest.mark.parametrize("kind", ["algorithm", "unknown_key", "string_timestamps"])
def test_token_header_and_claim_types(api, authority, kind):
    now = int(datetime.now(UTC).timestamp())
    claims = {
        "sub": "alice",
        "iss": authority.issuer,
        "aud": API_AUDIENCE,
        "iat": now,
        "exp": now + 300,
    }
    if kind == "string_timestamps":
        claims.update(iat=str(now), exp=str(now + 300))
    token = jwt.encode(
        claims,
        "x" * 32 if kind == "algorithm" else authority.key,
        algorithm="HS256" if kind == "algorithm" else "RS256",
        headers={"kid": "unknown" if kind == "unknown_key" else authority.kid},
    )
    api.headers["Authorization"] = "Bearer " + token
    assert api.get("/v1/datasets").status_code == 401


def test_transaction_deadline_cannot_leak_to_pool(db):
    with user_transaction(db.runtime, "alice") as connection:
        assert connection.exec_driver_sql("SHOW transaction_timeout").scalar_one() == "20s"
    with db.runtime.connect() as connection:
        assert connection.exec_driver_sql("SHOW transaction_timeout").scalar_one() == "0"


def test_upload_retry_is_durable_and_reused_id_rejected(api, db):
    body = upload(mutation())
    first = post(api, body)
    assert first[0]["status"] == "applied" and first[0]["version"] == 1
    assert post(api, body) == first  # The first committed response may have been lost.
    changed = deepcopy(body)
    changed["mutations"][0]["record"]["name"] = "重放篡改"
    assert api.post("/v1/sync/upload", json=changed).status_code == 409
    with user_transaction(db.runtime, "alice") as c:
        row = (
            c.execute(sa.select(Expense).where(Expense.id == body["mutations"][0]["record_id"]))
            .mappings()
            .one()
        )
        assert row["row_version"] == 1 and str(row["amount"]) == "59.10"


def test_same_device_offline_chain_and_competing_device(api):
    client = str(uuid4())
    first = mutation()
    second = mutation(first["record_id"], predecessor=first["mutation_id"], name="连续编辑")
    third = mutation(first["record_id"], predecessor=second["mutation_id"], kind="delete")
    results = post(api, upload(first, second, third, client_id=client))
    assert [r["version"] for r in results] == [1, 2, 3]
    assert all(r["status"] == "applied" for r in results)
    stale = post(api, upload(mutation(first["record_id"], version=1, name="另一端旧版本")))
    assert stale[0]["status"] == "conflict"


def test_other_device_interrupts_pending_chain(api):
    client = str(uuid4())
    first = mutation()
    post(api, upload(first, client_id=client))
    post(api, upload(mutation(first["record_id"], version=1, name="其他设备")))
    second = mutation(first["record_id"], predecessor=first["mutation_id"], name="过期连续编辑")
    result = post(api, upload(second, client_id=client))[0]
    assert result["status"] == "conflict" and result["version"] == 2


def test_concurrent_writers_keep_both_versions_and_resolve(api, db):
    first = mutation()
    post(api, upload(first))
    bodies = [
        upload(mutation(first["record_id"], version=1, name=name)) for name in ["甲修改", "乙修改"]
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda body: post(api, body)[0], bodies))
    assert sorted(r["status"] for r in results) == ["applied", "conflict"]
    conflict_id = next(r["conflict_id"] for r in results if r["status"] == "conflict")
    conflicts = api.get("/v1/conflicts", params={"dataset_id": "alice-data"}).json()["conflicts"]
    conflict = next(c for c in conflicts if c["id"] == conflict_id)
    assert {conflict["candidate"]["name"], conflict["server_record"]["name"]} == {
        "甲修改",
        "乙修改",
    }
    endpoint = f"/v1/conflicts/{conflict_id}/resolve"
    assert (
        api.post(endpoint, json={"choice": "candidate", "expected_version": 1}).status_code == 409
    )
    resolved = api.post(endpoint, json={"choice": "candidate", "expected_version": 2})
    assert resolved.json() == {"status": "resolved", "version": 3}
    assert (
        api.post(endpoint, json={"choice": "candidate", "expected_version": 2}).json()
        == resolved.json()
    )
    assert api.post(endpoint, json={"choice": "server", "expected_version": 3}).status_code == 409
    with user_transaction(db.runtime, "alice") as c:
        assert (
            c.scalar(sa.select(Expense.name).where(Expense.id == first["record_id"]))
            == conflict["candidate"]["name"]
        )


def test_rejected_batch_rolls_back_all_changes_and_receipts(api, db):
    existing = mutation()
    existing_body = upload(existing)
    post(api, existing_body)
    valid = mutation()
    changed = deepcopy(existing)
    changed["record"]["name"] = "错误重放"
    body = upload(valid, changed, client_id=existing_body["client_id"])
    assert api.post("/v1/sync/upload", json=body).status_code == 409
    with user_transaction(db.runtime, "alice") as c:
        assert c.scalar(sa.select(Expense.id).where(Expense.id == valid["record_id"])) is None
        assert (
            c.scalar(
                sa.select(MutationReceipt.record_id).where(
                    MutationReceipt.record_id == valid["record_id"]
                )
            )
            is None
        )


def test_account_and_app_isolation_at_api_and_rls(api, db, authority):
    assert (
        api.post("/v1/sync/upload", json=upload(mutation(), dataset="bob-data")).status_code == 404
    )
    assert (
        api.post("/v1/sync/upload", json=upload(mutation(), dataset="other-data")).status_code
        == 404
    )
    initial = mutation()
    post(api, upload(initial))
    conflict_id = post(api, upload(mutation(initial["record_id"])))[0]["conflict_id"]
    api.headers["Authorization"] = "Bearer " + authority.issue("bob", API_AUDIENCE)
    assert api.get("/v1/conflicts", params={"dataset_id": "alice-data"}).json() == {"conflicts": []}
    assert (
        api.post(
            f"/v1/conflicts/{conflict_id}/resolve",
            json={"choice": "candidate", "expected_version": 1},
        ).status_code
        == 404
    )
    with user_transaction(db.runtime, "bob") as c:
        assert (
            c.scalar(sa.select(ExpenseConflict.id).where(ExpenseConflict.id == conflict_id)) is None
        )
        assert (
            c.scalar(
                sa.select(MutationReceipt.record_id).where(
                    MutationReceipt.record_id == initial["record_id"]
                )
            )
            is None
        )
    for table in [ExpenseConflict.__table__, MutationReceipt.__table__]:
        with pytest.raises(DBAPIError):
            with db.replication.begin() as c:
                c.execute(sa.select(table).limit(1))


@pytest.mark.parametrize(
    "field,value",
    [("amount", "0.0000001"), ("amount", "NaN"), ("amount", 59.1), ("name", ""), ("unknown", "x")],
)
def test_invalid_values_never_enter_upload_queue_server(api, field, value):
    body = upload(mutation(**{field: value}))
    assert api.post("/v1/sync/upload", json=body).status_code == 422


def test_body_limit_and_unknown_identity(api, authority):
    assert api.post("/v1/sync/upload", content=b"x" * (512 * 1024 + 1)).status_code == 413
    api.headers["Authorization"] = "Bearer " + authority.issue("unknown", API_AUDIENCE)
    assert api.post("/v1/sync/credentials").status_code == 404
