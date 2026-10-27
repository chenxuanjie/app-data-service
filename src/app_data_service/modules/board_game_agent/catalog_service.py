"""One maintenance import/publish path and immutable SQL-backed public snapshots."""

import base64
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import sqlalchemy as sa

from . import catalog_models as m
from .catalog_import import MAX_FILE, digest_file, safe_path
from .catalog_objects import save_objects


def storage_root(value):
    if value is None:
        raise ValueError("library_store_not_configured")
    original = Path(value)
    if (
        not original.is_dir()
        or original.is_symlink()
        or getattr(original, "is_junction", lambda: False)()
    ):
        raise ValueError("explicit_existing_library_store_required")
    return original.resolve()


def _lock(c):
    c.exec_driver_sql("SET LOCAL transaction_timeout='20s'")
    c.exec_driver_sql("SELECT pg_advisory_xact_lock(194027,14)")
    if not c.scalar(sa.text("SELECT enabled FROM core.apps WHERE id='board_game_agent'")):
        raise ValueError("board_application_disabled")


def _revision(c):
    return c.scalar(sa.select(m.head.c.revision).where(m.head.c.id == 1))


def _entries(c, revision):
    return {
        r.edition_id: r.batch_id
        for r in c.execute(
            sa.select(m.entries.c.edition_id, m.entries.c.batch_id).where(
                m.entries.c.revision == revision
            )
        )
    }


def _copy_files(plan, root):
    if root == plan.source or root.is_relative_to(plan.source) or plan.source.is_relative_to(root):
        raise ValueError("source_and_library_store_must_be_separate")
    for record in plan.files.values():
        destination = safe_path(root, record["storage_key"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        safe_path(root, record["storage_key"])
        if destination.exists():
            if digest_file(destination) != (record["sha256"], record["size"]):
                raise ValueError("existing_blob_integrity_failure")
            continue
        source = safe_path(plan.source, record["source_path"])
        staged = destination.with_name(destination.name + "." + uuid4().hex + ".staging")
        try:
            digest, size = hashlib.sha256(), 0
            with source.open("rb") as incoming, staged.open("xb") as outgoing:
                while chunk := incoming.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_FILE:
                        raise ValueError("source_changed_while_copying")
                    digest.update(chunk)
                    outgoing.write(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if digest.hexdigest() != record["sha256"] or size != record["size"]:
                raise ValueError("source_changed_while_copying")
            # Exclusive hard-link publication never replaces an existing blob.
            # Temporary file and destination are on the same explicitly named store.
            try:
                os.link(staged, destination, follow_symlinks=False)
                if os.name != "nt":
                    descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            except FileExistsError:
                if digest_file(destination) != (record["sha256"], record["size"]):
                    raise ValueError("existing_blob_integrity_failure") from None
        finally:
            staged.unlink(missing_ok=True)


def import_library(engine, plan, store, *, progress=None):
    if plan.errors or not plan.rows[m.editions.name]:
        raise ValueError("library_plan_not_ready")
    root = storage_root(store)
    # All file IO precedes the bounded database transaction. Incomplete drafts and
    # unreferenced blobs cannot become public and are never deleted automatically.
    _copy_files(plan, root)
    save_objects(engine, plan, root, progress)
    summary = plan.summary()
    source_hash = plan.source_hash
    with engine.begin() as c:
        _lock(c)
        revision = _revision(c)
        current = _entries(c, revision)
        ids = {r["id"] for r in plan.rows[m.editions.name]}
        for previous in c.execute(
            sa.select(m.batches).where(m.batches.c.source_hash == source_hash)
        ).mappings():
            if all(current.get(key) == previous["id"] for key in ids):
                released = c.scalar(
                    sa.select(m.releases.c.revision).where(m.releases.c.batch_id == previous["id"])
                )
                return {
                    **summary,
                    "batch_id": str(previous["id"]),
                    "database_written": True,
                    "already_imported": True,
                    "published": True,
                    "revision": released,
                }
        batch = uuid5(NAMESPACE_URL, f"board_library/{source_hash}/{revision}")
        previous = c.scalar(sa.select(m.batches.c.id).where(m.batches.c.id == batch))
        if previous:
            return {
                **summary,
                "batch_id": str(batch),
                "database_written": True,
                "already_imported": True,
                "published": False,
            }
        c.execute(
            m.batches.insert().values(
                id=batch, source_hash=source_hash, base_revision=revision, report=summary
            )
        )
        for table in m.DRAFT_TABLES:
            rows = [{"batch_id": batch, **row} for row in plan.rows[table.name]]
            # Explicit, bounded groups avoid a huge multi-row statement.
            for offset in range(0, len(rows), 100):
                c.execute(table.insert(), rows[offset : offset + 100])
        for offset in range(0, len(plan.sources), 100):
            c.execute(
                m.sources.insert(),
                [{"batch_id": batch, **row} for row in plan.sources[offset : offset + 100]],
            )
        source_files = [
            {"batch_id": batch, "path": path, "file_sha256": digest}
            for path, digest in plan._paths.items()
        ]
        for offset in range(0, len(source_files), 100):
            c.execute(m.source_files.insert(), source_files[offset : offset + 100])
    return {
        **summary,
        "batch_id": str(batch),
        "database_written": True,
        "already_imported": False,
        "published": False,
    }


def publish_library(engine, batch_id, store):
    try:
        batch_id = UUID(str(batch_id))
    except ValueError as error:
        raise ValueError("invalid_library_batch") from error
    root = storage_root(store)
    with engine.connect() as c:
        batch = (
            c.execute(sa.select(m.batches).where(m.batches.c.id == batch_id))
            .mappings()
            .one_or_none()
        )
        if batch is None or not batch["report"].get("ready"):
            raise ValueError("library_batch_not_ready")
        blobs = (
            c.execute(
                sa.select(m.files)
                .join(m.source_files, m.source_files.c.file_sha256 == m.files.c.sha256)
                .where(m.source_files.c.batch_id == batch_id)
                .distinct()
            )
            .mappings()
            .all()
        )
    for blob in blobs:
        path = safe_path(root, blob["storage_key"])
        if not path.is_file() or digest_file(path) != (blob["sha256"], blob["size"]):
            raise ValueError("publication_blob_integrity_failure")
    with engine.begin() as c:
        _lock(c)
        prior = c.scalar(sa.select(m.releases.c.revision).where(m.releases.c.batch_id == batch_id))
        if prior is not None:
            return {
                "batch_id": str(batch_id),
                "revision": prior,
                "published": True,
                "already_published": True,
            }
        current_revision = _revision(c)
        current, base = _entries(c, current_revision), _entries(c, batch["base_revision"])
        editions = (
            c.execute(sa.select(m.editions).where(m.editions.c.batch_id == batch_id))
            .mappings()
            .all()
        )
        if not editions:
            raise ValueError("empty_library_batch")
        if any(current.get(row["id"]) != base.get(row["id"]) for row in editions):
            raise ValueError("library_draft_conflict_reimport_required")
        ids = [row["id"] for row in editions]
        retained = (
            c.execute(
                sa.select(m.editions)
                .select_from(current_editions(current_revision))
                .where(m.editions.c.id.not_in(ids))
            )
            .mappings()
            .all()
        )
        slugs = [r["slug"] for r in (*retained, *editions)]
        if len(slugs) != len(set(slugs)):
            raise ValueError("library_slug_owned_by_another_edition")
        # Resource IDs remain bound to one edition, including across archived
        # releases. Otherwise old pinned URLs could silently refer to another game.
        incoming = m.resources.alias("incoming")
        historical = m.resources.alias("historical")
        conflict = sa.select(1).select_from(
            incoming.join(
                historical,
                sa.and_(
                    incoming.c.id == historical.c.id,
                    incoming.c.edition_id != historical.c.edition_id,
                ),
            ).join(
                m.entries,
                sa.and_(
                    m.entries.c.batch_id == historical.c.batch_id,
                    m.entries.c.edition_id == historical.c.edition_id,
                ),
            )
        )
        if c.scalar(conflict.where(incoming.c.batch_id == batch_id).limit(1)):
            raise ValueError("library_resource_identity_conflict")
        revision = c.scalar(
            m.releases.insert().values(batch_id=batch_id).returning(m.releases.c.revision)
        )
        c.execute(
            m.entries.insert().from_select(
                ["revision", "edition_id", "batch_id"],
                sa.select(sa.literal(revision), m.entries.c.edition_id, m.entries.c.batch_id).where(
                    m.entries.c.revision == current_revision, m.entries.c.edition_id.not_in(ids)
                ),
            )
        )
        c.execute(
            m.entries.insert(),
            [
                {"revision": revision, "edition_id": row["id"], "batch_id": batch_id}
                for row in editions
            ],
        )
        c.execute(
            m.changes.insert(),
            [
                {
                    "revision": revision,
                    "edition_id": row["id"],
                    "kind": "upsert" if row["enabled"] else "unpublish",
                }
                for row in editions
            ],
        )
        c.execute(m.head.update().where(m.head.c.id == 1).values(revision=revision))
    return {
        "batch_id": str(batch_id),
        "revision": revision,
        "previous_revision": current_revision,
        "published": True,
        "already_published": False,
        "entries": len(editions),
    }


@contextmanager
def public_snapshot(engine):
    # One response sees one committed release, even while a maintainer publishes.
    with engine.connect().execution_options(isolation_level="REPEATABLE READ") as c, c.begin():
        c.exec_driver_sql("SET LOCAL transaction_timeout='20s'")
        c.exec_driver_sql("SET TRANSACTION READ ONLY")
        c.exec_driver_sql("SELECT set_config('app.app_id','board_game_agent',true)")
        yield c, _revision(c)


def current_editions(revision):
    return m.editions.join(
        m.entries,
        sa.and_(
            m.entries.c.batch_id == m.editions.c.batch_id,
            m.entries.c.edition_id == m.editions.c.id,
            m.entries.c.revision == revision,
        ),
    )


def edition_row(c, revision, edition_id):
    return (
        c.execute(
            sa.select(m.editions)
            .select_from(current_editions(revision))
            .where(m.editions.c.id == edition_id, m.editions.c.enabled)
        )
        .mappings()
        .one_or_none()
    )


def localized_text(c, row, locale):
    rows = (
        c.execute(
            sa.select(m.texts).where(
                m.texts.c.batch_id == row["batch_id"], m.texts.c.edition_id == row["id"]
            )
        )
        .mappings()
        .all()
    )
    return next(
        (r for language in (locale, "en", "zhHans") for r in rows if r["locale"] == language),
        rows[0] if rows else {},
    )


def resource_data(row):
    result = {key: value for key, value in row.items() if key not in {"batch_id", "observed_file"}}
    result["content_url"] = (
        f"/v1/apps/board_game_agent/catalog/resources/{row['id']}/content?version={row['file_sha256']}"
        if row["file_sha256"]
        else None
    )
    return result


def detail(c, revision, row, locale):
    batch, key = row["batch_id"], row["id"]
    result = {k: v for k, v in row.items() if k not in {"batch_id", "manifest_titles"}}
    texts = (
        c.execute(
            sa.select(m.texts)
            .where(m.texts.c.batch_id == batch, m.texts.c.edition_id == key)
            .order_by(m.texts.c.locale)
        )
        .mappings()
        .all()
    )
    result["locales"] = {
        r["locale"]: {k: v for k, v in r.items() if k not in {"batch_id", "edition_id", "locale"}}
        for r in texts
    }
    result["display"] = {
        k: v
        for k, v in localized_text(c, row, locale).items()
        if k not in {"batch_id", "edition_id"}
    }
    result["categories"] = list(
        c.scalars(
            sa.select(m.categories.c.category)
            .where(m.categories.c.batch_id == batch, m.categories.c.edition_id == key)
            .order_by(m.categories.c.position)
        )
    )
    available = (
        c.execute(
            sa.select(m.resources)
            .where(m.resources.c.batch_id == batch, m.resources.c.edition_id == key)
            .order_by(m.resources.c.priority, m.resources.c.id)
        )
        .mappings()
        .all()
    )
    result["resources"] = [resource_data(r) for r in available]
    visible = {r["id"] for r in available}
    relations = (
        c.execute(
            sa.select(m.derivations)
            .where(m.derivations.c.batch_id == batch, m.derivations.c.resource_id.in_(visible))
            .order_by(m.derivations.c.position)
        )
        .mappings()
        .all()
    )
    for resource in result["resources"]:
        resource["derived_from"] = [
            r["source_reference"] for r in relations if r["resource_id"] == resource["id"]
        ]
    bindings = (
        c.execute(
            sa.select(m.bindings).where(
                m.bindings.c.batch_id == batch, m.bindings.c.edition_id == key
            )
        )
        .mappings()
        .all()
    )
    result["bindings"] = [
        {k: v for k, v in r.items() if k not in {"batch_id", "path"}}
        for r in bindings
        if r["resource_id"] in visible
    ]
    return {"protocol": 1, "revision": revision, "edition": result}


def cursor_encode(value):
    return (
        base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )


def cursor_decode(value):
    try:
        if not isinstance(value, str) or len(value) > 600:
            raise ValueError()
        return json.loads(
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        )
    except (ValueError, UnicodeError):
        raise ValueError("invalid_catalog_cursor") from None
