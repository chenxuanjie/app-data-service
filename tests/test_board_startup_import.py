"""Startup automation and relaxed content retention use real isolated PostgreSQL."""

import json
from uuid import UUID

import pytest
import sqlalchemy as sa
from test_board_catalog import BASE, inputs, write_json
from test_board_catalog import catalog as catalog_fixture

from app_data_service.modules.board_game_agent import catalog_models as m
from app_data_service.modules.board_game_agent import catalog_startup as startup
from app_data_service.modules.board_game_agent.catalog_import import plan_library
from app_data_service.modules.board_game_agent.catalog_service import (
    import_library,
    publish_library,
)
from app_data_service.store import user_transaction


@pytest.fixture(name="catalog")
def catalog_database(db):
    return catalog_fixture.__wrapped__(db)


def test_small_bodies_raw_json_and_extra_fields_are_saved_and_scoped(db, catalog, tmp_path):
    source, store = inputs(tmp_path)
    game = source / "assets/games/first/game.json"
    original = json.loads(game.read_text(encoding="utf-8"))
    original["futureField"] = {"useful": "保留这个字段"}
    write_json(game, original)
    (source / "assets/ASSET_SPEC.md").write_text("资料说明", encoding="utf-8")
    rules = source / "assets/games/first/docs/rules.md"
    rules.write_bytes("规则正文".encode("gb18030"))
    cover = source / "assets/games/first/images/cover.png"
    cover.write_bytes(b"\x89PNG\x00binary")
    plan = plan_library(source)
    batch = import_library(db.migrator, plan, store)
    with user_transaction(db.runtime, "public", app_id="board_game_agent") as c:
        assert c.execute(sa.select(m.file_bodies)).all() == []
    with db.migrator.connect() as c:
        source_hash = c.scalar(
            sa.select(m.source_files.c.file_sha256).where(
                m.source_files.c.batch_id == UUID(batch["batch_id"]),
                m.source_files.c.path == "assets/games/first/game.json",
            )
        )
        body = (
            c.execute(sa.select(m.file_bodies).where(m.file_bodies.c.sha256 == source_hash))
            .mappings()
            .one()
        )
        assert body["body"] == game.read_bytes()
        assert body["json_body"]["futureField"] == {"useful": "保留这个字段"}
        assert (
            c.scalar(
                sa.select(sa.func.count())
                .select_from(m.source_files)
                .where(m.source_files.c.path == "assets/ASSET_SPEC.md")
            )
            == 1
        )
    publish_library(db.migrator, batch["batch_id"], store)
    with catalog(store) as client:
        text = client.get(BASE + "/resources/first-rules/text")
        assert text.status_code == 200
        assert text.json()["text"] == "规则正文"
        assert text.json()["encoding"] == "gb18030"
        assert client.get(BASE + "/resources/first-disabled/text").status_code == 404
        assert client.get(BASE + "/resources/first-past/text").status_code == 404
        assert client.get(BASE + "/resources/first-cover/text").status_code == 404
    with user_transaction(db.runtime, "public", app_id="board_game_agent") as c:
        assert c.scalar(sa.select(sa.func.count()).select_from(m.file_bodies)) == 4
    with user_transaction(db.runtime, "alice", app_id="zheri") as c:
        assert c.execute(sa.select(m.file_bodies)).all() == []


def test_library_wide_source_filename_changes_are_part_of_the_import_identity(tmp_path):
    source, _ = inputs(tmp_path)
    original = source / "assets/ASSET_SPEC.md"
    original.write_text("preserve this source file")
    before = plan_library(source).source_hash
    original.rename(source / "assets/NOTES.md")
    assert plan_library(source).source_hash != before


def test_startup_import_publishes_logs_then_skips_without_reading_source(
    db, catalog, tmp_path, monkeypatch
):
    source, store = inputs(tmp_path)
    # Exact NAS directory from the failed deployment, including the underscore.
    metadata = source / "assets/games/first/._DAV"
    metadata.mkdir()
    (metadata / "game.json.dir").write_bytes(b"WebDAV metadata directory index")
    (metadata / "game.json.pag").write_bytes(b"WebDAV metadata pages")
    messages = []

    def log(stage, **values):
        messages.append({"stage": stage, **values})

    result = startup.run_import(db.migrator, source, store, log=log)
    assert result["status"] == "completed" and result["games"] == 1
    assert {
        "stage": "warning",
        "game": "first",
        "code": "source_metadata_skipped",
        "path": "assets/games/first/._DAV",
    } in messages
    assert any(r["stage"] == "sql_file_saved" for r in messages)
    game = next(r for r in messages if r["stage"] == "game_in_database")
    assert game["title"] == "桌游first" and game["resource_records"] == 9
    assert game["sql_small_files"] == 8
    assert messages[-1]["stage"] == "completed"
    monkeypatch.setattr(
        startup, "plan_library", lambda *_a, **_k: pytest.fail("restart scanned NAS")
    )
    assert startup.run_import(db.migrator, source, store, log=log)["status"] == "already_completed"
    assert messages[-1]["stage"] == "skipped_completed"
    with db.migrator.connect() as c:
        assert c.scalar(sa.select(sa.func.count()).select_from(m.releases)) == 2
        assert (
            c.scalar(
                sa.select(sa.func.count())
                .select_from(m.source_files)
                .where(m.source_files.c.path.like("%/._DAV/%"))
            )
            == 0
        )
    assert (metadata / "game.json.pag").read_bytes() == b"WebDAV metadata pages"


def test_inline_limit_and_new_volume_are_honored_without_duplicate_publication(
    db, catalog, tmp_path
):
    source, store = inputs(tmp_path)
    # This sample body's size exceeds the configured SQL limit; its file survives.
    large = source / "assets/games/first/docs/rules.md"
    large.write_bytes(b"A" * 300)
    result = startup.run_import(
        db.migrator, source, store, sql_file_max=64, log=lambda *_a, **_k: None
    )
    with db.migrator.connect() as c:
        digest = c.scalar(
            sa.select(m.resources.c.file_sha256)
            .join(
                m.entries,
                sa.and_(
                    m.entries.c.batch_id == m.resources.c.batch_id,
                    m.entries.c.edition_id == m.resources.c.edition_id,
                ),
            )
            .where(m.entries.c.revision == result["revision"], m.resources.c.id == "first-rules")
        )
        assert (
            c.scalar(sa.select(m.file_bodies.c.sha256).where(m.file_bodies.c.sha256 == digest))
            is None
        )
        key = c.scalar(sa.select(m.files.c.storage_key).where(m.files.c.sha256 == digest))
    assert (store / key).read_bytes() == b"A" * 300
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    again = startup.run_import(
        db.migrator, source, replacement, sql_file_max=64, log=lambda *_a, **_k: None
    )
    assert again["revision"] == result["revision"]
    assert (replacement / key).read_bytes() == b"A" * 300


def test_failed_startup_preserves_data_and_redacts_connection(monkeypatch, capsys, tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    monkeypatch.setenv("APP_DATA_BOARD_LIBRARY_SOURCE", "source")
    monkeypatch.setenv("APP_DATA_BOARD_LIBRARY_STORE", str(store))
    monkeypatch.setenv("APP_DATA_DATABASE_URL", "secret-value-should-never-appear")
    result = startup.from_environment()
    assert result["board_library_import"] == "failed"
    assert "secret-value" not in capsys.readouterr().out
    assert list(store.iterdir()) == []


def test_auto_import_lock_does_not_start_a_second_scan(db, catalog, tmp_path, monkeypatch):
    source, store = inputs(tmp_path)
    with db.migrator.connect().execution_options(isolation_level="AUTOCOMMIT") as held:
        held.execute(sa.text("SELECT pg_advisory_lock(194027,15)"))
        try:
            monkeypatch.setattr(
                startup, "plan_library", lambda *_a, **_k: pytest.fail("concurrent scan")
            )
            result = startup.run_import(db.migrator, source, store, log=lambda *_a, **_k: None)
            assert result["status"] == "already_running"
            assert not (store / ".store-id").exists()
        finally:
            held.execute(sa.text("SELECT pg_advisory_unlock(194027,15)"))
