"""Anonymous SQL catalog reads. Publication is a maintenance operation, never HTTP DML."""

import hashlib
import mimetypes
from contextlib import contextmanager
from pathlib import Path

import sqlalchemy as sa
from fastapi import HTTPException, Query, Request, Response
from fastapi.responses import FileResponse

from . import catalog_models as m
from .catalog_import import digest_file, safe_path
from .catalog_service import (
    current_editions,
    cursor_decode,
    cursor_encode,
    detail,
    edition_row,
    public_snapshot,
    resource_data,
    storage_root,
)

PREFIX = "/v1/apps/board_game_agent/catalog"


@contextmanager
def _snapshot(engine):
    with public_snapshot(engine) as (connection, revision):
        if revision is None:
            # RLS can hide the head when an application is disabled. Never return
            # a successful empty catalog that a client could persist as a deletion.
            raise HTTPException(503, "catalog_unavailable")
        yield connection, revision


def _decode(value, length):
    try:
        result = cursor_decode(value)
        if not isinstance(result, list) or len(result) != length:
            raise ValueError()
        return result
    except ValueError as error:
        raise HTTPException(422, "invalid_catalog_cursor") from error


def _summaries(c, rows, locale):
    if not rows:
        return []
    keys = [(row["batch_id"], row["id"]) for row in rows]
    texts = (
        c.execute(
            sa.select(m.texts)
            .where(sa.tuple_(m.texts.c.batch_id, m.texts.c.edition_id).in_(keys))
            .order_by(m.texts.c.locale)
        )
        .mappings()
        .all()
    )
    covers = (
        c.execute(
            sa.select(m.resources)
            .select_from(m.bindings)
            .join(
                m.resources,
                sa.and_(
                    m.bindings.c.batch_id == m.resources.c.batch_id,
                    m.bindings.c.resource_id == m.resources.c.id,
                ),
            )
            .where(
                sa.tuple_(m.bindings.c.batch_id, m.bindings.c.edition_id).in_(keys),
                m.bindings.c.kind == "cover",
            )
        )
        .mappings()
        .all()
    )
    result = []
    for row in rows:
        localized = [
            t for t in texts if (t["batch_id"], t["edition_id"]) == (row["batch_id"], row["id"])
        ]
        selected = next(
            (
                t
                for language in (locale, "en", "zhHans")
                for t in localized
                if t["locale"] == language
            ),
            localized[0] if localized else {},
        )
        cover = next(
            (r for r in covers if (r["batch_id"], r["edition_id"]) == (row["batch_id"], row["id"])),
            None,
        )
        result.append(
            {
                **{
                    k: row[k]
                    for k in (
                        "id",
                        "game_id",
                        "slug",
                        "edition_code",
                        "sort_order",
                        "card_accent",
                        "external_score_text",
                        "supported_players",
                        "recommended_player",
                    )
                },
                "display": {
                    k: selected.get(k, "")
                    for k in ("locale", "title", "subtitle", "edition_label", "summary")
                },
                "cover": resource_data(cover) if cover else None,
            }
        )
    return result


class CatalogFiles:
    def __init__(self, root):
        self.root = root
        self.verified = {}

    def response(self, blob, resource, request):
        try:
            root = storage_root(self.root)
        except (ValueError, OSError) as error:
            raise HTTPException(503, "library_store_not_configured") from error
        try:
            path = safe_path(root, blob["storage_key"])
            status = path.stat()
            fingerprint = (status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)
            if self.verified.get(blob["sha256"]) != fingerprint:
                if digest_file(path) != (blob["sha256"], blob["size"]):
                    raise ValueError("blob_changed")
                self.verified[blob["sha256"]] = fingerprint
                if len(self.verified) > 4096:
                    self.verified.pop(next(iter(self.verified), None), None)
        except (ValueError, OSError) as error:
            raise HTTPException(503, "library_content_integrity_failure") from error
        etag = '"' + blob["sha256"] + '"'
        headers = {
            "ETag": etag,
            "Cache-Control": "public, max-age=0, must-revalidate",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        }
        if etag in [v.strip() for v in request.headers.get("if-none-match", "").split(",")]:
            return Response(status_code=304, headers=headers)
        name = Path(resource["path"]).name
        # Imported HTML/SVG cannot execute scripts under the account/API origin.
        attachment = Path(name).suffix.lower() in {".html", ".svg"}
        return FileResponse(
            path,
            media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
            headers=headers,
            filename=name,
            content_disposition_type="attachment" if attachment else "inline",
        )


def install(router, runtime):
    settings = runtime.application_settings.get("board_game_agent", {})
    files = CatalogFiles(settings.get("public_file_roots", {}).get("library_store"))

    @router.get(PREFIX + "/revision")
    def revision(response: Response):
        response.headers["Cache-Control"] = "no-store"
        with _snapshot(runtime.engine) as (c, current):
            published = c.scalar(
                sa.select(m.releases.c.published_at).where(m.releases.c.revision == current)
            )
            return {"protocol": 1, "revision": current, "published_at": published}

    @router.get(PREFIX)
    def catalog(
        response: Response,
        locale: str = Query(default="zhHans", max_length=64),
        q: str = Query(default="", max_length=200),
        category: str | None = Query(default=None, max_length=256),
        limit: int = Query(default=50, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=600),
    ):
        response.headers["Cache-Control"] = "no-store"
        fingerprint = hashlib.sha256(repr((locale, q, category)).encode()).hexdigest()
        with _snapshot(runtime.engine) as (c, current):
            query = (
                sa.select(m.editions)
                .select_from(current_editions(current))
                .where(m.editions.c.enabled)
            )
            if q:
                pattern = (
                    "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                )
                query = query.where(
                    sa.or_(
                        m.editions.c.slug.ilike(pattern, escape="\\"),
                        sa.exists(
                            sa.select(1).where(
                                m.texts.c.batch_id == m.editions.c.batch_id,
                                m.texts.c.edition_id == m.editions.c.id,
                                sa.or_(
                                    m.texts.c.title.ilike(pattern, escape="\\"),
                                    sa.func.array_to_string(m.texts.c.aliases, " ").ilike(
                                        pattern, escape="\\"
                                    ),
                                ),
                            )
                        ),
                    )
                )
            if category:
                query = query.where(
                    sa.exists(
                        sa.select(1).where(
                            m.categories.c.batch_id == m.editions.c.batch_id,
                            m.categories.c.edition_id == m.editions.c.id,
                            m.categories.c.category == category,
                        )
                    )
                )
            if cursor:
                revision, token, order, key = _decode(cursor, 4)
                if (
                    type(revision) is not int
                    or token != fingerprint
                    or type(order) is not int
                    or not isinstance(key, str)
                ):
                    raise HTTPException(422, "invalid_catalog_cursor")
                if revision != current:
                    raise HTTPException(409, "catalog_changed_restart_pagination")
                query = query.where(
                    sa.tuple_(m.editions.c.sort_order, m.editions.c.id) > (order, key)
                )
            rows = (
                c.execute(query.order_by(m.editions.c.sort_order, m.editions.c.id).limit(limit + 1))
                .mappings()
                .all()
            )
            page = rows[:limit]
            next_cursor = (
                cursor_encode([current, fingerprint, page[-1]["sort_order"], page[-1]["id"]])
                if len(rows) > limit
                else None
            )
            return {
                "protocol": 1,
                "revision": current,
                "items": _summaries(c, page, locale),
                "next_cursor": next_cursor,
            }

    @router.get(PREFIX + "/categories")
    def categories(response: Response):
        response.headers["Cache-Control"] = "no-store"
        with _snapshot(runtime.engine) as (c, current):
            query = (
                sa.select(m.categories.c.category, sa.func.count().label("editions"))
                .join(
                    m.entries,
                    sa.and_(
                        m.entries.c.batch_id == m.categories.c.batch_id,
                        m.entries.c.edition_id == m.categories.c.edition_id,
                        m.entries.c.revision == current,
                    ),
                )
                .group_by(m.categories.c.category)
                .order_by(m.categories.c.category)
            )
            return {
                "protocol": 1,
                "revision": current,
                "items": [dict(r) for r in c.execute(query).mappings()],
            }

    @router.get(PREFIX + "/games/{game_id}")
    def game(
        game_id: str, response: Response, locale: str = Query(default="zhHans", max_length=64)
    ):
        response.headers["Cache-Control"] = "no-store"
        with _snapshot(runtime.engine) as (c, current):
            rows = (
                c.execute(
                    sa.select(m.editions)
                    .select_from(current_editions(current))
                    .where(m.editions.c.game_id == game_id, m.editions.c.enabled)
                    .order_by(m.editions.c.sort_order, m.editions.c.id)
                )
                .mappings()
                .all()
            )
            if not rows:
                raise HTTPException(404, "game_unavailable")
            return {
                "protocol": 1,
                "revision": current,
                "game_id": game_id,
                "editions": _summaries(c, rows, locale),
            }

    @router.get(PREFIX + "/editions/{edition_id}")
    def edition(
        edition_id: str, response: Response, locale: str = Query(default="zhHans", max_length=64)
    ):
        response.headers["Cache-Control"] = "no-store"
        with _snapshot(runtime.engine) as (c, current):
            row = edition_row(c, current, edition_id)
            if row is None:
                raise HTTPException(404, "edition_unavailable")
            return detail(c, current, row, locale)

    @router.get(PREFIX + "/changes")
    def changes(
        response: Response,
        after: int = Query(default=0, ge=0, le=2**63 - 1),
        limit: int = Query(default=100, ge=1, le=200),
        cursor: str | None = Query(default=None, max_length=600),
    ):
        response.headers["Cache-Control"] = "no-store"
        with _snapshot(runtime.engine) as (c, current):
            target, last_revision, last_id = current, after, ""
            if cursor:
                target, original, last_revision, last_id = _decode(cursor, 4)
                if (
                    type(target) is not int
                    or type(last_revision) is not int
                    or type(original) is not int
                    or original != after
                    or not after <= last_revision <= target <= current
                    or not isinstance(last_id, str)
                ):
                    raise HTTPException(422, "invalid_catalog_cursor")
            elif after > current:
                raise HTTPException(409, "catalog_revision_ahead")
            query = (
                sa.select(m.changes)
                .where(
                    m.changes.c.revision > after,
                    m.changes.c.revision <= target,
                    sa.tuple_(m.changes.c.revision, m.changes.c.edition_id)
                    > (last_revision, last_id),
                )
                .order_by(m.changes.c.revision, m.changes.c.edition_id)
                .limit(limit + 1)
            )
            rows = c.execute(query).mappings().all()
            page = [dict(row) for row in rows[:limit]]
            next_cursor = (
                cursor_encode([target, after, page[-1]["revision"], page[-1]["edition_id"]])
                if len(rows) > limit
                else None
            )
            return {
                "protocol": 1,
                "revision": target,
                "latest_revision": current,
                "items": page,
                "through_revision": after if next_cursor else target,
                "next_cursor": next_cursor,
            }

    @router.get(PREFIX + "/resources/{resource_id}/text")
    def text_content(resource_id: str, response: Response):
        response.headers["Cache-Control"] = "no-store"
        with _snapshot(runtime.engine) as (c, current):
            row = (
                c.execute(
                    sa.select(
                        m.resources.c.file_sha256,
                        m.file_bodies.c.text_body,
                        m.file_bodies.c.json_body,
                        m.file_bodies.c.encoding,
                    )
                    .select_from(
                        m.resources.join(
                            current_editions(current),
                            sa.and_(
                                m.resources.c.batch_id == m.editions.c.batch_id,
                                m.resources.c.edition_id == m.editions.c.id,
                            ),
                        ).join(m.file_bodies, m.file_bodies.c.sha256 == m.resources.c.file_sha256)
                    )
                    .where(
                        m.resources.c.id == resource_id,
                        m.editions.c.enabled,
                        m.file_bodies.c.text_body.is_not(None),
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise HTTPException(404, "resource_text_unavailable")
            return {
                "protocol": 1,
                "revision": current,
                "resource_id": resource_id,
                "sha256": row["file_sha256"],
                "text": row["text_body"],
                "json": row["json_body"],
                "encoding": row["encoding"],
            }

    @router.api_route(PREFIX + "/resources/{resource_id}/content", methods=["GET", "HEAD"])
    def content(
        resource_id: str,
        request: Request,
        version: str | None = Query(default=None, pattern="^[a-f0-9]{64}$"),
    ):
        with _snapshot(runtime.engine) as (c, current):
            resource = (
                c.execute(
                    sa.select(m.resources)
                    .join(
                        current_editions(current),
                        sa.and_(
                            m.resources.c.batch_id == m.editions.c.batch_id,
                            m.resources.c.edition_id == m.editions.c.id,
                        ),
                    )
                    .where(m.resources.c.id == resource_id, m.editions.c.enabled)
                )
                .mappings()
                .one_or_none()
            )
            if resource is None:
                raise HTTPException(404, "resource_unavailable")
            digest = version or resource["file_sha256"]
            # A pinned historical file remains readable only while the same public
            # resource is currently enabled. A draft or withdrawn resource cannot leak.
            historical = (
                c.execute(
                    sa.select(m.resources)
                    .where(
                        m.resources.c.id == resource_id,
                        m.resources.c.edition_id == resource["edition_id"],
                        m.resources.c.file_sha256 == digest,
                    )
                    .order_by(m.resources.c.batch_id)
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            if historical is None:
                raise HTTPException(404, "resource_unavailable")
            blob = (
                c.execute(sa.select(m.files).where(m.files.c.sha256 == digest))
                .mappings()
                .one_or_none()
            )
            if blob is None:
                raise HTTPException(404, "resource_unavailable")
        return files.response(blob, historical, request)
