"""Validated per-record documents for payroll, allocation plans and business preferences."""

import re
from datetime import datetime
from decimal import Decimal

from .expenses import _string

DOCUMENT_SCHEMAS = {
    "payroll_records": {
        "monthKey": "month",
        "salary": "money",
        "personalConsumption": "money",
        "foodBudget": "money",
        "rentProperty": "money",
        "parentSupport": "money",
        "allocations": "allocations",
        "recordedAt": "timestamp",
        "fixedSavings": "money",
        "investmentRatio": "ratio",
        "savingsRatio": "ratio",
        "note": "note",
        "updatedAt": "nullable_timestamp",
    },
    "asset_allocations": {
        "color": "color",
        "currentAmount": "money",
        "ruleType": "rule",
        "ruleRatio": "ratio",
        "historicalHighPrice": "money",
        "addPositionSteps": "steps",
    },
    "payroll_defaults": {
        "personalConsumption": "money",
        "foodBudget": "money",
        "parentSupport": "money",
        "rentProperty": "nullable_money",
        "investmentRatio": "ratio",
        "savingsRatio": "ratio",
        "allocations": "allocations",
    },
    "allocation": {
        "categoryId": "text",
        "categoryName": "text",
        "ratio": "ratio",
        "amount": "money",
        "colorValue": "color",
    },
    "step": {"id": "identifier", "drawdownRatio": "ratio", "amount": "money"},
}
PREFERENCE_SCHEMAS = {
    "include_asset_costs": "boolean",
    "allocation_baseline": "money",
    "payroll_defaults": "payroll_defaults",
    "allocation_order": "ids",
}
DOCUMENT_ENTITIES = {"payroll_records", "asset_allocations", "business_preferences"}


def validate_document(kind: str, value):
    if kind.startswith("nullable_"):
        return value is None or validate_document(kind.removeprefix("nullable_"), value)
    if kind in ("money", "ratio"):
        limit = 6 if kind == "money" else 18
        return (
            isinstance(value, str)
            and re.fullmatch(r"(?:0|[1-9]\d{0,13})(?:\.\d{1," + str(limit) + r"})?", value)
            is not None
            and (kind != "ratio" or Decimal(value) <= 1)
        )
    if kind in ("text", "note", "identifier"):
        return (
            isinstance(value, str)
            and "\x00" not in value
            and len(value) <= (4000 if kind == "note" else 200)
            and (kind != "identifier" or bool(value.strip()))
        )
    if kind == "color":
        return type(value) is int and 0 <= value <= 4294967295
    if kind == "boolean":
        return type(value) is bool
    if kind == "rule":
        return value in ("target", "minimum", "maximum")
    if kind == "month":
        return (
            isinstance(value, str)
            and re.fullmatch(r"[0-9]{4}-(?:0[1-9]|1[0-2])", value) is not None
            and value[:4] != "0000"
        )
    if kind == "timestamp":
        if not isinstance(value, str) or len(value) > 40 or "T" not in value:
            return False
        try:
            return datetime.fromisoformat(value).tzinfo is not None
        except ValueError:
            return False
    if kind in ("steps", "allocations", "ids"):
        subtype = {"steps": "step", "allocations": "allocation", "ids": "identifier"}[kind]
        if not isinstance(value, list) or len(value) > 1000:
            return False
        if not all(validate_document(subtype, item) for item in value):
            return False
        identities = (
            [item["id"] for item in value] if kind == "steps" else value if kind == "ids" else None
        )
        return identities is None or len(set(identities)) == len(identities)
    fields = DOCUMENT_SCHEMAS.get(kind)
    return (
        isinstance(value, dict)
        and fields is not None
        and value.keys() == fields.keys()
        and all(validate_document(t, value[k]) for k, t in fields.items())
    )


def canonical_document_record(entity: str, value: dict) -> dict:
    if set(value) != {"id", "name", "payload", "sortOrder"}:
        raise ValueError("Missing or unsupported document fields")
    identifier = _string(value["id"], "id", empty=False)
    name = _string(value["name"], "name", empty=False)
    order = value["sortOrder"]
    if type(order) is not int or not 0 <= order <= 2147483647:
        raise ValueError("Invalid sortOrder")
    kind = PREFERENCE_SCHEMAS.get(name) if entity == "business_preferences" else entity
    if kind is None or not validate_document(kind, value["payload"]):
        raise ValueError("Invalid business document")
    if entity == "payroll_records" and name != value["payload"]["monthKey"]:
        raise ValueError("Payroll month mismatch")
    return {"id": identifier, "name": name, "payload": value["payload"], "sortOrder": order}
