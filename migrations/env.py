"""Run migrations inside a single advisory-locked transaction."""

from alembic import context
from sqlalchemy import text

from app_data_service.config import database_url, make_engine
from app_data_service.models import Base
from app_data_service.modules.registry import installed_applications

# Load installed modules' model metadata before migration inspection.
installed_applications()

if context.is_offline_mode():
    raise RuntimeError("Offline SQL generation is not enabled; use the PostgreSQL migration runner")

engine = make_engine(database_url())
try:
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(194027, 1)"))
        connection.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS core")
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            version_table_schema="core",
            include_schemas=True,
            compare_type=True,
        )
        context.run_migrations()
finally:
    engine.dispose()
