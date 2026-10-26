"""Application-scoped transactions with bounded waits and no business-model dependencies."""

from collections.abc import Iterator
from contextlib import contextmanager

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from app_data_service.applications import application_id


@contextmanager
def user_transaction(engine: Engine, user_id: str, *, app_id: str) -> Iterator[Connection]:
    """Scope an already verified user. Callers must authenticate before using this API."""
    if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 200:
        raise ValueError("Invalid authenticated user ID")
    application_id(app_id)
    with engine.begin() as connection:
        # PostgreSQL 17 bounds the whole transaction, not only individual SQL calls.
        connection.exec_driver_sql("SET LOCAL transaction_timeout = '20s'")
        connection.execute(
            sa.text(
                "SELECT set_config('app.user_id', :user_id, true), "
                "set_config('app.app_id', :app_id, true)"
            ),
            {"user_id": user_id, "app_id": app_id},
        )
        yield connection
