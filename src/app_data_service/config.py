"""Explicit connections with bounded waits and no credential logging."""

import os

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url


def database_url() -> str:
    value = os.environ.get("APP_DATA_DATABASE_URL", "")
    if not value:
        raise ValueError("APP_DATA_DATABASE_URL is required")
    url = make_url(value)
    if url.drivername != "postgresql+psycopg":
        raise ValueError("Only postgresql+psycopg connections are supported")
    return value


def make_engine(url: str) -> Engine:
    parsed = make_url(url)
    if parsed.drivername != "postgresql+psycopg":
        raise ValueError("A real PostgreSQL connection is required")
    return create_engine(
        parsed,
        hide_parameters=True,
        pool_size=2,
        max_overflow=2,
        pool_timeout=5,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 5,
            "options": "-c statement_timeout=15000 -c lock_timeout=5000 -c timezone=UTC",
        },
    )
