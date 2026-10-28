"""Run development checks with a WSL lease, DB preflight and durable stage logs."""

import argparse
import os
import socket
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path

from docker_runtime import docker_runtime
from sqlalchemy.engine import make_url
from task_diagnostics import TaskLog
from wsl_port_relay import database_transport

from app_data_service.cli import local_config
from app_data_service.config import make_engine

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", choices=["wsl"], default="wsl")
    parser.add_argument("--database-preflight", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.name != "nt" or sys.prefix == sys.base_prefix:
        parser.error(
            "Use the Windows project .venv Python; Docker runs in Ubuntu WSL. "
            "No automatic runtime switching."
        )
    if args.database_preflight:
        try:
            url = local_config()["urls"]["admin"]
            parsed = make_url(url)
            with socket.create_connection((parsed.host, parsed.port or 5432), timeout=15):
                pass
            engine = make_engine(url)
            try:
                with engine.connect() as connection:
                    connection.exec_driver_sql("SELECT 1")
            finally:
                engine.dispose()
        except Exception as error:
            print(
                f"PostgreSQL preflight failed: {type(error).__name__}; "
                "check local WSL/port/authentication."
            )
            return 2
        print("PostgreSQL connection and authentication passed.")
        return 0
    log = TaskLog(ROOT, "checks")
    try:
        with docker_runtime(args.docker), ExitStack() as transport:
            log.run(
                "local-environment-ready",
                [sys.executable, str(ROOT / "tools/local_environment.py"), "--purpose", "test"],
                timeout=900,
                cwd=ROOT,
            )
            database = make_url(local_config()["urls"]["admin"])
            transport.enter_context(database_transport(database.host, database.port or 5432))
            docker = (
                ["wsl", "-d", "Ubuntu", "--exec", "docker"] if args.docker == "wsl" else ["docker"]
            )
            log.run(
                "docker-preflight",
                [*docker, "info", "--format", "{{.OSType}}"],
                timeout=120,
                cwd=ROOT,
            )
            log.run(
                "postgres-preflight",
                [sys.executable, str(Path(__file__).resolve()), "--database-preflight"],
                timeout=120,
                cwd=ROOT,
            )
            for name, command, deadline in [
                ("ruff-check", ["ruff", "check", "."], 600),
                ("ruff-format", ["ruff", "format", "--check", "."], 600),
                ("pytest", ["pytest", "-q", "-x", "--tb=short"], 7200),
            ]:
                log.run(name, [sys.executable, "-m", *command], timeout=deadline, cwd=ROOT)
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError) as error:
        if isinstance(error, RuntimeError):
            print(str(error), file=sys.stderr)
        print(f"Checks stopped: {type(error).__name__}. See {log.directory}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
