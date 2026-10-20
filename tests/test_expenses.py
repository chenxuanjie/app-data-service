from decimal import Decimal

import pytest

from app_data_service.expenses import ExpenseImportPlan, amount_from_json


def expense_source(**changes):
    return {
        "id": "expense-123-0",
        "name": "房租",
        "amount": "59.10",
        "cycle": "monthly",
        "category": "住房",
        "subcategory": "房租",
        **changes,
    }


def test_existing_json_mapping_preserves_id_precision_and_defaults():
    plan = ExpenseImportPlan.from_json({"expenses": [expense_source()]})
    record = plan.records[0]
    assert record.id == "expense-123-0"
    assert record.amount == Decimal("59.10")
    assert record.sort_order == 0 and record.include_in_living_cost
    assert record.icon_id is None and record.note == ""


@pytest.mark.parametrize("amount", [True, None, "NaN", "Infinity", -1, "0.0000001", "1e100000"])
def test_amount_rejects_invalid_or_lossy_values(amount):
    with pytest.raises(ValueError):
        amount_from_json(amount)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": ""},
        {"name": " "},
        {"cycle": "unknown"},
        {"sortOrder": -1},
        {"sortOrder": True},
        {"includeInLivingCost": "true"},
        {"name": "坏\x00值"},
        {"unexpected": 1},
    ],
)
def test_bad_fields_are_not_silently_coerced(changes):
    with pytest.raises(ValueError):
        ExpenseImportPlan.from_json({"expenses": [expense_source(**changes)]})


def test_missing_id_never_generates_a_replacement_id():
    source = expense_source()
    del source["id"]
    with pytest.raises(ValueError, match="IDs are never regenerated"):
        ExpenseImportPlan.from_json({"expenses": [source]})


def test_duplicate_ids_rejected_before_writing():
    with pytest.raises(ValueError, match="Duplicate"):
        ExpenseImportPlan.from_json({"expenses": [expense_source(), expense_source()]})


def test_hash_is_stable_for_key_order_and_changes_for_business_changes():
    source = expense_source()
    a = ExpenseImportPlan.from_json({"expenses": [source]})
    b = ExpenseImportPlan.from_json({"expenses": [dict(reversed(list(source.items())))]})
    c = ExpenseImportPlan.from_json({"expenses": [expense_source(amount="60")]})
    assert a.source_hash == b.source_hash != c.source_hash
