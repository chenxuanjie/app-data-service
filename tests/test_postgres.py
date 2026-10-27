from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError

from app_data_service.cli import inspect_database, migrate
from app_data_service.modules.zheri.expenses import ExpenseImportPlan, ExpenseRecord
from app_data_service.modules.zheri.import_store import import_expenses
from app_data_service.modules.zheri.models import Expense, ImportBatch, ImportRecord
from app_data_service.store import user_transaction


def source(**changes):
    return {
        "id": f"expense-{uuid4()}",
        "name": "中文支出",
        "amount": "59.10",
        "cycle": "monthly",
        "category": "通信网络",
        "subcategory": "手机套餐",
        **changes,
    }


def values(dataset_id="alice-data", **changes):
    return ExpenseRecord.from_legacy_json(source(**changes), position=0).database_values(dataset_id)


def sqlstate(error):
    return error.value.orig.sqlstate


def test_real_postgres_migration_and_publication(db):
    report = inspect_database(db.urls["migrator"])
    assert report["postgres"].startswith("17.")
    assert report["revision"] == "0013_board_public_changes"
    assert report["wal_level"] == "logical"
    assert report["publication"] == [
        "board_game_agent.conversations",
        "board_game_agent.favorites",
        "board_game_agent.messages",
        "board_game_agent.preferences",
        "board_game_agent.public_changes",
        "board_game_agent.recent_games",
        "board_game_agent.votes",
        "core.dataset_members",
        "core.datasets",
        "zheri.asset_allocations",
        "zheri.assets",
        "zheri.business_preferences",
        "zheri.expense_categories",
        "zheri.expenses",
        "zheri.payroll_records",
    ]
    assert report["sync_connected"] is False
    migrate(db.urls["migrator"])
    with db.admin.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM pg_replication_slots WHERE database=current_database()"
            ).scalar_one()
            == 0
        )


def test_round_trip_preserves_decimal_unicode_and_server_version(db):
    row = values(note="中文 / 😀 / 空格", amount="0.100001")
    with user_transaction(db.runtime, "alice", app_id="zheri") as connection:
        connection.execute(sa.insert(Expense).values(**row, row_version=987))
    with user_transaction(db.runtime, "alice", app_id="zheri") as connection:
        result = (
            connection.execute(sa.select(Expense).where(Expense.id == row["id"])).mappings().one()
        )
        assert result["amount"] == Decimal("0.100001")
        assert result["note"] == row["note"]
        assert result["row_version"] == 1
        assert result["updated_at"].utcoffset().total_seconds() == 0


def test_rls_hides_other_users_and_missing_identity(db):
    row = values()
    with user_transaction(db.runtime, "alice", app_id="zheri") as connection:
        connection.execute(sa.insert(Expense).values(**row))
    with user_transaction(db.runtime, "bob", app_id="zheri") as connection:
        assert (
            connection.execute(sa.select(Expense.id).where(Expense.id == row["id"])).first() is None
        )
        assert (
            connection.execute(
                sa.update(Expense).where(Expense.id == row["id"]).values(name="越权修改")
            ).rowcount
            == 0
        )
    with db.runtime.begin() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(Expense)).scalar_one() == 0


@pytest.mark.parametrize("dataset,user", [("alice-data", "bob"), ("other-data", "alice")])
def test_rls_rejects_cross_user_and_cross_app_writes(db, dataset, user):
    with pytest.raises(DBAPIError) as error:
        with user_transaction(db.runtime, user, app_id="zheri") as connection:
            connection.execute(sa.insert(Expense).values(**values(dataset)))
    assert sqlstate(error) == "42501"


def test_runtime_cannot_create_tables_escalate_roles_or_hard_delete(db):
    for statement in (
        "CREATE TABLE zheri.unauthorized(id integer)",
        "SET ROLE app_migrator",
        "DELETE FROM zheri.expenses",
    ):
        with pytest.raises(DBAPIError) as error:
            with user_transaction(db.runtime, "alice", app_id="zheri") as connection:
                connection.exec_driver_sql(statement)
        assert sqlstate(error) == "42501"
    with db.admin.connect() as connection:
        runtime = connection.exec_driver_sql(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
            "FROM pg_roles WHERE rolname='app_runtime'"
        ).one()
        assert not any(runtime)


@pytest.mark.parametrize("amount", ["-1", "NaN", "Infinity", "0.0000001", "100000000000000"])
def test_database_rejects_invalid_amount_even_if_converter_is_bypassed(db, amount):
    row = values()
    row["amount"] = Decimal(amount)
    with pytest.raises(DBAPIError) as error:
        with user_transaction(db.runtime, "alice", app_id="zheri") as connection:
            connection.execute(sa.insert(Expense).values(**row))
    assert sqlstate(error) == "23514"


def test_optimistic_update_does_not_overwrite_a_newer_record(db):
    row = values()
    with user_transaction(db.runtime, "alice", app_id="zheri") as connection:
        connection.execute(sa.insert(Expense).values(**row))
    with user_transaction(db.runtime, "alice", app_id="zheri") as connection:
        assert (
            connection.execute(
                sa.update(Expense)
                .where(Expense.id == row["id"], Expense.row_version == 1)
                .values(name="新版", row_version=999)
            ).rowcount
            == 1
        )
    with user_transaction(db.runtime, "alice", app_id="zheri") as connection:
        assert (
            connection.execute(
                sa.update(Expense)
                .where(Expense.id == row["id"], Expense.row_version == 1)
                .values(name="过时修改")
            ).rowcount
            == 0
        )
        result = connection.execute(
            sa.select(Expense.name, Expense.row_version).where(Expense.id == row["id"])
        ).one()
        assert result == ("新版", 2)


def test_identity_and_dataset_cannot_be_reassigned(db):
    row = values()
    with user_transaction(db.runtime, "alice", app_id="zheri") as connection:
        connection.execute(sa.insert(Expense).values(**row))
    for change in ({"id": "replacement"}, {"dataset_id": "other-data"}):
        with pytest.raises(DBAPIError) as error:
            with user_transaction(db.runtime, "alice", app_id="zheri") as connection:
                connection.execute(
                    sa.update(Expense).where(Expense.id == row["id"]).values(**change)
                )
        assert sqlstate(error) == "23514"


def test_import_is_atomic_idempotent_and_records_mapping(db):
    payload = [source(), source(amount="2000")]
    plan = ExpenseImportPlan.from_json({"expenses": payload})
    with user_transaction(db.runtime, "alice", app_id="zheri") as connection:
        first = import_expenses(connection, "alice-data", plan)
    with user_transaction(db.runtime, "alice", app_id="zheri") as connection:
        second = import_expenses(connection, "alice-data", plan)
        count = connection.execute(
            sa.select(sa.func.count())
            .select_from(ImportRecord)
            .where(ImportRecord.batch_id == first.batch_id)
        ).scalar_one()
        total = connection.execute(
            sa.select(sa.func.sum(Expense.amount)).where(Expense.id.in_([v["id"] for v in payload]))
        ).scalar_one()
    assert first.created and not second.created
    assert first.batch_id == second.batch_id and count == 2
    assert total == Decimal("2059.10")


def test_import_mid_batch_failure_rolls_back_rows_and_receipt(db):
    existing = values()
    with user_transaction(db.runtime, "alice", app_id="zheri") as connection:
        connection.execute(sa.insert(Expense).values(**existing))
    new = source()
    plan = ExpenseImportPlan.from_json({"expenses": [new, source(id=existing["id"])]})
    with pytest.raises(DBAPIError) as error:
        with user_transaction(db.runtime, "alice", app_id="zheri") as connection:
            import_expenses(connection, "alice-data", plan)
    assert sqlstate(error) == "23505"
    with user_transaction(db.runtime, "alice", app_id="zheri") as connection:
        assert (
            connection.execute(sa.select(Expense.id).where(Expense.id == new["id"])).first() is None
        )
        assert (
            connection.execute(
                sa.select(ImportBatch.id).where(ImportBatch.source_hash == plan.source_hash)
            ).first()
            is None
        )


def test_import_denies_foreign_or_wrong_app_dataset(db):
    plan = ExpenseImportPlan.from_json({"expenses": [source()]})
    for dataset in ("bob-data", "other-data", "missing"):
        with pytest.raises(PermissionError):
            with user_transaction(db.runtime, "alice", app_id="zheri") as connection:
                import_expenses(connection, dataset, plan)


def test_concurrent_duplicate_import_has_one_commit(db):
    plan = ExpenseImportPlan.from_json({"expenses": [source()]})

    def run():
        with user_transaction(db.runtime, "alice", app_id="zheri") as connection:
            return import_expenses(connection, "alice-data", plan)

    with ThreadPoolExecutor(max_workers=2) as executor:
        a, b = list(executor.map(lambda _: run(), range(2)))
    assert a.batch_id == b.batch_id
    assert sorted([a.created, b.created]) == [False, True]


def test_user_scope_does_not_leak_through_reused_connections(db):
    with user_transaction(db.runtime, "alice", app_id="zheri") as connection:
        assert connection.exec_driver_sql("SELECT core.actor_id()").scalar_one() == "alice"
    with db.runtime.begin() as connection:
        assert connection.exec_driver_sql("SELECT core.actor_id()").scalar_one() is None
    with user_transaction(db.runtime, "bob", app_id="zheri") as connection:
        assert connection.exec_driver_sql("SELECT core.actor_id()").scalar_one() == "bob"


def test_replication_role_can_read_publication_but_not_write_or_read_users(db):
    with db.replication.connect() as connection:
        connection.execute(sa.select(Expense.id).limit(1))
    for statement in ("SELECT * FROM core.users", "UPDATE zheri.expenses SET name='bad'"):
        with pytest.raises(DBAPIError) as error:
            with db.replication.begin() as connection:
                connection.exec_driver_sql(statement)
        assert sqlstate(error) == "42501"


def test_audit_mapping_cannot_point_to_another_users_expense(db):
    row = values("bob-data")
    with user_transaction(db.runtime, "bob", app_id="zheri") as connection:
        connection.execute(sa.insert(Expense).values(**row))
    with user_transaction(db.runtime, "alice", app_id="zheri") as connection:
        batch = import_expenses(
            connection, "alice-data", ExpenseImportPlan.from_json({"expenses": []})
        )
    with pytest.raises(DBAPIError) as error:
        with user_transaction(db.runtime, "alice", app_id="zheri") as connection:
            connection.execute(
                sa.insert(ImportRecord).values(
                    batch_id=batch.batch_id, source_id="not-owned", target_id=row["id"]
                )
            )
    assert sqlstate(error) == "42501"


def test_connection_waits_have_explicit_limits(db):
    with db.runtime.connect() as connection:
        assert connection.exec_driver_sql("SHOW statement_timeout").scalar_one() == "15s"
        assert connection.exec_driver_sql("SHOW lock_timeout").scalar_one() == "5s"
