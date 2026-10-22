"""Strict v2 JSON mappings; money stays decimal text and purchase dates are days."""

import re
from datetime import date
from typing import Literal

from .business_documents import DOCUMENT_ENTITIES, canonical_document_record
from .expenses import ExpenseRecord, _string, amount_from_json, legacy_record
from .models import (
    Asset,
    AssetAllocation,
    BusinessPreference,
    Expense,
    ExpenseCategory,
    PayrollRecord,
)

EntityType = Literal[
    "expenses",
    "assets",
    "expense_categories",
    "payroll_records",
    "asset_allocations",
    "business_preferences",
]
ENTITY_MODELS = {
    "expenses": Expense,
    "assets": Asset,
    "expense_categories": ExpenseCategory,
    "payroll_records": PayrollRecord,
    "asset_allocations": AssetAllocation,
    "business_preferences": BusinessPreference,
}


def canonical_record(entity_type: EntityType, value: dict) -> dict:
    if entity_type in DOCUMENT_ENTITIES:
        return canonical_document_record(entity_type, value)
    if entity_type == "expenses":
        if not isinstance(value.get("amount"), str):
            raise ValueError("Upload money must use decimal strings")
        category_id = value.get("categoryId")
        if category_id is not None:
            _string(category_id, "categoryId", empty=False)
        legacy = {key: v for key, v in value.items() if key != "categoryId"}
        result = legacy_record(ExpenseRecord.from_legacy_json(legacy, position=0))
        if "categoryId" in value:
            result["categoryId"] = category_id
        return result
    common = {"id", "name"}
    fields = common | (
        {"iconId"}
        if entity_type == "expense_categories"
        else {
            "price",
            "purchaseDate",
            "category",
            "residualValue",
            "targetDailyCost",
            "icon",
            "color",
        }
    )
    optional = {"sortOrder"} | (
        {"note", "includeInLivingCost"} if entity_type == "assets" else set()
    )
    if not fields <= value.keys() or value.keys() - fields - optional:
        raise ValueError("Missing or unsupported business fields")
    result = {key: _string(value[key], key, empty=False) for key in common}
    order = value.get("sortOrder", 0)
    if type(order) is not int or not 0 <= order <= 2147483647:
        raise ValueError("Invalid sortOrder")
    result["sortOrder"] = order
    if entity_type == "expense_categories":
        result["iconId"] = _string(value["iconId"], "iconId", empty=False)
        return result
    for field in ("price", "residualValue", "targetDailyCost"):
        if not isinstance(value[field], str):
            raise ValueError("Upload money must use decimal strings")
        result[field] = format(amount_from_json(value[field]), "f")
    if (
        amount_from_json(result["residualValue"]) > amount_from_json(result["price"])
        or amount_from_json(result["targetDailyCost"]) <= 0
    ):
        raise ValueError("Invalid residual value or target daily cost")
    day = value["purchaseDate"]
    if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("purchaseDate must be YYYY-MM-DD")
    result["purchaseDate"] = date.fromisoformat(day).isoformat()
    if value["icon"] not in ("inventory", "smartphone", "headphones", "park"):
        raise ValueError("Unknown asset icon")
    if type(value["color"]) is not int or not 0 <= value["color"] <= 4294967295:
        raise ValueError("Invalid ARGB color")
    include = value.get("includeInLivingCost", False)
    if type(include) is not bool:
        raise ValueError("Invalid includeInLivingCost")
    result.update(
        icon=value["icon"],
        color=value["color"],
        includeInLivingCost=include,
        category=_string(value["category"], "category"),
        note=_string(value.get("note", ""), "note", maximum=4000),
    )
    return result


def database_values(entity_type: EntityType, value: dict, dataset_id: str) -> dict:
    if entity_type in DOCUMENT_ENTITIES:
        return {
            "id": value["id"],
            "dataset_id": dataset_id,
            "name": value["name"],
            "payload": value["payload"],
            "sort_order": value["sortOrder"],
        }
    if entity_type == "expenses":
        result = ExpenseRecord.from_legacy_json(
            {k: v for k, v in value.items() if k != "categoryId"}, position=0
        ).database_values(dataset_id)
        if "categoryId" in value:
            result["category_id"] = value["categoryId"]
        return result
    names = {
        "sortOrder": "sort_order",
        "iconId": "icon_id",
        "purchaseDate": "purchase_date",
        "residualValue": "residual_value",
        "targetDailyCost": "target_daily_cost",
        "icon": "icon_name",
        "color": "color_value",
        "includeInLivingCost": "include_in_living_cost",
    }
    result = {names.get(k, k): v for k, v in value.items()}
    if entity_type == "assets":
        result["purchase_date"] = date.fromisoformat(value["purchaseDate"])
        for field in ("price", "residual_value", "target_daily_cost"):
            result[field] = amount_from_json(result[field])
    return {**result, "dataset_id": dataset_id}
