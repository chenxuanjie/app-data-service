"""Zheri-owned administrative commands; never part of shared authentication."""

import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import sqlalchemy as sa

from app_data_service.config import make_engine
from app_data_service.store import user_transaction

from .expenses import ExpenseImportPlan
from .import_store import import_expenses


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


def preview(args, urls):
    plan = source_plan(args.source)
    return {
        "count": len(plan.records),
        "source_hash": plan.source_hash,
        "amount_sum": format(sum((r.amount for r in plan.records), Decimal(0)), "f"),
        "writes": False,
    }


def seed(args, urls):
    if not args.local:
        raise ValueError("seed-demo requires --local and cannot target another profile")
    seed_demo(urls["migrator"])
    return {"user_id": "dev-alice", "dataset_id": "dev-zheri", "verified_nas": False}


def import_records(args, urls):
    plan = source_plan(args.source)
    engine = make_engine(urls["runtime"])
    try:
        with user_transaction(engine, args.user_id, app_id="zheri") as connection:
            return asdict(import_expenses(connection, args.dataset_id, plan))
    finally:
        engine.dispose()


def register_commands(commands):
    command = commands.add_parser(
        "seed-demo", help="Zheri local-only fixture, not a verified NAS identity"
    )
    command.set_defaults(handler=seed)
    command = commands.add_parser("preview-expenses", help="Validate Zheri import without writing")
    command.add_argument("source", type=Path)
    command.set_defaults(handler=preview, needs_database=False)
    command = commands.add_parser("import-expenses")
    command.add_argument("source", type=Path)
    command.add_argument("--user-id", required=True)
    command.add_argument("--dataset-id", required=True)
    command.set_defaults(handler=import_records)
