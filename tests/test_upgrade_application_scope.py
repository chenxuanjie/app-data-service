"""Upgrade existing Zheri identities, rows and session ciphertext in place."""

from uuid import uuid4

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from app_data_service.cli import ROOT, migrate
from app_data_service.config import make_engine
from app_data_service.store import user_transaction


def test_upgrade_scopes_existing_sessions_without_replacing_zheri_data(db, monkeypatch):
    name = "app_data_app_upgrade_" + uuid4().hex
    engines = []
    actor, session_id = uuid4().hex * 2, uuid4()
    dataset = "test-" + actor
    try:
        with db.admin.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.exec_driver_sql(f'CREATE DATABASE "{name}" OWNER app_migrator')
            c.exec_driver_sql(f'REVOKE ALL ON DATABASE "{name}" FROM PUBLIC')
            c.exec_driver_sql(f'GRANT CONNECT ON DATABASE "{name}" TO app_runtime,app_replication')
        urls = {
            role: make_url(url).set(database=name).render_as_string(hide_password=False)
            for role, url in db.urls.items()
        }
        with monkeypatch.context() as context:
            context.setenv("APP_DATA_DATABASE_URL", urls["migrator"])
            command.upgrade(Config(str(ROOT / "alembic.ini")), "0009_preference_edit_time")
        migrator, runtime = make_engine(urls["migrator"]), make_engine(urls["runtime"])
        engines.extend([migrator, runtime])
        with migrator.begin() as c:
            c.execute(sa.text("INSERT INTO core.users VALUES(:actor,'existing')"), {"actor": actor})
            c.execute(
                sa.text(
                    "INSERT INTO core.datasets(id,app_id,owner_id,name) "
                    "VALUES(:dataset,'zheri',:actor,'existing')"
                ),
                {"dataset": dataset, "actor": actor},
            )
            c.execute(
                sa.text("INSERT INTO core.dataset_members VALUES(:dataset,:actor)"),
                {"dataset": dataset, "actor": actor},
            )
            c.execute(
                sa.text(
                    "INSERT INTO core.app_sessions(id,user_id,refresh_hash,dsm_context,expires_at) "
                    "VALUES(:id,:actor,repeat('c',64),'unchanged-cipher',now()+interval '1 day')"
                ),
                {"id": session_id, "actor": actor},
            )
            c.exec_driver_sql(
                "INSERT INTO core.browser_sessions(id_hash,expires_at) "
                "VALUES(repeat('a',64),now()+interval '10 minutes')"
            )
            before = (
                c.execute(sa.text("SELECT * FROM core.datasets WHERE id=:id"), {"id": dataset})
                .mappings()
                .one()
            )
        migrate(urls["migrator"])
        with user_transaction(runtime, actor, app_id="zheri") as c:
            assert c.scalar(sa.text("SELECT core.ensure_app_account('existing')")) == dataset
            after = (
                c.execute(sa.text("SELECT * FROM core.datasets WHERE id=:id"), {"id": dataset})
                .mappings()
                .one()
            )
            assert (after["id"], after["owner_id"], after["app_id"], after["kind"]) == (
                before["id"],
                before["owner_id"],
                before["app_id"],
                before["kind"],
            )
            session = (
                c.execute(
                    sa.text("SELECT * FROM core.app_sessions WHERE id=:id"), {"id": session_id}
                )
                .mappings()
                .one()
            )
            assert session["app_id"] == "zheri"
            assert (session["refresh_hash"], session["dsm_context"]) == (
                "c" * 64,
                "unchanged-cipher",
            )
        with user_transaction(runtime, actor, app_id="board_game_agent") as c:
            assert c.scalar(sa.text("SELECT id FROM core.datasets")) is None
            assert c.scalar(sa.text("SELECT id FROM core.app_sessions")) is None
    finally:
        for engine in engines:
            engine.dispose()
        assert name.startswith("app_data_app_upgrade_") and len(name) == 53
        with db.admin.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
