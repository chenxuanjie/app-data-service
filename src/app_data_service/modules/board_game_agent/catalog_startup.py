"""Explicit import job with committed SQL progress and persistent idempotency."""

import json
import os
import re
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from app_data_service.config import database_url, make_engine

from . import catalog_models as m
from .catalog_import import DEFAULT_SQL_FILE_MAX, identifier, plan_library
from .catalog_objects import object_transaction
from .catalog_service import import_library, publish_library, storage_root


def emit(stage, **values):
    print(
        json.dumps(
            {"event": "board_library_import", "stage": stage, **values},
            ensure_ascii=False,
            default=str,
        ),
        flush=True,
    )


def _store_id(root):
    path = root / ".store-id"
    if path.is_symlink():
        raise ValueError("invalid_library_store_id")
    try:
        with path.open("x", encoding="ascii") as stream:
            stream.write(str(uuid4()))
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        pass
    with path.open("r", encoding="ascii") as stream:
        return UUID(stream.read(64).strip())


def _log_games(c, batch, log):
    rows = (
        c.execute(
            sa.select(m.editions)
            .where(m.editions.c.batch_id == batch)
            .order_by(m.editions.c.sort_order, m.editions.c.id)
        )
        .mappings()
        .all()
    )
    for row in rows:
        title = c.scalar(
            sa.select(m.texts.c.title)
            .where(m.texts.c.batch_id == batch, m.texts.c.edition_id == row["id"])
            .order_by((m.texts.c.locale == "zhHans").desc(), m.texts.c.locale)
            .limit(1)
        )
        counts = (
            c.execute(
                sa.select(
                    sa.func.count(m.resources.c.id).label("resource_records"),
                    sa.func.count(sa.distinct(m.file_bodies.c.sha256)).label("sql_small_files"),
                    sa.func.count()
                    .filter(m.resources.c.observed_file == "absent")
                    .label("missing_files"),
                    sa.func.count()
                    .filter(m.resources.c.visibility == "archive")
                    .label("archive_records"),
                )
                .select_from(
                    m.resources.outerjoin(
                        m.file_bodies, m.resources.c.file_sha256 == m.file_bodies.c.sha256
                    )
                )
                .where(m.resources.c.batch_id == batch, m.resources.c.edition_id == row["id"])
            )
            .mappings()
            .one()
        )
        log(
            "game_in_database",
            game_id=row["id"],
            title=title,
            enabled=row["enabled"],
            message=f"桌游已入库：{title or row['id']}",
            **counts,
        )


def run_import(
    engine, source, store, *, import_id="initial", sql_file_max=DEFAULT_SQL_FILE_MAX, log=emit
):
    identifier(import_id)
    root = storage_root(store)
    # A session lock serializes whole import jobs without holding a SQL transaction
    # during source IO. Manual publication keeps its existing short transaction lock.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock:
        if not lock.scalar(sa.text("SELECT pg_try_advisory_lock(194027,15)")):
            log("already_running", message="已有资料导入正在运行，本次跳过")
            return {"status": "already_running"}
        try:
            store_id = _store_id(root)
            with engine.connect() as c:
                c.exec_driver_sql("SET LOCAL transaction_timeout='20s'")
                previous = (
                    c.execute(sa.select(m.import_runs).where(m.import_runs.c.id == import_id))
                    .mappings()
                    .one_or_none()
                )
                if (
                    previous
                    and previous["store_id"] == store_id
                    and previous["sql_file_max"] == sql_file_max
                ):
                    _log_games(c, previous["batch_id"], log)
                    log(
                        "skipped_completed",
                        import_id=import_id,
                        revision=previous["revision"],
                        message="该导入编号已成功完成；未重新扫描源目录",
                    )
                    return {"status": "already_completed", "revision": previous["revision"]}
            log(
                "planning",
                import_id=import_id,
                source=str(source),
                sql_file_max_bytes=sql_file_max,
                message="开始读取资料；旧目录只读，不移动或删除原文件",
            )
            plan = plan_library(source, sql_file_max=sql_file_max)
            log(
                "plan_ready" if not plan.errors else "plan_failed",
                counts=plan.summary()["counts"],
                files=len(plan.files),
                sql_file_candidates=plan.summary()["sql_file_candidates"],
                errors=len(plan.errors),
                warnings=len(plan.warnings),
            )
            for warning in plan.warnings:
                log(
                    "warning",
                    **{
                        k: v
                        for k, v in warning.items()
                        if k in {"code", "game", "resource", "path", "fields", "section"}
                    },
                )
            for error in plan.errors:
                log("input_error", **error)
            if plan.errors:
                raise ValueError("library_plan_not_ready")
            log("copying_and_saving", message="正在复制原件并将小文件内容写入 SQL")
            draft = import_library(engine, plan, root, progress=log)
            log("draft_saved", batch_id=draft["batch_id"], counts=draft["counts"])
            published = publish_library(engine, draft["batch_id"], root)
            batch = UUID(draft["batch_id"])
            with object_transaction(engine) as c:
                c.execute(
                    insert(m.import_runs)
                    .values(
                        id=import_id,
                        batch_id=batch,
                        revision=published["revision"],
                        store_id=store_id,
                        sql_file_max=sql_file_max,
                    )
                    .on_conflict_do_update(
                        index_elements=[m.import_runs.c.id],
                        set_={
                            "batch_id": batch,
                            "revision": published["revision"],
                            "store_id": store_id,
                            "sql_file_max": sql_file_max,
                            "completed_at": sa.func.now(),
                        },
                    )
                )
                total = c.scalar(
                    sa.select(sa.func.count())
                    .select_from(m.source_files)
                    .where(m.source_files.c.batch_id == batch)
                )
                inline = c.scalar(
                    sa.select(sa.func.count(sa.distinct(m.file_bodies.c.sha256)))
                    .select_from(
                        m.source_files.join(
                            m.file_bodies, m.source_files.c.file_sha256 == m.file_bodies.c.sha256
                        )
                    )
                    .where(m.source_files.c.batch_id == batch)
                )
                _log_games(c, batch, log)
            log(
                "completed",
                import_id=import_id,
                batch_id=str(batch),
                revision=published["revision"],
                games=len(plan.rows[m.editions.name]),
                source_files=total,
                sql_small_files=inline,
                warnings=len(plan.warnings),
                message="资料已入库并发布；本编号再次执行将直接跳过",
            )
            return {
                "status": "completed",
                "revision": published["revision"],
                "games": len(plan.rows[m.editions.name]),
                "sql_small_files": inline,
            }
        finally:
            lock.execute(sa.text("SELECT pg_advisory_unlock(194027,15)"))


def from_environment():
    source = os.environ.get("APP_DATA_BOARD_LIBRARY_SOURCE", "")
    if not source:
        return {"board_library_import": "disabled"}
    engine = None
    try:
        engine = make_engine(database_url())
        return run_import(
            engine,
            source,
            os.environ.get("APP_DATA_BOARD_LIBRARY_STORE", "/data/board-library"),
            import_id=os.environ.get("APP_DATA_BOARD_LIBRARY_IMPORT_ID", "initial"),
            sql_file_max=int(
                os.environ.get("APP_DATA_BOARD_LIBRARY_SQL_MAX_BYTES", str(DEFAULT_SQL_FILE_MAX))
            ),
        )
    except (ValueError, OSError, SQLAlchemyError, RecursionError) as error:
        # Do not print database URLs, provider responses, private bodies or raw errors.
        code = getattr(getattr(error, "orig", None), "sqlstate", None)
        if code is None:
            code = (
                str(error) if re.fullmatch(r"[a-z_]{1,100}", str(error)) else type(error).__name__
            )
        emit(
            "failed", code=code, message="资料导入失败，未自动清理；已发布资料保留，请检查此前日志"
        )
        return {"board_library_import": "failed", "code": code}
    finally:
        if engine is not None:
            engine.dispose()
