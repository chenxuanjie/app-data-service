"""Zheri transactional import; application-independent transactions live in store."""

from dataclasses import dataclass
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy import Connection

from app_data_service.models import Dataset
from app_data_service.modules.zheri.models import Expense, ImportBatch, ImportRecord

from .expenses import ExpenseImportPlan


@dataclass(frozen=True)
class ImportResult:
    batch_id: str
    count: int
    created: bool


def import_expenses(
    connection: Connection, dataset_id: str, plan: ExpenseImportPlan
) -> ImportResult:
    """Append an audited import atomically, never replace existing business rows."""
    if not connection.in_transaction():
        raise ValueError("Import requires an explicit transaction")
    dataset = connection.execute(
        sa.select(Dataset.id).where(Dataset.id == dataset_id, Dataset.app_id == "zheri")
    ).scalar_one_or_none()
    if dataset is None:
        raise PermissionError("Dataset is unavailable to this user")
    # Serialize imports of one dataset, with the connection's finite lock_timeout.
    connection.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:dataset_id, 101))"),
        {"dataset_id": dataset_id},
    )
    batch = connection.execute(
        sa.select(ImportBatch.id, ImportBatch.source_count).where(
            ImportBatch.dataset_id == dataset_id, ImportBatch.source_hash == plan.source_hash
        )
    ).one_or_none()
    if batch is not None:
        return ImportResult(batch.id, batch.source_count, False)
    batch_id = str(uuid4())
    connection.execute(
        sa.insert(ImportBatch).values(
            id=batch_id,
            dataset_id=dataset_id,
            source_hash=plan.source_hash,
            source_count=len(plan.records),
        )
    )
    for record in plan.records:
        connection.execute(sa.insert(Expense).values(**record.database_values(dataset_id)))
        connection.execute(
            sa.insert(ImportRecord).values(
                batch_id=batch_id, source_id=record.id, target_id=record.id
            )
        )
    return ImportResult(batch_id, len(plan.records), True)
