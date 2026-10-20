"""Use an isolated real PostgreSQL database; never reset app_data or skip silently."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app_data_service.cli import local_config, migrate
from app_data_service.config import make_engine
from app_data_service.models import App, Dataset, DatasetMember, User


@pytest.fixture(scope="session")
def db():
    urls = local_config()["urls"]
    database_name = f"app_data_test_{uuid4().hex}"
    assert database_name.startswith("app_data_test_") and len(database_name) == 46
    admin = make_engine(urls["admin"])
    engines = []
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}" OWNER app_migrator')
            connection.exec_driver_sql(f'REVOKE ALL ON DATABASE "{database_name}" FROM PUBLIC')
            connection.exec_driver_sql(
                f'GRANT CONNECT ON DATABASE "{database_name}" TO app_runtime, app_replication'
            )
        test_urls = {
            role: make_url(url).set(database=database_name).render_as_string(hide_password=False)
            for role, url in urls.items()
        }
        migrate(test_urls["migrator"])
        by_role = {role: make_engine(url) for role, url in test_urls.items()}
        engines.extend(by_role.values())
        with by_role["migrator"].begin() as connection:
            connection.execute(sa.insert(App).values(id="other", name="其他测试应用"))
            connection.execute(
                sa.insert(User),
                [{"id": "alice", "display_name": "甲"}, {"id": "bob", "display_name": "乙"}],
            )
            connection.execute(
                sa.insert(Dataset),
                [
                    {"id": "alice-data", "app_id": "zheri", "owner_id": "alice", "name": "甲"},
                    {"id": "bob-data", "app_id": "zheri", "owner_id": "bob", "name": "乙"},
                    {"id": "other-data", "app_id": "other", "owner_id": "alice", "name": "其他"},
                ],
            )
            connection.execute(
                sa.insert(DatasetMember),
                [
                    {"dataset_id": "alice-data", "user_id": "alice"},
                    {"dataset_id": "bob-data", "user_id": "bob"},
                    {"dataset_id": "other-data", "user_id": "alice"},
                ],
            )
        yield SimpleNamespace(**by_role, urls=test_urls)
    finally:
        for engine in engines:
            engine.dispose()
        # The exact database name is generated above, never taken from user input.
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database_name}" WITH (FORCE)')
        admin.dispose()
