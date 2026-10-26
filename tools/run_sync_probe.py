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
from render_sync_config import render as render_sync_config
from sqlalchemy.engine import make_url

from app_data_service.applications import ApplicationRegistry
from app_data_service.auth import API_AUDIENCE, SYNC_AUDIENCE, TokenAuthority
from app_data_service.cli import local_config, migrate
from app_data_service.config import make_engine
from app_data_service.models import App, Dataset, DatasetMember, User
from app_data_service.modules.registry import installed_applications
from app_data_service.modules.zheri.entities import canonical_record, database_values
from app_data_service.modules.zheri.models import Asset, Expense, ExpenseCategory

ROOT = Path(__file__).resolve().parents[1]
NOTE_SENTINEL = "10000000-0000-4000-8000-000000000001"


def check_application_streams(authority):
    """Black-box the pinned 1.27.0 NDJSON endpoint, including forged client parameters.

    Protocol: packages/service-core/src/util/protocol-types.ts and
    routes/endpoints/sync-stream.ts in powersync-service v1.27.0.
    This is a bounded diagnostic, never a replacement client sync engine.
    """

    def download(app_id, actor="alice", *, explicit=False, token=None):
        token = token or authority.issue(actor, SYNC_AUDIENCE, app_id=app_id)
        body = {
            "buckets": [],
            "raw_data": False,
            "parameters": {"app_id": "zheri", "user_id": "alice"},
        }
        if explicit:
            body["streams"] = {
                "include_defaults": True,
                "subscriptions": [
                    {
                        "stream": "private_expenses",
                        "parameters": {"app_id": "zheri"},
                        "override_priority": None,
                    }
                ],
            }
        objects, total = set(), 0
        with (
            httpx.Client(trust_env=False, timeout=10) as client,
            client.stream(
                "POST",
                "http://127.0.0.1:18290/sync/stream",
                json=body,
                headers={"Authorization": "Token " + token, "Accept": "application/x-ndjson"},
            ) as response,
        ):
            if response.status_code != 200:
                raise RuntimeError(f"PowerSync stream rejected: {response.status_code}")
            deadline = time.monotonic() + 20
            for line in response.iter_lines():
                total += len(line)
                if total > 2 * 1024 * 1024 or time.monotonic() > deadline:
                    raise TimeoutError("Application stream probe budget exceeded")
                value = json.loads(line)
                for entry in value.get("data", {}).get("data", []):
                    if entry.get("op") == "PUT":
                        objects.add(entry["object_id"])
                if "checkpoint_complete" in value:
                    return objects
        raise RuntimeError("PowerSync closed before a complete checkpoint")

    deadline = time.monotonic() + 35
    while "scope-zheri-only" not in download("zheri"):
        if time.monotonic() > deadline:
            raise TimeoutError("Application stream replication startup")
        time.sleep(0.2)
    assert download("zheri") == {"scope-zheri-only"}, "Other module leaked into Zheri"
    deadline = time.monotonic() + 35
    while NOTE_SENTINEL not in download("reading_notes"):
        if time.monotonic() >= deadline:
            raise TimeoutError("Third-application replication startup")
        time.sleep(0.2)
    assert download("reading_notes") == {NOTE_SENTINEL}, "Other app leaked into notes"
    assert download("reading_notes", explicit=True) == {NOTE_SENTINEL}, (
        "Subscription escaped notes scope"
    )
    assert download("reading_notes", "bob") == set(), "Other user's notes leaked"
    assert download("board_game_agent") == set(), "Cross-application data leaked"
    assert download("board_game_agent", explicit=True) == set(), "Subscription escaped app scope"
    assert download("zheri", "bob") == set(), "Cross-user data leaked"
    claims = authority.claims(
        authority.issue("alice", SYNC_AUDIENCE, app_id="zheri"), audience=SYNC_AUDIENCE
    )
    del claims["app_id"]
    import jwt

    unscoped = jwt.encode(claims, authority.key, algorithm="RS256", headers={"kid": authority.kid})
    assert download("zheri", token=unscoped) == set(), "Unscoped token received app data"
    return {
        "passed": [
            "zheri_signed_app_download",
            "board_token_no_zheri_download",
            "forged_parameters_and_explicit_subscription_rejected",
            "other_user_no_download",
            "unscoped_token_no_download",
            "third_application_own_stream_download",
            "third_application_rejects_cross_app_subscription",
            "third_application_cross_user_isolation",
        ],
        "platform": "powersync-service-1.27.0",
    }


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--docker", choices=["native", "wsl"], default="wsl" if os.name == "nt" else "native"
    )
    parser.add_argument("--dart", default=shutil.which("dart") or "dart")
    parser.add_argument("--application-scope-only", action="store_true")
    parser.add_argument(
        "--board-flutter",
        type=Path,
        help="Run the board game's actual Flutter PowerSync store tests",
    )
    args = parser.parse_args()
    registry = installed_applications()
    install_notes = None
    if args.application_scope_only:
        # A fixed test fixture proves extension without modifying installed production modules.
        sys.path.insert(0, str(ROOT / "tests"))
        from notes_application import definition as notes_definition
        from notes_application import install as install_notes

        registry = ApplicationRegistry((*registry.definitions, notes_definition))
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
    (directory / "sync-config.yaml").write_text(render_sync_config(registry), encoding="utf-8")
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
                "full_business": True,
                "tokens": {
                    user: authority.issue(
                        user,
                        API_AUDIENCE,
                        seconds=900,
                        app_id="board_game_agent" if args.board_flutter else "zheri",
                    )
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
            c.execute(sa.insert(App).values(id="other", name="other", schema_name="other"))
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
            if args.board_flutter:
                c.execute(
                    sa.insert(Dataset),
                    [
                        {
                            "id": "board-" + user,
                            "owner_id": user,
                            "app_id": "board_game_agent",
                            "name": user,
                        }
                        for user in ("alice", "bob")
                    ],
                )
                c.execute(
                    sa.insert(DatasetMember),
                    [{"dataset_id": "board-" + user, "user_id": user} for user in ("alice", "bob")],
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
            if args.application_scope_only:
                install_notes(c)
                c.execute(
                    sa.insert(Dataset).values(
                        id="notes-data", owner_id="alice", app_id="reading_notes", name="notes"
                    )
                )
                c.execute(sa.insert(DatasetMember).values(dataset_id="notes-data", user_id="alice"))
                c.execute(
                    sa.text(
                        "INSERT INTO reading_notes.notes(id,dataset_id,title,body) "
                        "VALUES(:id,'notes-data','module sentinel','private notes')"
                    ),
                    {"id": NOTE_SENTINEL},
                )
                c.execute(
                    sa.insert(Expense).values(
                        id="scope-zheri-only",
                        dataset_id="alice-data",
                        name="scope sentinel",
                        amount="1",
                        cycle="monthly",
                        category="",
                        subcategory="",
                        sort_order=0,
                    )
                )
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
        if args.board_flutter:
            environment.update(
                APP_DATA_SYNC_ENDPOINT="http://127.0.0.1:18280/powersync",
                APP_DATA_POWERSYNC_UPSTREAM="http://127.0.0.1:18290",
            )
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
        if args.application_scope_only:
            print("PowerSync healthy; checking signed application streams", flush=True)
            summary = check_application_streams(authority)
            print(json.dumps(summary), flush=True)
            (directory / "result.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            return
        if args.board_flutter:
            board = ROOT.parent.parent / "board-game-agent"
            result = subprocess.run(
                [
                    str(args.board_flutter),
                    "test",
                    "test/probes/board_live_probe.dart",
                    "--reporter",
                    "expanded",
                    "--dart-define=BOARD_PROBE_CONFIG=" + str(dart_config),
                ],
                cwd=board,
                timeout=180,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            (directory / "board.log").write_text(result.stdout + result.stderr, encoding="utf-8")
            if result.returncode:
                raise RuntimeError("Board live probe failed; inspect the ignored board.log")
            print(
                "Board actual store: offline convergence, preferences, chat and isolation passed",
                flush=True,
            )
            (directory / "result.json").write_text(
                json.dumps({"board_sdk_passed": True}), encoding="utf-8"
            )
            return
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
