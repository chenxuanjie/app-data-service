from copy import deepcopy
from uuid import uuid4

import pytest
import sqlalchemy as sa
from test_complete_business import sample
from test_multi_entity import client as client
from test_multi_entity import record
from test_multi_entity import upload as send_upload

from app_data_service.entities import ENTITY_MODELS, canonical_record
from app_data_service.models import Dataset, DatasetMember
from app_data_service.record_merge import row_record
from app_data_service.store import user_transaction


@pytest.fixture(autouse=True)
def isolated_dataset(client, db):
    client.merge_dataset = "merge-" + uuid4().hex
    with db.migrator.begin() as c:
        c.execute(
            sa.insert(Dataset).values(
                id=client.merge_dataset, app_id="zheri", owner_id="alice", name="merge test"
            )
        )
        c.execute(sa.insert(DatasetMember).values(dataset_id=client.merge_dataset, user_id="alice"))


def upload(client, *mutations, dataset=None, **kwargs):
    return send_upload(client, *mutations, dataset=dataset or client.merge_dataset, **kwargs)


ENTITIES = [
    "expenses",
    "assets",
    "expense_categories",
    "payroll_records",
    "asset_allocations",
    "business_preferences",
]


def value(entity):
    result = record(entity) if entity in ENTITIES[:3] else sample(entity)
    if entity == "business_preferences":
        result.update(
            name="payroll_defaults",
            payload={
                "personalConsumption": "100",
                "foodBudget": "50",
                "parentSupport": "0",
                "rentProperty": "100",
                "investmentRatio": "0.2",
                "savingsRatio": "0.3",
                "allocations": [],
            },
        )
    return canonical_record(entity, result)


def put(entity, edited, base=None, version=0, **extra):
    return {
        "entity_type": entity,
        "mutation_id": str(uuid4()),
        "record_id": edited["id"],
        "base_version": version,
        "kind": "put",
        "record": edited,
        **({"base_record": base} if base is not None else {}),
        **extra,
    }


def current(db, entity, id):
    with user_transaction(db.runtime, "alice") as c:
        row = (
            c.execute(sa.select(ENTITY_MODELS[entity]).where(ENTITY_MODELS[entity].id == id))
            .mappings()
            .one()
        )
        return row_record(entity, row), row["row_version"], row["deleted_at"]


@pytest.mark.parametrize("entity", ENTITIES)
def test_disjoint_fields_and_identical_edits_merge_without_spurious_conflicts(client, db, entity):
    base = value(entity)
    assert upload(client, put(entity, base)).json()["results"][0]["version"] == 1
    server, local = deepcopy(base), deepcopy(base)
    if entity == "business_preferences":
        server["payload"]["foodBudget"] = "60"
        local["payload"]["personalConsumption"] = "110"
    else:
        server["name"] = "server name"
        if entity == "expenses":
            local["amount"] = "60"
        elif entity == "assets":
            local["price"] = "7000"
        elif entity == "expense_categories":
            local["iconId"] = "another"
        elif entity == "payroll_records":
            server["name"] = base["name"]
            server["payload"]["note"] = "server note"
            server["payload"]["updatedAt"] = "2026-10-24T01:00:00Z"
            local["payload"]["salary"] = "10000"
            local["payload"]["updatedAt"] = "2026-10-24T02:00:00Z"
        else:
            local["payload"]["currentAmount"] = "2100"
    assert upload(client, put(entity, server, base, 1)).json()["results"][0]["status"] == "applied"
    result = upload(client, put(entity, local, base, 1))
    assert result.status_code == 200, result.text
    assert result.json()["results"][0]["status"] == "applied"
    merged, version, _ = current(db, entity, base["id"])
    assert version == 3
    if entity == "business_preferences":
        assert merged["payload"]["foodBudget"] == "60"
        assert merged["payload"]["personalConsumption"] == "110"
    elif entity == "payroll_records":
        assert merged["payload"]["note"] == "server note"
        assert merged["payload"]["salary"] == "10000"
        assert merged["payload"]["updatedAt"] == local["payload"]["updatedAt"]
    else:
        assert merged["name"] == "server name"
    # A different device submits the exact resulting content with an old version.
    duplicate = put(entity, merged, version=1)
    assert upload(client, duplicate).json()["results"][0] == {
        "mutation_id": duplicate["mutation_id"],
        "status": "applied",
        "version": version,
    }
    assert current(db, entity, base["id"])[1] == version


@pytest.mark.parametrize("entity", ENTITIES)
def test_repeated_deletes_are_idempotent_but_delete_edit_requires_choice(client, db, entity):
    base = value(entity)
    upload(client, put(entity, base))
    deletion = {**put(entity, base, base, 1), "kind": "delete", "record": None}
    assert upload(client, deletion).json()["results"][0]["version"] == 2
    repeated = {**deletion, "mutation_id": str(uuid4())}
    assert upload(client, repeated).json()["results"][0]["status"] == "applied"
    assert current(db, entity, base["id"])[1] == 2
    edited = deepcopy(base)
    if entity == "business_preferences":
        edited["payload"]["foodBudget"] = "80"
    elif entity == "payroll_records":
        edited["payload"]["salary"] = "12300"
    else:
        edited["name"] = "offline edit"
    assert upload(client, put(entity, edited, base, 1)).json()["results"][0]["status"] == "conflict"
    assert current(db, entity, base["id"])[2] is not None


def test_conflict_resolution_keeps_other_fields_and_newer_server_edits(client, db):
    entity = "assets"
    base = value(entity)
    upload(client, put(entity, base))
    server = {**base, "price": "7100", "note": "server note"}
    upload(client, put(entity, server, base, 1))
    local = {**base, "price": "7200"}
    candidate = put(entity, local, base, 1)
    client_id = str(uuid4())
    result = upload(client, candidate, client_id=client_id).json()["results"][0]
    assert result["status"] == "conflict"
    assert upload(client, candidate, client_id=client_id).json()["results"][0] == result
    conflict = next(
        e
        for e in client.get("/v2/conflicts", params={"dataset_id": client.merge_dataset}).json()[
            "conflicts"
        ]
        if e["id"] == result["conflict_id"]
    )
    assert conflict["candidate"]["note"] == "server note"
    updated = {**server, "note": "newer server note"}
    upload(client, put(entity, updated, server, 2))
    resolution = {"entity_type": entity, "choice": "candidate", "expected_version": 3}
    assert (
        client.post(f"/v2/conflicts/{result['conflict_id']}/resolve", json=resolution).status_code
        == 200
    )
    resolved, _, _ = current(db, entity, base["id"])
    assert resolved["price"] == "7200"
    assert resolved["note"] == "newer server note"


@pytest.mark.parametrize("entity", ["expenses", "assets", "expense_categories"])
def test_concurrent_order_and_content_changes_never_prompt_for_order(client, db, entity):
    base = value(entity)
    upload(client, put(entity, base))
    server = {**base, "name": "server", "sortOrder": 2}
    upload(client, put(entity, server, base, 1))
    local = {**base, "sortOrder": 5}
    assert upload(client, put(entity, local, base, 1)).json()["results"][0]["status"] == "applied"
    merged, version, _ = current(db, entity, base["id"])
    assert merged["name"] == "server"
    assert merged["sortOrder"] == 2
    assert version == 2


def test_offline_chain_undo_uses_its_own_edit_base_and_preserves_remote_change(client, db):
    base = value("expenses")
    upload(client, put("expenses", base))
    first = {**base, "amount": "80"}
    upload(client, put("expenses", first, base, 1))
    remote = {**first, "note": "remote"}
    upload(client, put("expenses", remote, first, 2))
    undone = put("expenses", base, first, 1)
    assert upload(client, undone).json()["results"][0]["status"] == "applied"
    result, _, _ = current(db, "expenses", base["id"])
    assert result["amount"] == "59.10"
    assert result["note"] == "remote"


def test_invalid_combined_financial_values_and_missing_base_remain_conflicts(client):
    base = value("assets")
    upload(client, put("assets", base))
    upload(client, put("assets", {**base, "price": "4000"}, base, 1))
    # Each edit is valid on its own; combining would make residual > price.
    local = {**base, "residualValue": "5000"}
    assert (
        upload(client, put("assets", local, base, 1)).json()["results"][0]["status"] == "conflict"
    )
    assert (
        upload(client, put("assets", {**base, "note": "unknown base"}, version=1)).json()[
            "results"
        ][0]["status"]
        == "conflict"
    )


def test_base_validation_and_replay_identity_include_the_baseline(client):
    base = value("assets")
    upload(client, put("assets", base))
    candidate = put("assets", {**base, "note": "edit"}, base, 1)
    device = str(uuid4())
    assert upload(client, candidate, client_id=device).status_code == 200
    changed = {**candidate, "base_record": {**base, "note": "different baseline"}}
    assert upload(client, changed, client_id=device).status_code == 409
    wrong = {**candidate, "mutation_id": str(uuid4()), "base_record": {**base, "id": "other"}}
    assert upload(client, wrong).status_code == 422
    assert upload(client, candidate, dataset="bob-data").status_code == 404


def test_same_amount_with_different_decimal_text_and_another_field_is_not_a_conflict(client, db):
    base = value("expenses")
    upload(client, put("expenses", base))
    upload(client, put("expenses", {**base, "amount": "60.00"}, base, 1))
    result = upload(client, put("expenses", {**base, "amount": "60", "note": "computer"}, base, 1))
    assert result.json()["results"][0]["status"] == "applied"
    assert current(db, "expenses", base["id"])[0]["note"] == "computer"


def test_delete_cannot_discard_a_concurrent_business_edit(client, db):
    base = value("expenses")
    upload(client, put("expenses", base))
    upload(client, put("expenses", {**base, "amount": "99"}, base, 1))
    deletion = {**put("expenses", base, base, 1), "kind": "delete", "record": None}
    assert upload(client, deletion).json()["results"][0]["status"] == "conflict"
    assert current(db, "expenses", base["id"])[2] is None


def test_order_only_candidate_with_no_reliable_baseline_keeps_server_order(client, db):
    base = value("assets")
    upload(client, put("assets", base))
    changed = put("assets", {**base, "sortOrder": 7}, version=0)
    result = upload(client, changed).json()["results"][0]
    assert result["status"] == "applied"
    assert result["version"] == 1
    assert current(db, "assets", base["id"])[0]["sortOrder"] == base["sortOrder"]


def test_even_automatic_preference_cannot_resurrect_a_concurrent_deletion(client, db):
    base = canonical_record("business_preferences", sample("business_preferences"))
    upload(client, put("business_preferences", base))
    deletion = {**put("business_preferences", base, base, 1), "kind": "delete", "record": None}
    assert upload(client, deletion).json()["results"][0]["status"] == "applied"
    changed = put("business_preferences", {**base, "payload": "30000"}, base, 1)
    assert upload(client, changed).json()["results"][0]["status"] == "conflict"
