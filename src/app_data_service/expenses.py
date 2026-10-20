"""Strict, auditable conversion of Zheri's existing expense JSON."""

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation

CYCLES = frozenset(("daily", "weekly", "monthly", "quarterly", "halfYearly", "yearly"))
MAX_AMOUNT = Decimal("99999999999999.999999")


def _string(value: object, label: str, *, maximum: int = 200, empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(f"Invalid {label}")
    if "\x00" in value:
        raise ValueError(f"Invalid {label}: null character")
    return value


def amount_from_json(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError("amount must be a decimal number")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Invalid decimal amount") from error
    if not amount.is_finite() or not Decimal(0) <= amount <= MAX_AMOUNT:
        raise ValueError("amount must be finite and within the supported nonnegative range")
    # Do not quantize or normalize away source precision silently.
    if amount.as_tuple().exponent < -6:
        raise ValueError("amount has more than six fractional digits; explicit review required")
    return amount


@dataclass(frozen=True)
class ExpenseRecord:
    id: str
    name: str
    amount: Decimal
    cycle: str
    category: str
    subcategory: str
    icon_id: str | None
    include_in_living_cost: bool
    note: str
    sort_order: int

    @classmethod
    def from_legacy_json(cls, value: object, *, position: int) -> "ExpenseRecord":
        if not isinstance(value, dict):
            raise ValueError("Each expense must be an object")
        required = {"id", "name", "amount", "cycle", "category", "subcategory"}
        if not required <= value.keys():
            raise ValueError(
                "Expense is missing a required field; source IDs are never regenerated"
            )
        allowed = required | {"iconId", "includeInLivingCost", "note", "sortOrder"}
        if value.keys() - allowed:
            raise ValueError("Expense contains unsupported fields; explicit mapping required")
        cycle = value["cycle"]
        if not isinstance(cycle, str) or cycle not in CYCLES:
            raise ValueError("Unsupported billing cycle")
        include = value.get("includeInLivingCost", True)
        if not isinstance(include, bool):
            raise ValueError("includeInLivingCost must be a boolean")
        order = value.get("sortOrder", position)
        if isinstance(order, bool) or not isinstance(order, int) or not 0 <= order <= 2147483647:
            raise ValueError("sortOrder must be a nonnegative 32-bit integer")
        icon = value.get("iconId")
        return cls(
            id=_string(value["id"], "id", empty=False),
            name=_string(value["name"], "name", empty=False),
            amount=amount_from_json(value["amount"]),
            cycle=cycle,
            category=_string(value["category"], "category"),
            subcategory=_string(value["subcategory"], "subcategory"),
            icon_id=None if icon is None else _string(icon, "iconId"),
            include_in_living_cost=include,
            note=_string(value.get("note", ""), "note", maximum=4000),
            sort_order=order,
        )

    def database_values(self, dataset_id: str) -> dict:
        return {**asdict(self), "dataset_id": dataset_id}


@dataclass(frozen=True)
class ExpenseImportPlan:
    records: tuple[ExpenseRecord, ...]
    source_hash: str

    @classmethod
    def from_json(cls, source: object) -> "ExpenseImportPlan":
        if not isinstance(source, dict) or not isinstance(source.get("expenses"), list):
            raise ValueError("Expected an object with an expenses array")
        if len(source["expenses"]) > 10000:
            raise ValueError("At most 10000 expenses may be imported per batch")
        records = tuple(
            ExpenseRecord.from_legacy_json(value, position=index)
            for index, value in enumerate(source["expenses"])
        )
        if len({record.id for record in records}) != len(records):
            raise ValueError("Duplicate source expense IDs")
        canonical = [
            {**asdict(record), "amount": format(record.amount, "f")}
            for record in sorted(records, key=lambda item: item.id)
        ]
        payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return cls(records, hashlib.sha256(payload.encode("utf-8")).hexdigest())
