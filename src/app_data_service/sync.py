"""Business writes for PowerSync. Replication/queues remain the framework's job."""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import Connection

from .entities import ENTITY_MODELS, EntityType, canonical_record, database_values
from .models import Dataset, ExpenseConflict, MutationReceipt, SyncConflict
from .record_merge import merge_record, row_record, same

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\x00]+$")]
Version = Annotated[int, Field(strict=True, ge=0, le=9223372036854775806)]
AUTOMATIC_PREFERENCES = {"allocation_baseline", "allocation_order"}


class Mutation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mutation_id: UUID
    entity_type: EntityType
    record_id: Identifier
    base_version: Version
    predecessor: UUID | None = None
    kind: Literal["put", "delete"]
    record: dict | None = None
    base_record: dict | None = None
    edited_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_record(self):
        if self.kind == "put":
            if not isinstance(self.record, dict):
                raise ValueError("Put requires a record")
            self.record = canonical_record(self.entity_type, self.record)
            if self.record["id"] != self.record_id:
                raise ValueError("Record identity mismatch")
        elif self.record is not None:
            raise ValueError("Delete must not include a record")
        if self.base_record is not None:
            self.base_record = canonical_record(self.entity_type, self.base_record)
            if self.base_record["id"] != self.record_id:
                raise ValueError("Base record identity mismatch")
        if self.edited_at is not None:
            if not self.automatic_preference:
                raise ValueError("Edit time is only supported for automatic preferences")
            if self.edited_at > datetime.now(UTC) + timedelta(minutes=5):
                raise ValueError("Edit time is ahead of the server clock")
        return self

    @property
    def automatic_preference(self):
        return (
            self.entity_type == "business_preferences"
            and self.kind == "put"
            and self.record is not None
            and self.record["name"] in AUTOMATIC_PREFERENCES
        )


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
    entity_type: EntityType
    choice: Literal["server", "candidate"]
    expected_version: Version


class ScopeError(Exception):
    pass


class ReplayError(Exception):
    pass


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


def current_record(
    connection: Connection,
    dataset_id: str,
    record_id: str,
    *,
    lock: bool = True,
    entity_type: EntityType = "expenses",
):
    model = ENTITY_MODELS[entity_type]
    query = sa.select(model).where(model.id == record_id, model.dataset_id == dataset_id)
    return connection.execute(query.with_for_update() if lock else query).mappings().one_or_none()


def snapshot(row) -> dict | None:
    if row is None:
        return None
    return {
        key: (
            format(value, "f")
            if isinstance(value, Decimal)
            else value.isoformat()
            if isinstance(value, (date, datetime))
            else value
        )
        for key, value in row.items()
    }


def save_candidate(
    connection: Connection,
    dataset_id: str,
    record_id: str,
    candidate: dict | None,
    current,
    *,
    entity_type: EntityType = "expenses",
    edited_at: datetime | None = None,
) -> int:
    model = ENTITY_MODELS[entity_type]
    if candidate is None:
        if current is None:
            return 0
        values = {"deleted_at": datetime.now(UTC)}
    else:
        values = database_values(entity_type, candidate, dataset_id)
        values["deleted_at"] = None
        if entity_type == "business_preferences":
            values["edited_at"] = edited_at
    if current is None:
        return connection.scalar(sa.insert(model).values(**values).returning(model.row_version))
    values.pop("id", None)
    values.pop("dataset_id", None)
    return connection.scalar(
        sa.update(model)
        .where(model.id == record_id, model.dataset_id == dataset_id)
        .values(**values)
        .returning(model.row_version)
    )


def apply_upload(connection: Connection, actor: str, upload: Upload) -> dict:
    visible_dataset(connection, upload.dataset_id)
    lock_dataset(connection, upload.dataset_id)
    receipts = MutationReceipt.__table__
    results = []
    for mutation in upload.mutations:
        entity_type = mutation.entity_type
        payload = {"dataset_id": upload.dataset_id, **mutation.model_dump(mode="json")}
        if mutation.edited_at is None:
            payload.pop("edited_at")
        if mutation.base_record is None:
            payload.pop("base_record")
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
            accepted = {digest}
            # Existing expense receipts predate the entity discriminator. Accept
            # only the identical historical payload, retaining the durable ID.
            if entity_type == "expenses" and previous["entity_type"] == "expenses":
                historical = {key: value for key, value in payload.items() if key != "entity_type"}
                accepted.add(
                    hashlib.sha256(
                        json.dumps(historical, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                )
            if previous["payload_hash"] not in accepted:
                raise ReplayError("Mutation ID reused with different content")
            results.append(previous["result"])
            continue
        current = current_record(
            connection, upload.dataset_id, mutation.record_id, entity_type=entity_type
        )
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
                        receipts.c.entity_type == entity_type,
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
        stored = row_record(entity_type, current)
        candidate = mutation.record
        unchanged = same(candidate, stored)
        if (
            mutation.base_record is None
            and not can_apply
            and candidate is not None
            and stored is not None
            and same(
                {k: v for k, v in candidate.items() if k != "sortOrder"},
                {k: v for k, v in stored.items() if k != "sortOrder"},
            )
        ):
            candidate = stored
            unchanged = True
        if mutation.base_record is not None and not unchanged:
            if stored is None:
                # A concurrent deletion cannot be silently resurrected.
                can_apply = False
            elif candidate is None:
                # Pure ordering is not a business edit that blocks deletion.
                can_apply = same(
                    {k: v for k, v in mutation.base_record.items() if k != "sortOrder"},
                    {k: v for k, v in stored.items() if k != "sortOrder"},
                )
            else:
                proposal, conflicting = merge_record(mutation.base_record, candidate, stored)
                try:
                    candidate = canonical_record(entity_type, proposal)
                except ValueError:
                    # Two individually valid edits may violate a cross-field constraint.
                    conflicting = True
                can_apply = not conflicting
        if (
            mutation.automatic_preference
            and current is not None
            and current["deleted_at"] is not None
        ):
            can_apply = False
        if mutation.automatic_preference and (current is None or current["deleted_at"] is None):
            # Compare the edit, not the arrival. A missing/equal time retains
            # the current value; retries still use the normal durable receipt.
            newer = (
                current is None
                or mutation.edited_at is not None
                and (current["edited_at"] is None or mutation.edited_at > current["edited_at"])
            )
            if newer:
                version = save_candidate(
                    connection,
                    upload.dataset_id,
                    mutation.record_id,
                    mutation.record,
                    current,
                    entity_type=entity_type,
                    edited_at=mutation.edited_at,
                )
            result.update(status="applied", version=version)
        elif unchanged or can_apply:
            if not same(candidate, stored):
                version = save_candidate(
                    connection,
                    upload.dataset_id,
                    mutation.record_id,
                    candidate,
                    current,
                    entity_type=entity_type,
                )
            result.update(status="applied", version=version)
        else:
            conflict_id = uuid4()
            connection.execute(
                sa.insert(ExpenseConflict if entity_type == "expenses" else SyncConflict).values(
                    **({} if entity_type == "expenses" else {"entity_type": entity_type}),
                    id=conflict_id,
                    dataset_id=upload.dataset_id,
                    user_id=actor,
                    record_id=mutation.record_id,
                    candidate=candidate,
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
                entity_type=entity_type,
                payload_hash=digest,
                base_version=mutation.base_version,
                result=result,
            )
        )
        results.append(result)
    return {"results": results}


def resolve_conflict(
    connection: Connection,
    conflict_id: UUID,
    resolution: Resolution,
    *,
    entity_type: EntityType = "expenses",
) -> dict:
    table = (ExpenseConflict if entity_type == "expenses" else SyncConflict).__table__
    predicate = [table.c.id == conflict_id]
    if entity_type != "expenses":
        predicate.append(table.c.entity_type == entity_type)
    row = connection.execute(sa.select(table).where(*predicate)).mappings().one_or_none()
    if row is None:
        raise ScopeError("Conflict unavailable")
    lock_dataset(connection, row["dataset_id"])
    row = connection.execute(sa.select(table).where(*predicate).with_for_update()).mappings().one()
    if row["resolved_at"]:
        if row["resolution"] != resolution.choice:
            raise ReplayError("Conflict already resolved with a different choice")
        return {"status": "resolved", "version": row["resolved_version"]}
    current = current_record(
        connection, row["dataset_id"], row["record_id"], entity_type=entity_type
    )
    version = current["row_version"] if current else 0
    if version != resolution.expected_version:
        raise ReplayError("Record changed again; review the current version")
    if resolution.choice == "candidate":
        candidate = row["candidate"]
        baseline = row_record(entity_type, row["server_record"])
        latest = row_record(entity_type, current)
        if candidate is not None and baseline is not None and latest is not None:
            proposal, _ = merge_record(baseline, candidate, latest)
            try:
                candidate = canonical_record(entity_type, proposal)
            except ValueError as error:
                raise ReplayError("Merged resolution is no longer valid; review again") from error
        version = save_candidate(
            connection,
            row["dataset_id"],
            row["record_id"],
            candidate,
            current,
            entity_type=entity_type,
        )
    connection.execute(
        sa.update(table)
        .where(table.c.id == conflict_id)
        .values(
            resolution=resolution.choice, resolved_version=version, resolved_at=datetime.now(UTC)
        )
    )
    return {"status": "resolved", "version": version}
