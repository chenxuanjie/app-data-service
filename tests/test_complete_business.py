from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from test_multi_entity import client as client
from test_multi_entity import upload

from app_data_service.auth import API_AUDIENCE
from app_data_service.business_documents import validate_document
from app_data_service.entities import ENTITY_MODELS, database_values
from app_data_service.models import BusinessPreference, Dataset, DatasetMember, SyncConflict, User
from app_data_service.store import user_transaction


def sample(entity):
    identifier = str(uuid4())
    allocation = {
        "categoryId": "old-category",
        "categoryName": "投资",
        "ratio": "0.333333333333333333",
        "amount": "123.450001",
        "colorValue": 4294967295,
    }
    if entity == "payroll_records":
        month = f"{1000 + uuid4().int % 8000:04d}-{1 + uuid4().int % 12:02d}"
        return {
            "id": identifier,
            "name": month,
            "sortOrder": 0,
            "payload": {
                "monthKey": month,
                "salary": "9999.123456",
                "personalConsumption": "1000",
                "foodBudget": "300",
                "rentProperty": "2000",
                "parentSupport": "1200",
                "allocations": [allocation],
                "recordedAt": "2026-10-21T12:00:00Z",
                "fixedSavings": "1000",
                "investmentRatio": "0.8",
                "savingsRatio": "0.2",
                "note": "历史快照",
                "updatedAt": None,
            },
        }
    if entity == "asset_allocations":
        return {
            "id": identifier,
            "name": "投资账户",
            "sortOrder": 0,
            "payload": {
                "color": 4294967295,
                "currentAmount": "1999.100001",
                "ruleType": "target",
                "ruleRatio": "0.333333333333333333",
                "historicalHighPrice": "100",
                "addPositionSteps": [{"id": "step-a", "drawdownRatio": "0.04", "amount": "1000"}],
            },
        }
    return {
        "id": identifier,
        "name": "allocation_baseline",
        "sortOrder": 0,
        "payload": "12345.123456",
    }


def mutation(entity, value, version=0):
    return {
        "entity_type": entity,
        "mutation_id": str(uuid4()),
        "record_id": value["id"],
        "base_version": version,
        "kind": "put",
        "record": value,
    }


@pytest.fixture
def workspace(client, db):
    actor = "full-" + uuid4().hex
    dataset = "full-data-" + uuid4().hex
    with db.migrator.begin() as c:
        c.execute(sa.insert(User).values(id=actor, display_name="完整业务测试"))
        c.execute(
            sa.insert(Dataset).values(id=dataset, owner_id=actor, app_id="zheri", name="测试账本")
        )
        c.execute(sa.insert(DatasetMember).values(dataset_id=dataset, user_id=actor))
    client.headers["Authorization"] = "Bearer " + client.authority.issue(actor, API_AUDIENCE)
    return client, actor, dataset


@pytest.mark.parametrize("entity", ["payroll_records", "asset_allocations", "business_preferences"])
def test_complete_entities_roundtrip_conflict_resolution_delete_and_isolation(
    workspace, db, entity
):
    client, actor, dataset = workspace
    value = sample(entity)
    if entity == "business_preferences":
        value.update(name="include_asset_costs", payload=True)
    first = mutation(entity, value)
    assert upload(client, first, dataset=dataset).status_code == 200
    assert (
        upload(client, first, dataset=dataset, client_id=str(uuid4())).status_code == 200
    )  # distinct client -> conflict
    with user_transaction(db.runtime, actor) as c:
        row = (
            c.execute(
                sa.select(ENTITY_MODELS[entity]).where(ENTITY_MODELS[entity].id == value["id"])
            )
            .mappings()
            .one()
        )
        assert row["payload"] == value["payload"]
        assert row["row_version"] == 1
    changed = deepcopy(value)
    changed["payload"] = (
        False
        if entity == "business_preferences"
        else {
            **value["payload"],
            ("salary" if entity == "payroll_records" else "currentAmount"): "200",
        }
    )
    result = upload(client, mutation(entity, changed), dataset=dataset).json()["results"][0]
    assert result["status"] == "conflict"
    conflict_id = result["conflict_id"]
    resolution = {"entity_type": entity, "choice": "candidate", "expected_version": 1}
    assert client.post(f"/v2/conflicts/{conflict_id}/resolve", json=resolution).status_code == 200
    assert client.post(f"/v2/conflicts/{conflict_id}/resolve", json=resolution).status_code == 200
    deletion = {**mutation(entity, changed, version=2), "kind": "delete", "record": None}
    assert upload(client, deletion, dataset=dataset).status_code == 200
    with user_transaction(db.runtime, "bob") as c:
        assert (
            c.execute(
                sa.select(ENTITY_MODELS[entity]).where(ENTITY_MODELS[entity].id == value["id"])
            ).first()
            is None
        )
    with pytest.raises(DBAPIError):
        with user_transaction(db.runtime, actor) as c:
            c.execute(
                sa.insert(ENTITY_MODELS[entity]).values(
                    **database_values(entity, sample(entity), "other-data")
                )
            )
    with pytest.raises(DBAPIError):
        with user_transaction(db.runtime, actor) as c:
            c.execute(
                sa.delete(ENTITY_MODELS[entity]).where(ENTITY_MODELS[entity].id == value["id"])
            )


@pytest.mark.parametrize("entity", ["payroll_records", "asset_allocations", "business_preferences"])
@pytest.mark.parametrize("bad", ["precision", "negative", "unknown", "numeric", "nested"])
def test_reject_invalid_documents_in_api_and_direct_postgres(workspace, db, entity, bad):
    client, actor, dataset = workspace
    value = sample(entity)
    field = "salary" if entity == "payroll_records" else "currentAmount"
    if entity == "business_preferences":
        value["payload"] = {
            "precision": "0.0000001",
            "negative": "-1",
            "unknown": {},
            "numeric": 1,
            "nested": ["0"],
        }[bad]
    elif bad == "unknown":
        value["payload"]["unknown"] = True
    elif bad == "nested":
        key = "allocations" if entity == "payroll_records" else "addPositionSteps"
        value["payload"][key][0]["amount"] = "NaN"
    else:
        value["payload"][field] = {"precision": "0.0000001", "negative": "-1", "numeric": 1}[bad]
    assert upload(client, mutation(entity, value), dataset=dataset).status_code == 422
    with pytest.raises(DBAPIError):
        with user_transaction(db.runtime, actor) as c:
            c.execute(
                sa.insert(ENTITY_MODELS[entity]).values(**database_values(entity, value, dataset))
            )


def test_personal_dataset_replay_and_cross_account_isolation(workspace, db):
    client, actor, dataset = workspace
    msg = mutation("payroll_records", sample("payroll_records"))
    client_id = str(uuid4())
    response = upload(client, msg, dataset=dataset, client_id=client_id).json()
    assert upload(client, msg, dataset=dataset, client_id=client_id).json() == response
    assert client.get("/v1/datasets").json()["datasets"][0]["kind"] == "personal"
    client.headers["Authorization"] = "Bearer " + client.authority.issue("bob", API_AUDIENCE)
    assert upload(client, msg, dataset=dataset, client_id=client_id).status_code == 404


@pytest.mark.parametrize(
    "kind,value",
    [
        ("ratio", "1.000000000000000001"),
        ("money", "1e3"),
        ("money", True),
        ("timestamp", "2026-02-30T12:00:00Z"),
        ("month", "0000-01"),
        ("ids", ["a", "a"]),
        (
            "steps",
            [
                {"id": "a", "drawdownRatio": "0.1", "amount": "1"},
                {"id": "a", "drawdownRatio": "0.2", "amount": "2"},
            ],
        ),
    ],
)
def test_recursive_contract_rejects_invalid_values_in_python_and_postgres(db, kind, value):
    assert not validate_document(kind, value)
    with db.migrator.connect() as c:
        assert (
            c.scalar(
                sa.text("SELECT zheri.valid_business_document(:kind,CAST(:value AS JSONB))"),
                {"kind": kind, "value": __import__("json").dumps(value)},
            )
            is False
        )


@pytest.mark.parametrize("name", ["allocation_baseline", "allocation_order"])
@pytest.mark.parametrize("newest_first", [False, True])
def test_automatic_preference_uses_edit_time_not_upload_order(workspace, db, name, newest_first):
    client, actor, dataset = workspace
    identifier = str(uuid4())
    now = datetime.now(UTC)

    def msg(payload, edited):
        value = {"id": identifier, "name": name, "payload": payload, "sortOrder": 0}
        return {**mutation("business_preferences", value), "edited_at": edited.isoformat()}

    old_value = "100" if name == "allocation_baseline" else ["a", "b"]
    new_value = "30000" if name == "allocation_baseline" else ["b", "a", "c"]
    old = msg(old_value, now - timedelta(hours=2))
    new = msg(new_value, now - timedelta(hours=1))
    first, second = (new, old) if newest_first else (old, new)
    assert upload(client, first, dataset=dataset).json()["results"][0]["status"] == "applied"
    client_id = str(uuid4())
    response = upload(client, second, dataset=dataset, client_id=client_id)
    assert response.status_code == 200
    assert response.json()["results"][0]["status"] == "applied"
    assert upload(client, second, dataset=dataset, client_id=client_id).json() == response.json()
    with user_transaction(db.runtime, actor) as c:
        row = (
            c.execute(sa.select(BusinessPreference).where(BusinessPreference.id == identifier))
            .mappings()
            .one()
        )
        assert row["payload"] == new_value
        assert row["edited_at"] == now - timedelta(hours=1)
        assert (
            c.scalar(
                sa.select(sa.func.count())
                .select_from(SyncConflict)
                .where(SyncConflict.record_id == identifier)
            )
            == 0
        )


def test_unknown_and_equal_preference_times_keep_current_value(workspace, db):
    client, actor, dataset = workspace
    value = sample("business_preferences")
    first = mutation("business_preferences", value)
    timestamp = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    first["edited_at"] = timestamp
    assert upload(client, first, dataset=dataset).status_code == 200
    changed = {**value, "payload": "0"}
    for edited in [None, timestamp]:
        candidate = mutation("business_preferences", changed, version=1)
        if edited is not None:
            candidate["edited_at"] = edited
        assert (
            upload(client, candidate, dataset=dataset).json()["results"][0]["status"] == "applied"
        )
    with user_transaction(db.runtime, actor) as c:
        row = (
            c.execute(sa.select(BusinessPreference).where(BusinessPreference.id == value["id"]))
            .mappings()
            .one()
        )
        assert row["payload"] == value["payload"]
        assert row["row_version"] == 1


def test_preference_edit_time_rejects_clock_errors_and_cross_account_upload(workspace, db):
    client, actor, dataset = workspace
    message = mutation("business_preferences", sample("business_preferences"))
    for edited in ["2026-10-23T12:00:00", (datetime.now(UTC) + timedelta(days=1)).isoformat()]:
        assert upload(client, {**message, "edited_at": edited}, dataset=dataset).status_code == 422
    message["edited_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    client.headers["Authorization"] = "Bearer " + client.authority.issue("bob", API_AUDIENCE)
    assert upload(client, message, dataset=dataset).status_code == 404
