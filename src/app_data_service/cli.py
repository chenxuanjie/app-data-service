"""Small maintenance CLI. Never prints database URLs or credentials."""

import argparse
import json
import os
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError

from app_data_service.config import database_url, make_engine
from app_data_service.expenses import ExpenseImportPlan
from app_data_service.store import import_expenses, user_transaction

ROOT = Path(os.environ.get("APP_DATA_PROJECT_ROOT", Path(__file__).resolve().parents[2])).resolve()


def local_config() -> dict:
    path = ROOT / ".local" / "dev-config.json"
    if not path.exists():
        raise ValueError("Run tools/bootstrap_dev.py first")
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("profile") != "local-development":
        raise ValueError("Not a local development profile")
    return config


def migrate(url: str) -> None:
    previous = os.environ.get("APP_DATA_DATABASE_URL")
    os.environ["APP_DATA_DATABASE_URL"] = url
    try:
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    finally:
        if previous is None:
            os.environ.pop("APP_DATA_DATABASE_URL", None)
        else:
            os.environ["APP_DATA_DATABASE_URL"] = previous


def source_plan(path: Path) -> ExpenseImportPlan:
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Expense source exceeds 8 MiB")
    return ExpenseImportPlan.from_json(
        json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
    )


def seed_demo(url: str) -> None:
    engine = make_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO core.users(id, display_name) VALUES ('dev-alice', '本地测试账号') "
                    "ON CONFLICT (id) DO NOTHING"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO core.datasets(id, app_id, owner_id, name) "
                    "VALUES ('dev-zheri', 'zheri', 'dev-alice', '本地测试数据') "
                    "ON CONFLICT (id) DO NOTHING"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO core.dataset_members(dataset_id,user_id) "
                    "VALUES ('dev-zheri','dev-alice') ON CONFLICT DO NOTHING"
                )
            )
    finally:
        engine.dispose()


def inspect_database(url: str) -> dict:
    engine = make_engine(url)
    try:
        with engine.connect() as connection:
            version = connection.exec_driver_sql("SHOW server_version").scalar_one()
            wal = connection.exec_driver_sql("SHOW wal_level").scalar_one()
            revision = connection.exec_driver_sql(
                "SELECT version_num FROM core.alembic_version"
            ).scalar_one()
            publication = (
                connection.exec_driver_sql(
                    "SELECT schemaname || '.' || tablename FROM pg_publication_tables "
                    "WHERE pubname='powersync' ORDER BY schemaname, tablename"
                )
                .scalars()
                .all()
            )
            expected = [
                "core.dataset_members",
                "core.datasets",
                "zheri.assets",
                "zheri.expense_categories",
                "zheri.expenses",
            ]
            if wal != "logical" or publication != expected:
                raise ValueError("PowerSync prerequisite configuration is incomplete")
            return {
                "postgres": version,
                "revision": revision,
                "wal_level": wal,
                "publication": publication,
                "sync_connected": False,
            }
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true", help="Use the ignored local dev profile")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    commands.add_parser("check")
    commands.add_parser("seed-demo", help="Local-only fixture; not a verified NAS identity")
    preview = commands.add_parser("preview-expenses")
    preview.add_argument("source", type=Path)
    apply = commands.add_parser("import-expenses")
    apply.add_argument("source", type=Path)
    apply.add_argument("--user-id", required=True)
    apply.add_argument("--dataset-id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "preview-expenses":
            plan = source_plan(args.source)
            output = {
                "count": len(plan.records),
                "source_hash": plan.source_hash,
                "amount_sum": format(sum((r.amount for r in plan.records), Decimal(0)), "f"),
                "writes": False,
            }
        else:
            urls = local_config()["urls"] if args.local else None
            migrator_url = urls["migrator"] if urls else database_url()
            if args.command == "migrate":
                migrate(migrator_url)
                output = {"migration": "head", "completed": True}
            elif args.command == "check":
                output = inspect_database(migrator_url)
            elif args.command == "seed-demo":
                if not args.local:
                    raise ValueError("seed-demo requires --local and cannot target another profile")
                seed_demo(migrator_url)
                output = {"user_id": "dev-alice", "dataset_id": "dev-zheri", "verified_nas": False}
            else:
                runtime_url = urls["runtime"] if urls else database_url()
                plan = source_plan(args.source)
                engine = make_engine(runtime_url)
                try:
                    with user_transaction(engine, args.user_id) as connection:
                        result = import_expenses(connection, args.dataset_id, plan)
                    output = asdict(result)
                finally:
                    engine.dispose()
        print(json.dumps(output, ensure_ascii=False))
    except DBAPIError as error:
        state = getattr(error.orig, "sqlstate", "unknown")
        parser.exit(1, f"Database operation failed (SQLSTATE {state}); no credentials printed.\n")
    except (ValueError, PermissionError, OSError) as error:
        # Known input/config errors contain fixed labels, not URLs or private record values.
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
