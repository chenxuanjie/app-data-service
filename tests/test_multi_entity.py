from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy.exc import DBAPIError

from app_data_service.api import create_app
from app_data_service.auth import API_AUDIENCE, TokenAuthority
from app_data_service.entities import canonical_record, database_values
from app_data_service.models import Asset, Expense, ExpenseCategory, MutationReceipt
from app_data_service.store import user_transaction


@pytest.fixture
def client(db):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    authority = TokenAuthority(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        "multi-entity-test",
    )
    with TestClient(create_app(db.runtime, authority, "http://127.0.0.1:18090")) as client:
        client.headers["Authorization"] = "Bearer " + authority.issue("alice", API_AUDIENCE)
        client.authority = authority
        yield client


def record(entity, id=None, **changes):
    common = {"id": id or str(uuid4()), "name": "测试记录"}
    return {
        **common,
        **{
            "assets": {
                "price": "6899.100001",
                "purchaseDate": "2024-02-29",
                "category": "数码",
                "residualValue": "2180",
                "targetDailyCost": "5",
                "icon": "smartphone",
                "color": 4294967295,
                "includeInLivingCost": True,
                "note": "中文备注",
            },
            "expense_categories": {"iconId": "custom"},
            "expenses": {
                "amount": "59.10",
                "cycle": "monthly",
                "category": "通信",
                "subcategory": "手机",
            },
        }[entity],
        **changes,
    }


def mutation(entity, *, version=0, predecessor=None, kind="put", **changes):
    value = record(entity, **changes)
    return {
        "mutation_id": str(uuid4()),
        "entity_type": entity,
        "record_id": value["id"],
        "base_version": version,
        "predecessor": predecessor,
        "kind": kind,
        "record": value if kind == "put" else None,
    }


def upload(client, *mutations, client_id=None, dataset="alice-data"):
    body = {
        "dataset_id": dataset,
        "client_id": client_id or str(uuid4()),
        "mutations": list(mutations),
    }
    return client.post("/v2/sync/upload", json=body)


def test_mixed_upload_exact_money_date_color_and_category_reference(client, db):
    category = mutation("expense_categories")
    asset = mutation("assets")
    expense = mutation("expenses", categoryId=category["record_id"])
    response = upload(client, category, asset, expense)
    assert response.status_code == 200
    assert [r["version"] for r in response.json()["results"]] == [1, 1, 1]
    with user_transaction(db.runtime, "alice") as c:
        row = c.execute(sa.select(Asset).where(Asset.id == asset["record_id"])).mappings().one()
        assert row["price"] == Decimal("6899.100001")
        assert row["purchase_date"] == date(2024, 2, 29)
        assert row["color_value"] == 4294967295
        assert (
            c.scalar(sa.select(Expense.category_id).where(Expense.id == expense["record_id"]))
            == category["record_id"]
        )
    assert client.get("/v1/capabilities").json() == {
        "sync_protocols": [2],
        "entities": [
            "expenses",
            "assets",
            "expense_categories",
            "payroll_records",
            "asset_allocations",
            "business_preferences",
        ],
        "max_mutations": 100,
        "record_merge": "three_way_v1",
        "automatic_preferences": {
            "allocation_baseline": "latest_edit",
            "allocation_order": "latest_edit",
        },
    }


@pytest.mark.parametrize("entity", ["assets", "expense_categories"])
def test_new_entities_conflict_keep_both_versions_and_resolve(client, db, entity):
    original = mutation(entity)
    assert upload(client, original).status_code == 200
    stale = mutation(entity, id=original["record_id"], name="离线修改")
    conflict = upload(client, stale).json()["results"][0]
    assert conflict["status"] == "conflict"
    rows = client.get("/v2/conflicts", params={"dataset_id": "alice-data"}).json()["conflicts"]
    row = next(row for row in rows if row["id"] == conflict["conflict_id"])
    assert row["candidate"]["name"] == "离线修改"
    assert row["current_record"]["name"] == "测试记录"
    assert row["entity_type"] == entity
    path = f"/v2/conflicts/{row['id']}/resolve"
    body = {"entity_type": entity, "choice": "candidate", "expected_version": 1}
    assert client.post(path, json={**body, "expected_version": 0}).status_code == 409
    assert client.post(path, json=body).json()["version"] == 2
    assert client.post(path, json=body).json()["version"] == 2
    assert client.post(path, json={**body, "choice": "server"}).status_code == 409
    deleted = mutation(entity, id=original["record_id"], version=2, kind="delete")
    assert upload(client, deleted).json()["results"][0]["version"] == 3
    with user_transaction(db.runtime, "alice") as c:
        model = Asset if entity == "assets" else ExpenseCategory
        assert (
            c.scalar(sa.select(model.deleted_at).where(model.id == original["record_id"]))
            is not None
        )


def test_receipts_do_not_replay_across_entities_or_chain_wrong_predecessors(client):
    first = mutation("expenses")
    client_id = str(uuid4())
    legacy = first
    original = upload(client, legacy, client_id=client_id)
    assert original.status_code == 200
    assert upload(client, legacy, client_id=client_id).json() == original.json()
    asset = mutation("assets", id=first["record_id"])
    asset["mutation_id"] = first["mutation_id"]
    assert upload(client, asset, client_id=client_id).status_code == 409
    asset["mutation_id"] = str(uuid4())
    assert upload(client, asset, client_id=client_id).status_code == 200
    wrong = mutation(
        "assets", id=first["record_id"], predecessor=first["mutation_id"], name="changed"
    )
    assert upload(client, wrong, client_id=client_id).json()["results"][0]["status"] == "conflict"


@pytest.mark.parametrize("entity", ["assets", "expense_categories"])
def test_rls_read_write_app_isolation_and_no_hard_delete(client, db, entity):
    row = mutation(entity)
    assert upload(client, row, dataset="bob-data").status_code == 404
    assert upload(client, row, dataset="other-data").status_code == 404
    assert upload(client, row).status_code == 200
    model = Asset if entity == "assets" else ExpenseCategory
    with user_transaction(db.runtime, "bob") as c:
        assert c.scalar(sa.select(model.id).where(model.id == row["record_id"])) is None
        assert (
            c.execute(
                sa.update(model).where(model.id == row["record_id"]).values(name="窃取")
            ).rowcount
            == 0
        )
    with pytest.raises(DBAPIError):
        with user_transaction(db.runtime, "alice") as c:
            c.execute(sa.delete(model).where(model.id == row["record_id"]))


def test_category_cannot_reference_other_dataset_and_batch_rolls_back(client, db):
    with user_transaction(db.runtime, "bob") as c:
        foreign_id = str(uuid4())
        c.execute(
            sa.insert(ExpenseCategory).values(
                id=foreign_id, dataset_id="bob-data", name="私有", icon_id="custom", sort_order=0
            )
        )
    asset, expense = mutation("assets"), mutation("expenses", categoryId=foreign_id)
    assert upload(client, asset, expense).status_code == 409
    with user_transaction(db.runtime, "alice") as c:
        assert c.scalar(sa.select(Asset.id).where(Asset.id == asset["record_id"])) is None
        assert (
            c.scalar(
                sa.select(MutationReceipt.mutation_id).where(
                    MutationReceipt.mutation_id == asset["mutation_id"]
                )
            )
            is None
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("price", 1.1),
        ("price", "NaN"),
        ("price", "0.0000001"),
        ("targetDailyCost", "0"),
        ("residualValue", "7000"),
        ("color", 4294967296),
        ("purchaseDate", "2025-02-29"),
        ("purchaseDate", "2024-02-29T10:00:00"),
        ("icon", "unknown"),
    ],
)
def test_invalid_asset_values_are_rejected_without_writes(client, field, value):
    assert upload(client, mutation("assets", **{field: value})).status_code == 422


def test_soft_deleted_category_preserves_expense_snapshot_and_legacy_conflicts(client):
    category = mutation("expense_categories", name="自定义分类")
    expense = mutation("expenses", category="自定义分类", categoryId=category["record_id"])
    assert upload(client, category, expense).status_code == 200
    deleted = mutation("expense_categories", id=category["record_id"], version=1, kind="delete")
    assert upload(client, deleted).status_code == 200
    legacy = mutation("expenses", id=expense["record_id"], name="旧客户端候选")
    conflict = upload(client, legacy).json()["results"][0]
    rows = client.get("/v2/conflicts", params={"dataset_id": "alice-data"}).json()["conflicts"]
    row = next(row for row in rows if row["id"] == conflict["conflict_id"])
    assert row["entity_type"] == "expenses"
    assert row["current_record"]["category"] == "自定义分类"
    assert (
        client.post(
            f"/v2/conflicts/{row['id']}/resolve",
            json={"entity_type": "expenses", "choice": "server", "expected_version": 1},
        ).status_code
        == 200
    )


@pytest.mark.parametrize("entity", ["assets", "expense_categories"])
@pytest.mark.parametrize("dataset", ["bob-data", "other-data"])
def test_new_table_rls_also_rejects_direct_cross_scope_inserts(db, entity, dataset):
    model = Asset if entity == "assets" else ExpenseCategory
    values = database_values(entity, canonical_record(entity, record(entity)), dataset)
    with pytest.raises(DBAPIError) as error:
        with user_transaction(db.runtime, "alice") as c:
            c.execute(sa.insert(model).values(**values))
    assert error.value.orig.sqlstate == "42501"


@pytest.mark.parametrize(
    "changes",
    [
        {"price": "NaN"},
        {"price": "0.0000001"},
        {"residual_value": "0.0000001"},
        {"target_daily_cost": "0"},
        {"color_value": 4294967296},
    ],
)
def test_asset_database_constraints_reject_bypassed_converter(client, db, changes):
    value = mutation("assets")
    assert upload(client, value).status_code == 200
    with pytest.raises(DBAPIError) as error:
        with user_transaction(db.runtime, "alice") as c:
            c.execute(sa.update(Asset).where(Asset.id == value["record_id"]).values(**changes))
    assert error.value.orig.sqlstate == "23514"
