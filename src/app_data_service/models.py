"""Typed business records; PowerSync's internal tables are deliberately separate."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class App(Base):
    __tablename__ = "apps"
    __table_args__ = {"schema": "core"}

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    name: Mapped[str] = mapped_column(sa.Text)


class User(Base):
    __tablename__ = "users"
    __table_args__ = {"schema": "core"}

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(sa.Text)


class Dataset(Base):
    __tablename__ = "datasets"
    __table_args__ = {"schema": "core"}

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    app_id: Mapped[str] = mapped_column(sa.ForeignKey("core.apps.id"))
    owner_id: Mapped[str] = mapped_column(sa.ForeignKey("core.users.id"))
    name: Mapped[str] = mapped_column(sa.Text)


class DatasetMember(Base):
    __tablename__ = "dataset_members"
    __table_args__ = {"schema": "core"}

    dataset_id: Mapped[str] = mapped_column(sa.ForeignKey("core.datasets.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(sa.ForeignKey("core.users.id"), primary_key=True)


class Expense(Base):
    __tablename__ = "expenses"
    __table_args__ = (
        sa.CheckConstraint("length(id) BETWEEN 1 AND 200", name="expense_id_length"),
        sa.CheckConstraint("length(btrim(name)) BETWEEN 1 AND 200", name="expense_name_length"),
        sa.CheckConstraint(
            "amount >= 0 AND amount <= 99999999999999.999999 "
            "AND scale(amount) <= 6 AND amount <> 'NaN'::numeric",
            name="expense_amount_range",
        ),
        sa.CheckConstraint(
            "cycle IN ('daily','weekly','monthly','quarterly','halfYearly','yearly')",
            name="expense_cycle",
        ),
        sa.CheckConstraint("length(category) <= 200", name="expense_category_length"),
        sa.CheckConstraint("length(subcategory) <= 200", name="expense_subcategory_length"),
        sa.CheckConstraint("icon_id IS NULL OR length(icon_id) <= 200", name="expense_icon_length"),
        sa.CheckConstraint("length(note) <= 4000", name="expense_note_length"),
        sa.CheckConstraint("sort_order BETWEEN 0 AND 2147483647", name="expense_sort_order"),
        sa.CheckConstraint("row_version > 0", name="expense_row_version"),
        sa.Index("ix_expenses_dataset_order", "dataset_id", "sort_order", "id"),
        {"schema": "zheri"},
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    dataset_id: Mapped[str] = mapped_column(sa.ForeignKey("core.datasets.id"))
    name: Mapped[str] = mapped_column(sa.Text)
    amount: Mapped[Decimal] = mapped_column(sa.Numeric)
    cycle: Mapped[str] = mapped_column(sa.Text)
    category: Mapped[str] = mapped_column(sa.Text)
    subcategory: Mapped[str] = mapped_column(sa.Text)
    icon_id: Mapped[str | None] = mapped_column(sa.Text)
    include_in_living_cost: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.true())
    note: Mapped[str] = mapped_column(sa.Text, server_default="")
    sort_order: Mapped[int] = mapped_column(sa.BigInteger)
    row_version: Mapped[int] = mapped_column(sa.BigInteger, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class ImportBatch(Base):
    __tablename__ = "import_batches"
    __table_args__ = (
        sa.UniqueConstraint("dataset_id", "source_hash", name="uq_import_dataset_hash"),
        sa.CheckConstraint("source_count >= 0", name="import_source_count"),
        sa.CheckConstraint("source_hash ~ '^[0-9a-f]{64}$'", name="import_source_hash"),
        {"schema": "core"},
    )

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    dataset_id: Mapped[str] = mapped_column(sa.ForeignKey("core.datasets.id"))
    source_hash: Mapped[str] = mapped_column(sa.Text)
    source_count: Mapped[int] = mapped_column(sa.Integer)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class ImportRecord(Base):
    __tablename__ = "import_records"
    __table_args__ = (
        sa.UniqueConstraint("batch_id", "target_id", name="uq_import_target"),
        {"schema": "core"},
    )

    batch_id: Mapped[str] = mapped_column(
        sa.ForeignKey("core.import_batches.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    target_id: Mapped[str] = mapped_column(sa.ForeignKey("zheri.expenses.id"))


class MutationReceipt(Base):
    __tablename__ = "mutation_receipts"
    __table_args__ = {"schema": "core"}

    user_id: Mapped[str] = mapped_column(sa.ForeignKey("core.users.id"), primary_key=True)
    client_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    mutation_id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True)
    dataset_id: Mapped[str] = mapped_column(sa.ForeignKey("core.datasets.id"))
    record_id: Mapped[str] = mapped_column(sa.Text)
    payload_hash: Mapped[str] = mapped_column(sa.Text)
    base_version: Mapped[int] = mapped_column(sa.BigInteger)
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )


class ExpenseConflict(Base):
    __tablename__ = "expense_conflicts"
    __table_args__ = {"schema": "zheri"}

    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True)
    dataset_id: Mapped[str] = mapped_column(sa.ForeignKey("core.datasets.id"))
    user_id: Mapped[str] = mapped_column(sa.ForeignKey("core.users.id"))
    record_id: Mapped[str] = mapped_column(sa.Text)
    candidate: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    server_record: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    server_version: Mapped[int] = mapped_column(sa.BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    resolution: Mapped[str | None] = mapped_column(sa.Text)
    resolved_version: Mapped[int | None] = mapped_column(sa.BigInteger)
    resolved_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
