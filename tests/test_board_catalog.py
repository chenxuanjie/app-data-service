"""Catalog acceptance uses isolated real PostgreSQL plus owned temporary source/store files."""

import json
from pathlib import Path
from uuid import UUID

import pytest
import sqlalchemy as sa
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app_data_service.api import create_app
from app_data_service.auth import TokenAuthority
from app_data_service.modules.board_game_agent import catalog_import
from app_data_service.modules.board_game_agent import catalog_models as m
from app_data_service.modules.board_game_agent.catalog_import import plan_library
from app_data_service.modules.board_game_agent.catalog_routes import _snapshot
from app_data_service.modules.board_game_agent.catalog_service import (
    import_library,
    publish_library,
)
from app_data_service.store import user_transaction

BASE = "/v1/apps/board_game_agent/catalog"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def inputs(tmp_path, slugs=("first",)):
    source, store = tmp_path / "source", tmp_path / "store"
    source.mkdir()
    store.mkdir()
    write_json(
        source / "assets/catalog.json",
        {
            "version": 1,
            "games": [
                {"slug": slug, "order": index, "enabled": True, "browseCategories": ["strategy"]}
                for index, slug in enumerate(slugs)
            ],
        },
    )
    for slug in slugs:
        root = source / "assets/games" / slug
        write_json(
            root / "game.json",
            {
                "id": slug,
                "slug": slug,
                "editionLabel": {"en": "First edition", "zhHans": "第一版"},
                "aliases": {"en": ["search-alias"], "zhHans": ["别名"]},
                "supportedPlayers": [2, 3],
                "recommendedPlayer": 2,
                "score": "8.2",
                "coverAsset": "images/cover.png",
                "documents": {"knowledge": {"en": "docs/rules.md"}},
                "locales": {
                    "en": {"title": slug, "summary": "A game"},
                    "zhHans": {"title": "桌游" + slug, "summary": "简介"},
                },
            },
        )
        resources = [
            {
                "id": slug + "-rules",
                "path": "docs/rules.md",
                "documentType": "rulebook",
                "aiEnabled": True,
            },
            {"id": slug + "-pdf", "path": "docs/rules.pdf", "documentType": "rulebook"},
            {
                "id": slug + "-cover",
                "path": "images/cover.png",
                "documentType": "cover",
                "width": 100,
                "height": 200,
                "variant": "front",
            },
            {
                "id": slug + "-disabled",
                "path": "docs/disabled.md",
                "enabled": False,
                "derivedFrom": [slug + "-pdf", "unresolved-source"],
            },
            {"id": slug + "-missing", "path": "docs/missing.pdf", "status": "missing"},
            {"id": slug + "-past", "path": "docs/others/past.md", "status": "superseded"},
            {"id": slug + "-zip", "path": "docs/presskit.zip", "documentType": "other"},
            {"id": slug + "-html", "path": "docs/faq.html", "documentType": "faq"},
        ]
        resources = [
            {"enabled": True, "aiEnabled": False, "status": "available", **r} for r in resources
        ]
        write_json(
            root / "manifest.json",
            {
                "schemaVersion": 1,
                "game": {"id": slug, "slug": slug, "edition": "2026", "title": {"en": slug}},
                "resources": resources,
                "validation": {
                    "checkedAt": "old-unparsed-date",
                    "checkedAgainst": "old-source",
                    "issues": ["historical issue"],
                },
            },
        )
        for record in resources:
            if record["status"] == "missing":
                continue
            path = root / record["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"%PDF-fixture" if path.suffix == ".pdf" else record["id"].encode())
        (root / "docs/unregistered.txt").write_text("retained archive")
    return source, store


@pytest.fixture
def catalog(db):
    # This cleanup is allowed only in conftest's newly generated test database.
    assert make_url(db.urls["migrator"]).database.startswith("app_data_test_")
    tables = [
        t
        for t in m.Base.metadata.tables.values()
        if t.schema == m.SCHEMA and t.name.startswith("library_")
    ]
    with db.migrator.begin() as c:
        c.exec_driver_sql("TRUNCATE " + ",".join(t.fullname for t in tables) + " RESTART IDENTITY")
        c.execute(m.releases.insert().values(revision=0))
        c.execute(m.head.insert().values(id=1, revision=0))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )

    def client(store):
        return TestClient(
            create_app(
                db.runtime,
                TokenAuthority(key, "http://testserver"),
                "http://testserver/powersync",
                application_settings={
                    "board_game_agent": {"public_file_roots": {"library_store": str(store)}}
                },
            )
        )

    return client


def test_plan_preserves_history_and_extra_fields(tmp_path):
    source, store = inputs(tmp_path)
    plan = plan_library(source)
    assert not plan.errors and plan.source_hash == plan_library(source).source_hash
    assert len(plan.rows[m.resources.name]) == 9
    resources = {r["id"]: r for r in plan.rows[m.resources.name]}
    assert resources["first-cover"]["image_role"] == "cover"
    assert resources["first-cover"]["legacy_document_type"] == "cover"
    assert resources["first-cover"]["width"] == 100
    assert resources["first-missing"]["observed_file"] == "absent"
    assert resources["first-zip"]["visibility"] == "archive"
    assert not resources["first-disabled"]["enabled"]
    assert plan.rows[m.validations.name][0]["issues"] == ["historical issue"]
    assert plan.rows[m.derivations.name][1]["source_reference"] == "unresolved-source"
    assert list(store.iterdir()) == []
    game = source / "assets/games/first/game.json"
    write_json(
        game, {**json.loads(game.read_text(encoding="utf-8")), "unknownFutureField": "retain"}
    )
    extended = plan_library(source)
    assert not extended.errors
    assert any("unknownFutureField" in w.get("fields", []) for w in extended.warnings)
    assert (
        next(s for s in extended.sources if s["path"].endswith("game.json"))["snapshot"][
            "unknownFutureField"
        ]
        == "retain"
    )
    assert list(store.iterdir()) == []


def test_nas_metadata_is_pruned_without_reading_or_changing_the_import(tmp_path, monkeypatch):
    source, store = inputs(tmp_path)
    baseline = plan_library(source)
    game = source / "assets/games/first"
    metadata = [
        game / "._DAV",
        game / "docs/._DAV",
        game / ".DAV",
        game / "docs/.DAV",
        game / "images/@eaDir",
    ]
    for directory in metadata:
        directory.mkdir(parents=True)
        (directory / "game.json.dir").write_bytes(b"")
        (directory / "game.json.pag").write_bytes(b"not a game resource")

    scandir = catalog_import.os.scandir

    def guarded_scandir(path):
        assert Path(path) not in metadata, "metadata directory was opened"
        return scandir(path)

    monkeypatch.setattr(catalog_import.os, "scandir", guarded_scandir)
    plan = plan_library(source)
    assert not plan.errors
    assert plan.source_hash == baseline.source_hash
    assert plan.rows == baseline.rows and plan.files == baseline.files
    assert {w["path"] for w in plan.warnings if w["code"] == "source_metadata_skipped"} == {
        p.relative_to(source).as_posix() for p in metadata
    }
    assert (game / "docs/unregistered.txt").relative_to(source).as_posix() in plan._paths
    assert list(store.iterdir()) == []
    assert all((directory / "game.json.pag").is_file() for directory in metadata)


@pytest.mark.parametrize(
    "relative",
    [
        "._DAV/game.json.pag",
        ".DAV/game.json.pag",
        "images/@eaDir/cover.png/thumb.jpg",
        "docs/.private/rules.md",
        "../outside.txt",
        "docs\\rules.md",
        "docs/report:copy.txt",
    ],
)
def test_declared_unsafe_paths_still_block_with_the_exact_filename(tmp_path, relative):
    source, _ = inputs(tmp_path)
    manifest_path = source / "assets/games/first/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["resources"][0]["path"] = relative
    write_json(manifest_path, manifest)
    assert plan_library(source).errors == [
        {
            "game": "first",
            "code": "invalid_relative_path",
            "path": "assets/games/first/" + relative,
        }
    ]


def test_unregistered_hidden_content_is_reported_instead_of_silently_dropped(tmp_path):
    source, _ = inputs(tmp_path)
    hidden = source / "assets/games/first/docs/.notes.md"
    hidden.write_text("actual content, not a known service metadata name")
    assert plan_library(source).errors == [
        {
            "game": "first",
            "code": "invalid_relative_path",
            "path": hidden.relative_to(source).as_posix(),
        }
    ]


def test_unreadable_content_directory_blocks_instead_of_importing_an_incomplete_game(
    tmp_path, monkeypatch
):
    source, _ = inputs(tmp_path)
    unreadable = source / "assets/games/first/docs"
    scandir = catalog_import.os.scandir

    def denied_scandir(path):
        if Path(path) == unreadable:
            raise PermissionError(13, "denied", str(path))
        return scandir(path)

    monkeypatch.setattr(catalog_import.os, "scandir", denied_scandir)
    assert plan_library(source).errors == [{"game": "first", "code": "PermissionError"}]


def test_invalid_path_diagnostics_do_not_echo_urls_or_control_data(tmp_path):
    source, _ = inputs(tmp_path)
    manifest_path = source / "assets/games/first/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for relative in ("https://user:private@example.invalid/rules.pdf?token=private", "docs/\x00"):
        manifest["resources"][0]["path"] = relative
        write_json(manifest_path, manifest)
        errors = plan_library(source).errors
        assert errors == [
            {
                "game": "first",
                "code": "expected_text" if "\x00" in relative else "invalid_relative_path",
            }
        ]


@pytest.mark.parametrize("invalid_value", [[], "", 0, False])
def test_empty_invalid_document_types_are_not_silently_treated_as_null(tmp_path, invalid_value):
    source, _ = inputs(tmp_path)
    game_path = source / "assets/games/first/game.json"
    game = json.loads(game_path.read_text(encoding="utf-8"))
    game["documents"] = invalid_value
    write_json(game_path, game)
    assert plan_library(source).errors[0]["code"] == "documents:expected_object"


def test_global_binding_path_cannot_reenable_a_disabled_manifest_resource(tmp_path):
    source, _ = inputs(tmp_path)
    game_path = source / "assets/games/first/game.json"
    game = json.loads(game_path.read_text(encoding="utf-8"))
    game["coverAsset"] = "assets/games/first/images/cover.png"
    write_json(game_path, game)
    manifest_path = source / "assets/games/first/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    next(r for r in manifest["resources"] if r["id"] == "first-cover")["enabled"] = False
    write_json(manifest_path, manifest)
    plan = plan_library(source)
    assert not plan.errors
    assert len(plan.rows[m.resources.name]) == 9
    assert (
        next(r for r in plan.rows[m.bindings.name] if r["kind"] == "cover")["resource_id"]
        == "first-cover"
    )
    assert not next(r for r in plan.rows[m.resources.name] if r["id"] == "first-cover")["enabled"]


def test_draft_hidden_then_atomic_publication_and_rls(db, catalog, tmp_path):
    source, store = inputs(tmp_path)
    result = import_library(db.migrator, plan_library(source), store)
    assert not result["published"]
    with catalog(store) as client:
        assert client.get(BASE).json()["items"] == []
        assert client.get(BASE + "/resources/first-pdf/content").status_code == 404
        released = publish_library(db.migrator, result["batch_id"], store)
        body = client.get(BASE + "/editions/first").json()
        assert body["revision"] == released["revision"]
        edition = body["edition"]
        assert edition["locales"]["en"]["aliases"] == ["search-alias"]
        assert {r["id"] for r in edition["resources"]} == {
            "first-rules",
            "first-pdf",
            "first-cover",
            "first-html",
        }
        assert client.get(BASE, headers={"X-App-ID": "zheri"}).status_code == 401
        html = client.get(BASE + "/resources/first-html/content")
        assert html.status_code == 200 and "attachment" in html.headers["content-disposition"]
        assert "sandbox" in html.headers["content-security-policy"]
    with db.migrator.connect() as c:
        assert c.scalar(sa.select(sa.func.count()).select_from(m.resources)) == 9
        assert c.scalar(sa.select(m.validations.c.checked_at)) == "old-unparsed-date"
        assert c.scalar(sa.select(sa.func.count()).select_from(m.sources)) == 3
    with user_transaction(db.runtime, "alice", app_id="zheri") as c:
        assert c.execute(sa.select(m.resources)).all() == []
    with (
        pytest.raises(DBAPIError),
        user_transaction(db.runtime, "alice", app_id="board_game_agent") as c,
    ):
        c.execute(m.head.update().values(revision=0))
    with (
        pytest.raises(DBAPIError),
        user_transaction(db.runtime, "alice", app_id="board_game_agent") as c,
    ):
        c.execute(sa.select(m.sources))


def test_retry_idempotency_and_file_integrity(db, catalog, tmp_path):
    source, store = inputs(tmp_path)
    plan = plan_library(source)
    first = import_library(db.migrator, plan, store)
    assert import_library(db.migrator, plan, store)["batch_id"] == first["batch_id"]
    blob = next(iter(plan.files.values()))
    path = store / blob["storage_key"]
    original = path.read_bytes()
    path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="integrity_failure"):
        publish_library(db.migrator, first["batch_id"], store)
    with catalog(store) as client:
        assert client.get(BASE + "/revision").json()["revision"] == 0
    path.write_bytes(original)
    release = publish_library(db.migrator, first["batch_id"], store)
    assert publish_library(db.migrator, first["batch_id"], store)["revision"] == release["revision"]
    again = import_library(db.migrator, plan, store)
    assert again["published"] and again["batch_id"] == first["batch_id"]
    assert source.joinpath("assets/games/first/docs/rules.pdf").read_bytes() == b"%PDF-fixture"


def test_pagination_changes_and_withdrawn_content(db, catalog, tmp_path):
    source, store = inputs(tmp_path, ("first", "second"))
    first = import_library(db.migrator, plan_library(source), store)
    v1 = publish_library(db.migrator, first["batch_id"], store)["revision"]
    with catalog(store) as client:
        page = client.get(BASE, params={"limit": 1, "q": "search-alias", "locale": "en"}).json()
        assert page["items"][0]["display"]["title"] == "first"
        assert page["next_cursor"]
        assert (
            client.get(
                BASE, params={"cursor": page["next_cursor"], "q": "search-alias", "locale": "en"}
            ).json()["items"][0]["id"]
            == "second"
        )
        assert client.get(BASE + "/categories").json()["items"] == [
            {"category": "strategy", "editions": 2}
        ]
        assert client.get(BASE + "/games/first").json()["editions"][0]["id"] == "first"
        uri = BASE + "/resources/first-pdf/content"
        pdf = client.get(uri)
        assert pdf.content == b"%PDF-fixture"
        assert client.get(uri, headers={"Range": "bytes=0-3"}).content == b"%PDF"
        assert client.head(uri).content == b""
        assert client.get(uri, headers={"If-None-Match": pdf.headers["etag"]}).status_code == 304
        digest = pdf.headers["etag"].strip('"')
        changes = client.get(BASE + "/changes", params={"limit": 1}).json()
        assert changes["through_revision"] == 0 and changes["next_cursor"]
        filename = source / "assets/catalog.json"
        data = json.loads(filename.read_text())
        data["games"][0]["enabled"] = False
        write_json(filename, data)
        second = import_library(db.migrator, plan_library(source, ["first"]), store)
        v2 = publish_library(db.migrator, second["batch_id"], store)["revision"]
        assert v2 > v1
        assert (
            client.get(
                BASE, params={"cursor": page["next_cursor"], "q": "search-alias", "locale": "en"}
            ).status_code
            == 409
        )
        assert client.get(uri, params={"version": digest}).status_code == 404
        assert client.get(BASE + "/editions/first").status_code == 404
        assert [r["id"] for r in client.get(BASE).json()["items"]] == ["second"]
        remaining = client.get(BASE + "/changes", params={"cursor": changes["next_cursor"]}).json()
        assert remaining["revision"] == v1 and remaining["latest_revision"] == v2
        assert remaining["through_revision"] == v1
        delta = client.get(BASE + "/changes", params={"after": v1}).json()
        assert delta["items"] == [{"revision": v2, "edition_id": "first", "kind": "unpublish"}]


def test_conflicting_drafts_never_overwrite_a_newer_release(db, catalog, tmp_path):
    source, store = inputs(tmp_path)
    a = import_library(db.migrator, plan_library(source), store)
    game = source / "assets/games/first/game.json"
    data = json.loads(game.read_text(encoding="utf-8"))
    data["locales"]["en"]["title"] = "Changed"
    write_json(game, data)
    b = import_library(db.migrator, plan_library(source), store)
    version = publish_library(db.migrator, a["batch_id"], store)["revision"]
    with pytest.raises(ValueError, match="draft_conflict"):
        publish_library(db.migrator, b["batch_id"], store)
    with catalog(store) as client:
        assert client.get(BASE + "/revision").json()["revision"] == version
    fresh = import_library(db.migrator, plan_library(source), store)
    assert fresh["batch_id"] != b["batch_id"]
    assert publish_library(db.migrator, fresh["batch_id"], store)["revision"] > version


def test_missing_file_metadata_is_kept_without_exposing_a_broken_download(db, catalog, tmp_path):
    source, store = inputs(tmp_path)
    (source / "assets/games/first/docs/rules.pdf").unlink()
    plan = plan_library(source)
    assert not plan.errors
    assert any(r.get("resource") == "first-pdf" for r in plan.warnings)
    batch = import_library(db.migrator, plan, store)
    publish_library(db.migrator, batch["batch_id"], store)
    with catalog(store) as client:
        assert client.get(BASE + "/resources/first-pdf/content").status_code == 404
    with db.migrator.connect() as c:
        assert (
            c.scalar(
                sa.select(m.resources.c.observed_file).where(
                    m.resources.c.batch_id == UUID(batch["batch_id"]),
                    m.resources.c.id == "first-pdf",
                )
            )
            == "absent"
        )


def test_pinned_files_never_expose_a_draft_or_withdrawn_resource(db, catalog, tmp_path):
    source, store = inputs(tmp_path)
    plan = plan_library(source)
    old_hash = next(r["file_sha256"] for r in plan.rows[m.resources.name] if r["id"] == "first-pdf")
    batch = import_library(db.migrator, plan, store)
    publish_library(db.migrator, batch["batch_id"], store)
    (source / "assets/games/first/docs/rules.pdf").write_bytes(b"%PDF-new-version")
    plan = plan_library(source)
    new_hash = next(r["file_sha256"] for r in plan.rows[m.resources.name] if r["id"] == "first-pdf")
    draft = import_library(db.migrator, plan, store)
    uri = BASE + "/resources/first-pdf/content"
    with catalog(store) as client:
        assert client.get(uri, params={"version": new_hash}).status_code == 404
        publish_library(db.migrator, draft["batch_id"], store)
        assert client.get(uri).content == b"%PDF-new-version"
        assert client.get(uri, params={"version": old_hash}).content == b"%PDF-fixture"
        manifest = source / "assets/games/first/manifest.json"
        data = json.loads(manifest.read_text())
        next(r for r in data["resources"] if r["id"] == "first-pdf")["enabled"] = False
        write_json(manifest, data)
        withdrawn = import_library(db.migrator, plan_library(source), store)
        publish_library(db.migrator, withdrawn["batch_id"], store)
        assert client.get(uri, params={"version": old_hash}).status_code == 404
        assert client.get(uri, params={"version": new_hash}).status_code == 404


def test_source_change_during_import_cannot_write_a_draft(db, catalog, tmp_path):
    source, store = inputs(tmp_path)
    plan = plan_library(source)
    (source / "assets/games/first/docs/rules.md").write_bytes(b"changed after preview")
    with pytest.raises(ValueError, match="changed_while_copying"):
        import_library(db.migrator, plan, store)
    with db.migrator.connect() as c:
        assert c.scalar(sa.select(sa.func.count()).select_from(m.batches)) == 0


def test_independent_drafts_merge_without_losing_untouched_editions(db, catalog, tmp_path):
    source, store = inputs(tmp_path, ("first", "second"))
    batch = import_library(db.migrator, plan_library(source), store)
    publish_library(db.migrator, batch["batch_id"], store)
    drafts = []
    for slug in ("first", "second"):
        file = source / f"assets/games/{slug}/game.json"
        data = json.loads(file.read_text(encoding="utf-8"))
        data["locales"]["en"]["title"] = slug + " revised"
        write_json(file, data)
        drafts.append(import_library(db.migrator, plan_library(source, [slug]), store))
    for draft in drafts:
        publish_library(db.migrator, draft["batch_id"], store)
    with catalog(store) as client:
        assert [
            r["display"]["title"] for r in client.get(BASE, params={"locale": "en"}).json()["items"]
        ] == ["first revised", "second revised"]


def test_disabled_parent_children_are_not_public(db, catalog, tmp_path):
    source, store = inputs(tmp_path)
    file = source / "assets/catalog.json"
    data = json.loads(file.read_text())
    data["games"][0]["enabled"] = False
    write_json(file, data)
    batch = import_library(db.migrator, plan_library(source), store)
    publish_library(db.migrator, batch["batch_id"], store)
    with user_transaction(db.runtime, "alice", app_id="board_game_agent") as c:
        for table in (
            m.editions,
            m.texts,
            m.categories,
            m.resources,
            m.bindings,
            m.derivations,
            m.files,
        ):
            assert c.execute(sa.select(table)).all() == []
    with db.migrator.connect() as c:
        assert (
            c.scalar(
                sa.select(sa.func.count())
                .select_from(m.editions)
                .where(m.editions.c.batch_id == UUID(batch["batch_id"]))
            )
            == 1
        )


def test_disabled_application_never_reports_a_successful_empty_catalog(db, catalog, tmp_path):
    _, store = inputs(tmp_path)
    with db.migrator.begin() as c:
        original = c.scalar(sa.text("SELECT enabled FROM core.apps WHERE id='board_game_agent'"))
        c.execute(sa.text("UPDATE core.apps SET enabled=false WHERE id='board_game_agent'"))
    try:
        with catalog(store) as client:
            assert client.get(BASE).status_code == 403
            assert client.get(BASE + "/revision").status_code == 403
        # Also cover disabling between the application guard and SQL snapshot.
        with pytest.raises(HTTPException) as error, _snapshot(db.runtime):
            pytest.fail("disabled head must not yield an empty successful snapshot")
        assert error.value.status_code == 503
    finally:
        with db.migrator.begin() as c:
            c.execute(
                sa.text("UPDATE core.apps SET enabled=:enabled WHERE id='board_game_agent'"),
                {"enabled": original},
            )
