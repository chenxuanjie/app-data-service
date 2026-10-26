"""Upgrade a real v4 database with receipts/conflicts, without editing their hashes."""

import hashlib
import json
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import make_url

from app_data_service.cli import ROOT, migrate
from app_data_service.config import make_engine
from app_data_service.modules.zheri.expenses import ExpenseRecord
from app_data_service.modules.zheri.models import Expense
from app_data_service.modules.zheri.sync import ReplayError, Upload, apply_upload
from app_data_service.store import user_transaction


def test_upgrade_preserves_v1_receipts_records_and_unresolved_conflicts(db, monkeypatch):
    name = "app_data_upgrade_" + uuid4().hex
    assert name.startswith("app_data_upgrade_") and len(name) == 49
    created = False
    engines = []
    admin = db.admin
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            c.exec_driver_sql(f'CREATE DATABASE "{name}" OWNER app_migrator')
            created = True
            c.exec_driver_sql(f'REVOKE ALL ON DATABASE "{name}" FROM PUBLIC')
            c.exec_driver_sql(f'GRANT CONNECT ON DATABASE "{name}" TO app_runtime,app_replication')
        urls = {
            role: make_url(url).set(database=name).render_as_string(hide_password=False)
            for role, url in db.urls.items()
        }
        with monkeypatch.context() as context:
            context.setenv("APP_DATA_DATABASE_URL", urls["migrator"])
            command.upgrade(Config(str(ROOT / "alembic.ini")), "0004_account_sessions")
        migrator, runtime = make_engine(urls["migrator"]), make_engine(urls["runtime"])
        engines.extend([migrator, runtime])
        source = {
            "id": "legacy-id",
            "name": "旧客户端支出",
            "amount": "59.10",
            "cycle": "monthly",
            "category": "通信",
            "subcategory": "手机",
        }
        upload = Upload.model_validate(
            {
                "dataset_id": "legacy-data",
                "client_id": str(uuid4()),
                "mutations": [
                    {
                        "entity_type": "expenses",
                        "mutation_id": str(uuid4()),
                        "record_id": "legacy-id",
                        "base_version": 0,
                        "predecessor": None,
                        "kind": "put",
                        "record": source,
                    }
                ],
            }
        )
        mutation = upload.mutations[0]
        payload = {"dataset_id": upload.dataset_id, **mutation.model_dump(mode="json")}
        payload.pop("entity_type")
        payload.pop("edited_at")  # This field did not exist in the v1 receipt.
        payload.pop("base_record")
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        result = {"mutation_id": str(mutation.mutation_id), "status": "applied", "version": 1}
        conflict_id = str(uuid4())
        with migrator.begin() as c:
            c.exec_driver_sql("INSERT INTO core.users VALUES('legacy-user','旧账号')")
            c.exec_driver_sql(
                "INSERT INTO core.datasets VALUES('legacy-data','zheri','legacy-user','旧账本')"
            )
            c.exec_driver_sql(
                "INSERT INTO core.dataset_members VALUES('legacy-data','legacy-user')"
            )
            c.execute(
                sa.insert(Expense).values(
                    **ExpenseRecord.from_legacy_json(source, position=0).database_values(
                        "legacy-data"
                    )
                )
            )
            c.execute(
                sa.text("""INSERT INTO core.mutation_receipts(
                  user_id,client_id,mutation_id,dataset_id,record_id,payload_hash,base_version,result)
                VALUES('legacy-user',:client,:mutation,'legacy-data',
                  'legacy-id',:digest,0,CAST(:result AS JSONB))"""),
                {
                    "client": str(upload.client_id),
                    "mutation": mutation.mutation_id,
                    "digest": digest,
                    "result": json.dumps(result),
                },
            )
            c.execute(
                sa.text("""INSERT INTO zheri.expense_conflicts(
                  id,dataset_id,user_id,record_id,candidate,server_version)
                VALUES(:id,'legacy-data','legacy-user','legacy-id',CAST(:candidate AS JSONB),1)"""),
                {"id": conflict_id, "candidate": json.dumps(source)},
            )
        migrate(urls["migrator"])
        with user_transaction(runtime, "legacy-user", app_id="zheri") as c:
            assert apply_upload(c, "legacy-user", upload) == {"results": [result]}
            receipt = c.execute(
                sa.text("SELECT payload_hash,entity_type FROM zheri.mutation_receipts")
            ).one()
            assert tuple(receipt) == (digest, "expenses")
            assert (
                c.scalar(sa.text("SELECT to_regprocedure('core.promote_personal_dataset(text)')"))
                is None
            )
            assert (
                c.scalar(sa.text("SELECT to_regprocedure('core.ensure_test_account(text)')"))
                is None
            )
            assert c.execute(sa.text("SELECT id,kind FROM core.datasets")).one() == (
                "legacy-data",
                "personal",
            )
            assert c.scalar(sa.select(Expense.row_version).where(Expense.id == "legacy-id")) == 1
            assert (
                c.scalar(
                    sa.text("SELECT resolved_at IS NULL FROM zheri.expense_conflicts WHERE id=:id"),
                    {"id": conflict_id},
                )
                is True
            )
        changed = upload.model_copy(deep=True)
        changed.mutations[0].record["name"] = "不同内容"
        with (
            pytest.raises(ReplayError),
            user_transaction(runtime, "legacy-user", app_id="zheri") as c,
        ):
            apply_upload(c, "legacy-user", changed)
    finally:
        for engine in engines:
            engine.dispose()
        if created:
            with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
                c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
