"""Notifications share publication transactions and expose only app-scoped watermarks."""

import runpy
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import DBAPIError
from test_board_catalog import catalog as catalog_fixture
from test_board_catalog import inputs
from test_public_releases import BASE, DATA, DIGEST, headers, publication, publish
from test_public_releases import service as service_fixture

from app_data_service import publication_models as notices
from app_data_service import release_models
from app_data_service.modules.board_game_agent.catalog_import import plan_library
from app_data_service.modules.board_game_agent.catalog_service import (
    import_library,
    publish_library,
)
from app_data_service.modules.registry import installed_applications
from app_data_service.public_releases import transaction
from app_data_service.store import user_transaction


@pytest.fixture(name="service")
def release_service(db, tmp_path, monkeypatch):
    yield from service_fixture.__wrapped__(db, tmp_path, monkeypatch)


@pytest.fixture(name="catalog")
def library_service(db):
    return catalog_fixture.__wrapped__(db)


def rows(db, app_id="board_game_agent"):
    with user_transaction(db.runtime, "anonymous", app_id=app_id) as c:
        return c.execute(sa.select(notices.versions)).mappings().all()


def test_release_upload_does_not_notify_and_publish_is_atomic_idempotent(service, db):
    client, _ = service
    before = [r for r in rows(db) if r["topic"] == "release"]
    assert before == []
    assert (
        client.put(BASE + "/objects/" + DIGEST, content=DATA, headers=headers()).status_code == 200
    )
    assert not [r for r in rows(db) if r["topic"] == "release"]
    publish(client)
    result = [r for r in rows(db) if r["topic"] == "release"]
    assert len(result) == 1
    assert dict(result[0]) == {
        "id": "board_game_agent:release:stable:android:arm64",
        "app_id": "board_game_agent",
        "topic": "release",
        "revision": 9001,
        "channel": "stable",
        "platform": "android",
        "architecture": "arm64",
    }
    publish(client)
    assert [r for r in rows(db) if r["topic"] == "release"] == result
    with pytest.raises(RuntimeError, match="rollback"):
        with transaction(db.runtime, "board_game_agent", publishing=True) as c:
            previous = dict(c.execute(sa.select(release_models.releases)).mappings().one())
            c.execute(
                release_models.releases.insert().values(**(previous | {"build_number": 9002}))
            )
            assert (
                c.scalar(
                    sa.select(notices.versions.c.revision).where(
                        notices.versions.c.topic == "release"
                    )
                )
                == 9002
            )
            raise RuntimeError("rollback")
    assert [r["revision"] for r in rows(db) if r["topic"] == "release"] == [9001]
    publish(client, publication(9002))
    assert [r["revision"] for r in rows(db) if r["topic"] == "release"] == [9002]


def test_watermarks_cannot_be_written_or_cross_application_read(service, db):
    client, _ = service
    publish(client)
    with transaction(db.runtime, "board_game_agent") as c:
        other = dict(c.execute(sa.select(release_models.releases)).mappings().one())
    # The platform mechanism works for another app without changing its module.
    with transaction(db.runtime, "zheri", publishing=True) as c:
        c.execute(
            release_models.blobs.insert().values(app_id="zheri", sha256=DIGEST, size=len(DATA))
        )
        c.execute(release_models.releases.insert().values(**(other | {"app_id": "zheri"})))
    assert {r["app_id"] for r in rows(db, "zheri")} == {"zheri"}
    assert {r["app_id"] for r in rows(db)} == {"board_game_agent"}
    assert not [r for r in rows(db, "zheri") if r["app_id"] == "board_game_agent"]
    for operation in (
        notices.versions.insert().values(
            id="forged",
            app_id="board_game_agent",
            topic="catalog",
            revision=999,
            channel="",
            platform="",
            architecture="",
        ),
        notices.versions.update().values(revision=999),
        notices.versions.delete(),
    ):
        with pytest.raises(DBAPIError):
            with user_transaction(db.runtime, "anonymous", app_id="board_game_agent") as c:
                c.execute(operation)
    config = installed_applications().sync_config()
    stream = config["streams"]["public_publication_versions"]
    assert "app_id=auth.parameter('app_id')" in stream["query"]
    assert "auth.parameter('sync_scope')='public'" in stream["query"]
    assert "core.publication_versions" in installed_applications().publication_tables


def test_catalog_draft_never_notifies_and_publication_advances_once(catalog, db, tmp_path):
    source, store = inputs(tmp_path)
    batch = import_library(db.migrator, plan_library(source), store)
    assert [r["revision"] for r in rows(db) if r["topic"] == "catalog"] == [0]
    result = publish_library(db.migrator, batch["batch_id"], store)
    assert [r["revision"] for r in rows(db) if r["topic"] == "catalog"] == [result["revision"]]
    publish_library(db.migrator, batch["batch_id"], store)
    assert [r["revision"] for r in rows(db) if r["topic"] == "catalog"] == [result["revision"]]


@pytest.mark.parametrize("architecture", ["arm64", "any"])
def test_upgrade_backfills_existing_releases_and_restores_force_rls(service, db, architecture):
    client, _ = service
    body = publication()
    body["artifacts"][0]["architecture"] = architecture
    publish(client, body)
    assert {r["architecture"] for r in rows(db) if r["topic"] == "release"} == {architecture}
    assert db.runtime.url.database.startswith("app_data_test_")
    # Exercise 0016 -> 0017 with actual existing publications. All DDL is inside
    # a rolled-back transaction in conftest's disposable PostgreSQL database.
    with db.migrator.connect() as c:
        tx = c.begin()
        try:
            before = c.execute(sa.select(notices.versions).order_by(notices.versions.c.id)).all()
            c.exec_driver_sql("""
                DROP TRIGGER release_publication_version ON core.public_releases;
                DROP TRIGGER catalog_publication_version ON board_game_agent.library_head;
                DROP FUNCTION core.release_publication_version();
                DROP FUNCTION board_game_agent.catalog_publication_version();
                DROP TABLE core.publication_versions;
            """)
            migration = (
                Path(__file__).parents[1] / "migrations/versions/0017_publication_versions.py"
            )
            with Operations.context(MigrationContext.configure(c)):
                runpy.run_path(str(migration))["upgrade"]()
            assert (
                c.execute(sa.select(notices.versions).order_by(notices.versions.c.id)).all()
                == before
            )
            assert c.exec_driver_sql(
                "SELECT relforcerowsecurity FROM pg_class "
                "WHERE oid='core.public_releases'::regclass"
            ).scalar_one()
        finally:
            tx.rollback()
