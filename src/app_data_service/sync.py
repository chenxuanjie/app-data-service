"""Business writes for PowerSync. Replication/queues remain the framework's job."""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Connection

from .expenses import ExpenseRecord
from .models import Dataset, Expense, ExpenseConflict, MutationReceipt

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\x00]+$")]
Version = Annotated[int, Field(strict=True, ge=0, le=9223372036854775806)]


class Mutation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mutation_id: UUID
    record_id: Identifier
    base_version: Version
    predecessor: UUID | None = None
    kind: Literal["put", "delete"]
    record: dict | None = None

    @model_validator(mode="after")
    def validate_record(self):
        if self.kind == "put":
            if not isinstance(self.record, dict) or not isinstance(self.record.get("amount"), str):
                raise ValueError("Upload amounts must use decimal strings")
            record = ExpenseRecord.from_legacy_json(self.record, position=0)
            if record.id != self.record_id:
                raise ValueError("Record identity mismatch")
            # Canonical full snapshot: no rebasing against a newer server record.
            self.record = legacy_record(record)
        elif self.record is not None:
            raise ValueError("Delete must not include a replacement record")
        return self


class Upload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_id: Identifier
    client_id: UUID
    mutations: list[Mutation] = Field(min_length=1, max_length=100)

    @field_validator("mutations")
    @classmethod
    def unique_mutations(cls, values):
        if len({value.mutation_id for value in values}) != len(values):
            raise ValueError("Duplicate mutation IDs in transaction")
        return values


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    choice: Literal["server", "candidate"]
    expected_version: Version


class ScopeError(Exception):
    pass


class ReplayError(Exception):
    pass


def legacy_record(record: ExpenseRecord) -> dict:
    values = asdict(record)
    values["amount"] = format(record.amount, "f")
    for old, new in [
        ("icon_id", "iconId"),
        ("include_in_living_cost", "includeInLivingCost"),
        ("sort_order", "sortOrder"),
    ]:
        values[new] = values.pop(old)
    return values


def visible_dataset(connection: Connection, dataset_id: str) -> None:
    found = connection.scalar(
        sa.select(Dataset.id).where(Dataset.id == dataset_id, Dataset.app_id == "zheri")
    )
    if not found:
        raise ScopeError("Dataset unavailable")


def lock_dataset(connection: Connection, dataset_id: str) -> None:
    # Same lock as legacy imports: prevent a concurrent import/write race.
    connection.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 101))"), {"id": dataset_id}
    )


def current_record(connection: Connection, dataset_id: str, record_id: str):
    return (
        connection.execute(
            sa.select(Expense)
            .where(Expense.id == record_id, Expense.dataset_id == dataset_id)
            .with_for_update()
        )
        .mappings()
        .one_or_none()
    )


def snapshot(row) -> dict | None:
    if row is None:
        return None
    return {
        key: (
            format(value, "f")
            if key == "amount"
            else value.isoformat()
            if isinstance(value, datetime)
            else value
        )
        for key, value in row.items()
    }


def save_candidate(
    connection: Connection, dataset_id: str, record_id: str, candidate: dict | None, current
) -> int:
    if candidate is None:
        if current is None:
            return 0
        values = {"deleted_at": datetime.now(UTC)}
    else:
        values = ExpenseRecord.from_legacy_json(candidate, position=0).database_values(dataset_id)
        values["deleted_at"] = None
    if current is None:
        return connection.scalar(sa.insert(Expense).values(**values).returning(Expense.row_version))
    values.pop("id", None)
    values.pop("dataset_id", None)
    return connection.scalar(
        sa.update(Expense)
        .where(Expense.id == record_id, Expense.dataset_id == dataset_id)
        .values(**values)
        .returning(Expense.row_version)
    )


def apply_upload(connection: Connection, actor: str, upload: Upload) -> dict:
    visible_dataset(connection, upload.dataset_id)
    lock_dataset(connection, upload.dataset_id)
    receipts = MutationReceipt.__table__
    results = []
    for mutation in upload.mutations:
        payload = {"dataset_id": upload.dataset_id, **mutation.model_dump(mode="json")}
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        identity = [
            receipts.c.user_id == actor,
            receipts.c.client_id == str(upload.client_id),
            receipts.c.mutation_id == mutation.mutation_id,
        ]
        previous = connection.execute(sa.select(receipts).where(*identity)).mappings().one_or_none()
        if previous:
            if previous["payload_hash"] != digest:
                raise ReplayError("Mutation ID reused with different content")
            results.append(previous["result"])
            continue
        current = current_record(connection, upload.dataset_id, mutation.record_id)
        version = current["row_version"] if current else 0
        can_apply = version == mutation.base_version
        if not can_apply and mutation.predecessor:
            predecessor = (
                connection.execute(
                    sa.select(receipts).where(
                        receipts.c.user_id == actor,
                        receipts.c.client_id == str(upload.client_id),
                        receipts.c.mutation_id == mutation.predecessor,
                        receipts.c.dataset_id == upload.dataset_id,
                        receipts.c.record_id == mutation.record_id,
                    )
                )
                .mappings()
                .one_or_none()
            )
            can_apply = bool(
                predecessor
                and predecessor["result"]["status"] == "applied"
                and predecessor["result"]["version"] == version
                and predecessor["base_version"] == mutation.base_version
            )
        result = {"mutation_id": str(mutation.mutation_id)}
        if can_apply:
            version = save_candidate(
                connection, upload.dataset_id, mutation.record_id, mutation.record, current
            )
            result.update(status="applied", version=version)
        else:
            conflict_id = uuid4()
            connection.execute(
                sa.insert(ExpenseConflict).values(
                    id=conflict_id,
                    dataset_id=upload.dataset_id,
                    user_id=actor,
                    record_id=mutation.record_id,
                    candidate=mutation.record,
                    server_record=snapshot(current),
                    server_version=version,
                )
            )
            result.update(status="conflict", version=version, conflict_id=str(conflict_id))
        connection.execute(
            sa.insert(receipts).values(
                user_id=actor,
                client_id=str(upload.client_id),
                mutation_id=mutation.mutation_id,
                dataset_id=upload.dataset_id,
                record_id=mutation.record_id,
                payload_hash=digest,
                base_version=mutation.base_version,
                result=result,
            )
        )
        results.append(result)
    return {"results": results}


def resolve_conflict(connection: Connection, conflict_id: UUID, resolution: Resolution) -> dict:
    table = ExpenseConflict.__table__
    row = (
        connection.execute(sa.select(table).where(table.c.id == conflict_id))
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise ScopeError("Conflict unavailable")
    lock_dataset(connection, row["dataset_id"])
    row = (
        connection.execute(sa.select(table).where(table.c.id == conflict_id).with_for_update())
        .mappings()
        .one()
    )
    if row["resolved_at"]:
        if row["resolution"] != resolution.choice:
            raise ReplayError("Conflict already resolved with a different choice")
        return {"status": "resolved", "version": row["resolved_version"]}
    current = current_record(connection, row["dataset_id"], row["record_id"])
    version = current["row_version"] if current else 0
    if version != resolution.expected_version:
        raise ReplayError("Record changed again; review the current version")
    if resolution.choice == "candidate":
        version = save_candidate(
            connection, row["dataset_id"], row["record_id"], row["candidate"], current
        )
    connection.execute(
        sa.update(table)
        .where(table.c.id == conflict_id)
        .values(
            resolution=resolution.choice, resolved_version=version, resolved_at=datetime.now(UTC)
        )
    )
    return {"status": "resolved", "version": version}
