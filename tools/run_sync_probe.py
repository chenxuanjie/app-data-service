"""Run real PowerSync + API + isolated Dart clients, then clean only this run.

No DSM login, production data, NAS deployment or app_data reset. Needs the local
development Postgres and the pinned PowerSync Docker image already available.
"""

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
import sqlalchemy as sa
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from psycopg import sql
from sqlalchemy.engine import make_url

from app_data_service.auth import API_AUDIENCE, TokenAuthority
from app_data_service.cli import local_config, migrate
from app_data_service.config import make_engine
from app_data_service.entities import canonical_record, database_values
from app_data_service.models import (
    App,
    Asset,
    Dataset,
    DatasetMember,
    Expense,
    ExpenseCategory,
    User,
)

ROOT = Path(__file__).resolve().parents[1]


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--docker", choices=["native", "wsl"], default="wsl" if os.name == "nt" else "native"
    )
    parser.add_argument("--dart", default=shutil.which("dart") or "dart")
    args = parser.parse_args()
    docker = ["wsl", "-d", "Ubuntu", "--exec", "docker"] if args.docker == "wsl" else ["docker"]

    def host_path(path: Path) -> str:
        if args.docker == "wsl":
            return subprocess.check_output(
                ["wsl", "-d", "Ubuntu", "--exec", "wslpath", "-a", str(path)], text=True, timeout=10
            ).strip()
        return str(path)

    # Fail before creating anything if the fixed local ports are already used.
    import socket

    for port in (18280, 18290):
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", port))

    urls = local_config()["urls"]  # Profile guard refuses non-development configuration.
    identifier = uuid4().hex[:12]
    source_name = "app_data_sync_" + identifier
    storage_name = "app_data_buckets_" + identifier
    storage_role = "probe_store_" + identifier
    project = "app-data-sync-probe-" + identifier
    directory = ROOT / ".local" / project
    directory.mkdir(parents=True, exist_ok=False)
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    key_path = directory / "signing.pem"
    key_path.write_bytes(private)
    authority = TokenAuthority(private, "local-sync-probe")
    source_urls = {
        role: make_url(url).set(database=source_name).render_as_string(hide_password=False)
        for role, url in urls.items()
    }
    password = secrets.token_urlsafe(32)
    storage_url = make_url(urls["admin"]).set(
        drivername="postgresql",
        host="postgres",
        port=5432,
        username=storage_role,
        password=password,
        database=storage_name,
    )
    replication_url = make_url(source_urls["replication"]).set(
        drivername="postgresql", host="postgres", port=5432
    )
    config_source = (ROOT / "deploy/powersync/service.yaml").read_text()
    config_source = config_source.replace(
        "  jwks_uri: !env PS_JWKS_URL",
        "  jwks:\n"
        + "\n".join(
            "    " + line for line in yaml.safe_dump({"keys": [authority.jwk]}).splitlines()
        ),
    )
    (directory / "service.yaml").write_text(config_source, encoding="utf-8")
    shutil.copyfile(ROOT / "deploy/powersync/sync-config.yaml", directory / "sync-config.yaml")
    env_path = directory / "compose.env"
    env_path.write_text(
        f"PS_DATA_SOURCE_URI={replication_url.render_as_string(hide_password=False)}\n"
        f"PS_STORAGE_SOURCE_URI={storage_url.render_as_string(hide_password=False)}\n"
        f"APP_DATA_PROBE_CONFIG_DIRECTORY={host_path(directory)}\n",
        encoding="utf-8",
    )
    compose = [
        *docker,
        "compose",
        "-p",
        project,
        "--env-file",
        host_path(env_path),
        "-f",
        host_path(ROOT / "deploy/compose.sync-probe.yaml"),
    ]
    dart_config = directory / "client.json"
    dart_config.write_text(
        json.dumps(
            {
                "directory": str(directory),
                "api": "http://127.0.0.1:18280",
                "multi_entity": True,
                "tokens": {
                    user: authority.issue(user, API_AUDIENCE, seconds=900)
                    for user in ["alice", "bob"]
                },
            }
        ),
        encoding="utf-8",
    )
    admin = make_engine(urls["admin"])
    engines = []
    backend = None
    service_log = None
    started = False
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            cursor = c.connection.driver_connection.cursor()
            cursor.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION PASSWORD {}"
                ).format(sql.Identifier(storage_role), sql.Literal(password))
            )
            cursor.close()
            for name, owner in [(source_name, "app_migrator"), (storage_name, storage_role)]:
                c.exec_driver_sql(f'CREATE DATABASE "{name}" OWNER "{owner}"')
                c.exec_driver_sql(f'REVOKE ALL ON DATABASE "{name}" FROM PUBLIC')
            c.exec_driver_sql(
                f'GRANT CONNECT ON DATABASE "{source_name}" TO app_runtime, app_replication'
            )
        migrate(source_urls["migrator"])
        source = make_engine(source_urls["migrator"])
        engines.append(source)
        with source.begin() as c:
            c.execute(sa.insert(App).values(id="other", name="other"))
            c.execute(
                sa.insert(User), [{"id": user, "display_name": user} for user in ["alice", "bob"]]
            )
            c.execute(
                sa.insert(Dataset),
                [
                    {"id": user + "-data", "owner_id": user, "app_id": "zheri", "name": user}
                    for user in ["alice", "bob"]
                ]
                + [{"id": "other-data", "owner_id": "alice", "app_id": "other", "name": "other"}],
            )
            c.execute(
                sa.insert(DatasetMember),
                [{"dataset_id": user + "-data", "user_id": user} for user in ["alice", "bob"]]
                + [{"dataset_id": "other-data", "user_id": "alice"}],
            )
            c.execute(
                sa.insert(Expense).values(
                    id="other-app-secret",
                    dataset_id="other-data",
                    name="other app",
                    amount="1.00",
                    cycle="monthly",
                    category="",
                    subcategory="",
                    sort_order=0,
                )
            )
        with source.begin() as c:
            c.execute(
                sa.insert(Asset).values(
                    **database_values(
                        "assets",
                        canonical_record(
                            "assets",
                            {
                                "id": "other-app-asset",
                                "name": "other app",
                                "price": "1",
                                "purchaseDate": "2024-01-01",
                                "category": "",
                                "residualValue": "0",
                                "targetDailyCost": "1",
                                "icon": "inventory",
                                "color": 0,
                            },
                        ),
                        "other-data",
                    )
                )
            )
            c.execute(
                sa.insert(ExpenseCategory).values(
                    **database_values(
                        "expense_categories",
                        canonical_record(
                            "expense_categories",
                            {
                                "id": "other-app-category",
                                "name": "other app",
                                "iconId": "custom",
                            },
                        ),
                        "other-data",
                    )
                )
            )
        environment = {
            **os.environ,
            "APP_DATA_AUTH_MODE": "probe",
            "APP_DATA_DATABASE_URL": source_urls["runtime"],
            "APP_DATA_SIGNING_KEY": str(key_path),
            "APP_DATA_ISSUER": authority.issuer,
            "APP_DATA_SYNC_ENDPOINT": "http://127.0.0.1:18290",
        }
        service_log = (directory / "api.log").open("w", encoding="utf-8")
        backend = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app_data_service.api:from_environment",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "18280",
                "--no-access-log",
            ],
            cwd=ROOT,
            env=environment,
            stdout=service_log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        deadline = time.monotonic() + 20
        with httpx.Client(trust_env=False, timeout=2) as http:
            while True:
                if backend.poll() is not None:
                    raise RuntimeError("Probe API exited; inspect the ignored API log")
                try:
                    if http.get("http://127.0.0.1:18280/health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() >= deadline:
                    raise TimeoutError("Probe API startup")
                time.sleep(0.2)
        print("API ready; starting isolated PowerSync service", flush=True)
        started = True
        subprocess.run(
            [*compose, "up", "-d", "--wait", "--wait-timeout", "50", "--pull", "never"],
            check=True,
            timeout=75,
        )
        print("PowerSync healthy; running independent Dart clients", flush=True)
        result = subprocess.run(
            [args.dart, "run", "bin/live_probe.dart", str(dart_config)],
            cwd=ROOT / "experiments/powersync_probe",
            timeout=180,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        (directory / "dart.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        if result.returncode:
            raise RuntimeError("Dart live probe failed; inspect the ignored dart.log")
        summary = next(
            json.loads(line) for line in result.stdout.splitlines() if line.startswith('{"passed":')
        )
        print(json.dumps(summary), flush=True)
        (directory / "result.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        with source.connect() as c:
            check = c.exec_driver_sql(
                "SELECT row_version, deleted_at IS NOT NULL "
                "FROM zheri.expenses WHERE id='live-existing-id'"
            ).one()
            if tuple(check) != (5, True):
                raise RuntimeError("Unexpected final PostgreSQL state")
        print("PostgreSQL final version=5, soft deleted; no production data touched", flush=True)
    finally:
        if started:
            try:
                with (directory / "powersync.log").open("w", encoding="utf-8") as log:
                    subprocess.run(
                        [*compose, "logs", "--no-color"],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=15,
                        check=False,
                    )
            except subprocess.SubprocessError:
                print("Probe log capture failed: " + project, flush=True)
            try:
                subprocess.run([*compose, "down", "--timeout", "10"], timeout=30, check=True)
            except subprocess.SubprocessError:
                print("Probe container cleanup needs inspection: " + project, flush=True)
        if backend is not None:
            backend.terminate()
            try:
                backend.wait(timeout=10)
            except subprocess.TimeoutExpired:
                backend.kill()
                backend.wait(timeout=5)
        if service_log is not None:
            service_log.close()
        for engine in engines:
            engine.dispose()
        # Identifiers are generated internally, validated again before drops.
        assert re.fullmatch(r"app_data_sync_[a-f0-9]{12}", source_name)
        assert re.fullmatch(r"app_data_buckets_[a-f0-9]{12}", storage_name)
        assert re.fullmatch(r"probe_store_[a-f0-9]{12}", storage_role)
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as c:
            slots = (
                c.execute(
                    sa.text("SELECT slot_name FROM pg_replication_slots WHERE database=:name"),
                    {"name": source_name},
                )
                .scalars()
                .all()
            )
            for slot in slots:
                c.execute(sa.text("SELECT pg_drop_replication_slot(:slot)"), {"slot": slot})
            for name in [source_name, storage_name]:
                c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            c.exec_driver_sql(f'DROP ROLE IF EXISTS "{storage_role}"')
        admin.dispose()
        print("Cleaned this run's containers, databases, role and replication slots", flush=True)


if __name__ == "__main__":
    run()
