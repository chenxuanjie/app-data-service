"""Explicit local conversion and maintenance publication; no NAS/network discovery."""

from pathlib import Path

from app_data_service.config import make_engine

from .catalog_import import DEFAULT_SQL_FILE_MAX, plan_library
from .catalog_service import import_library, publish_library


def _import(args, urls):
    plan = plan_library(args.source, args.games, sql_file_max=args.sql_file_max_bytes)
    if plan.errors:
        raise ValueError("library_plan_not_ready; run board-plan-library for details")
    engine = make_engine(urls["migrator"])
    try:
        return import_library(engine, plan, args.store)
    finally:
        engine.dispose()


def _publish(args, urls):
    engine = make_engine(urls["migrator"])
    try:
        return publish_library(engine, args.batch, args.store)
    finally:
        engine.dispose()


def register_commands(commands):
    plan = commands.add_parser(
        "board-plan-library", help="Preview legacy conversion without writes"
    )
    plan.add_argument("--source", type=Path, required=True)
    plan.add_argument("--games", nargs="+", help="Explicit subset of catalog slugs")
    plan.add_argument("--sql-file-max-bytes", type=int, default=DEFAULT_SQL_FILE_MAX)
    plan.set_defaults(
        needs_database=False,
        handler=lambda args, _: plan_library(
            args.source, args.games, sql_file_max=args.sql_file_max_bytes
        ).summary(),
    )

    draft = commands.add_parser(
        "board-import-library", help="Import a typed, unpublished SQL batch"
    )
    draft.add_argument("--source", type=Path, required=True)
    draft.add_argument("--store", type=Path, required=True, help="Existing separate content store")
    draft.add_argument("--games", nargs="+", help="Explicit subset of catalog slugs")
    draft.add_argument("--sql-file-max-bytes", type=int, default=DEFAULT_SQL_FILE_MAX)
    draft.set_defaults(handler=_import)

    release = commands.add_parser(
        "board-publish-library", help="Atomically publish an imported batch"
    )
    release.add_argument("--batch", required=True)
    release.add_argument("--store", type=Path, required=True)
    release.set_defaults(handler=_publish)

    auto = commands.add_parser(
        "board-auto-import-library", help="Explicit one-time configured import with logs"
    )
    from .catalog_startup import from_environment

    auto.set_defaults(needs_database=False, handler=lambda _args, _urls: from_environment())
