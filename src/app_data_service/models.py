"""Shared account, application, session and data-space models."""

from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class App(Base):
    __tablename__ = "apps"
    __table_args__ = {"schema": "core"}

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    name: Mapped[str] = mapped_column(sa.Text)
    enabled: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.true())
    schema_name: Mapped[str] = mapped_column(sa.Text)
    personal_data: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.true())
    personal_space_name: Mapped[str] = mapped_column(sa.Text, server_default="个人数据")
    personal_id_prefix: Mapped[str | None] = mapped_column(sa.Text)


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
    kind: Mapped[str] = mapped_column(sa.Text, server_default="personal")


class DatasetMember(Base):
    __tablename__ = "dataset_members"
    __table_args__ = {"schema": "core"}

    dataset_id: Mapped[str] = mapped_column(sa.ForeignKey("core.datasets.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(sa.ForeignKey("core.users.id"), primary_key=True)


class AppSession(Base):
    __tablename__ = "app_sessions"
    __table_args__ = {"schema": "core"}

    id: Mapped[UUID] = mapped_column(sa.Uuid, primary_key=True)
    user_id: Mapped[str] = mapped_column(sa.ForeignKey("core.users.id"))
    app_id: Mapped[str] = mapped_column(sa.ForeignKey("core.apps.id"))
    refresh_hash: Mapped[str] = mapped_column(sa.Text)
    dsm_context: Mapped[str] = mapped_column(sa.Text)
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
