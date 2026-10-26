"""Business field comparison. Replication and durable retries stay in PowerSync."""

from datetime import datetime
from decimal import Decimal

from .business_documents import DOCUMENT_ENTITIES
from .entities import EntityType

_MISSING = object()
_DECIMALS = {
    "amount",
    "price",
    "residualValue",
    "targetDailyCost",
    "currentAmount",
    "salary",
    "personalConsumption",
    "foodBudget",
    "rentProperty",
    "parentSupport",
    "fixedSavings",
    "investmentRatio",
    "savingsRatio",
    "ruleRatio",
    "historicalHighPrice",
    "drawdownRatio",
    "ratio",
}


def same(left, right, key="") -> bool:
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(same(v, right[k], k) for k, v in left.items())
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            same(a, b, key) for a, b in zip(left, right, strict=True)
        )
    if key in _DECIMALS and isinstance(left, str) and isinstance(right, str):
        return Decimal(left) == Decimal(right)
    return type(left) is type(right) and left == right


def row_record(entity: EntityType, row) -> dict | None:
    """Project only editable business fields, excluding SQL audit/version columns."""
    if row is None or row["deleted_at"] is not None:
        return None
    result = {"id": row["id"], "name": row["name"], "sortOrder": row["sort_order"]}
    if entity in DOCUMENT_ENTITIES:
        return {**result, "payload": row["payload"]}
    if entity == "expense_categories":
        return {**result, "iconId": row["icon_id"]}
    if entity == "expenses":
        result.update(
            amount=format(Decimal(str(row["amount"])), "f"),
            cycle=row["cycle"],
            category=row["category"],
            subcategory=row["subcategory"],
            iconId=row["icon_id"],
            includeInLivingCost=row["include_in_living_cost"],
            note=row["note"],
        )
        if row["category_id"] is not None:
            result["categoryId"] = row["category_id"]
        return result
    return {
        **result,
        "price": format(Decimal(str(row["price"])), "f"),
        "residualValue": format(Decimal(str(row["residual_value"])), "f"),
        "targetDailyCost": format(Decimal(str(row["target_daily_cost"])), "f"),
        "purchaseDate": (
            row["purchase_date"].isoformat()
            if hasattr(row["purchase_date"], "isoformat")
            else row["purchase_date"]
        ),
        "category": row["category"],
        "icon": row["icon_name"],
        "color": row["color_value"],
        "includeInLivingCost": row["include_in_living_cost"],
        "note": row["note"],
    }


def merge_record(base: dict, candidate: dict, current: dict) -> tuple[dict, bool]:
    """Return a candidate-side resolution that already preserves disjoint server edits.

    Arrays are one field. Only sortOrder and payroll's derived modification time
    resolve concurrent differences automatically; financial values never use time.
    """
    conflict = False

    def merge(before, edited, stored, path=()):
        nonlocal conflict
        key = path[-1] if path else ""
        if same(edited, before, key):
            return stored
        if same(stored, before, key) or same(edited, stored, key):
            return edited
        if all(isinstance(v, dict) for v in (before, edited, stored)):
            result = {}
            for name in sorted(before.keys() | edited.keys() | stored.keys()):
                value = merge(
                    before.get(name, _MISSING),
                    edited.get(name, _MISSING),
                    stored.get(name, _MISSING),
                    (*path, name),
                )
                if value is not _MISSING:
                    result[name] = value
            return result
        if path == ("sortOrder",):
            return stored
        if path == ("payload", "updatedAt"):
            values = [v for v in (edited, stored) if isinstance(v, str)]
            return max(values, key=datetime.fromisoformat) if values else None
        conflict = True
        return edited

    return merge(base, candidate, current), conflict
