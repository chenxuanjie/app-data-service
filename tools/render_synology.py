"""Render a self-contained Compose file with local, never-printed credentials."""

import argparse
import re
import secrets
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def compose(data_directory: str, passwords: dict[str, str]) -> dict:
    if not re.fullmatch(r"/volume[0-9]+/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+", data_directory):
        raise ValueError("Use an absolute Synology volume directory without shell substitutions")
    if any(part in (".", "..") for part in data_directory.split("/")):
        raise ValueError("Relative path components are not allowed")
    sql = (ROOT / "deploy" / "postgres" / "001-roles.sql").read_text(encoding="utf-8")
    service = {
        "image": "postgres:17.11-bookworm",
        "restart": "unless-stopped",
        "environment": {
            "POSTGRES_DB": "app_data",
            "POSTGRES_USER": "app_admin",
            "POSTGRES_PASSWORD": passwords["ADMIN"],
            "POSTGRES_INITDB_ARGS": "--auth-host=scram-sha-256 --encoding=UTF8",
            "APP_MIGRATOR_PASSWORD": passwords["MIGRATOR"],
            "APP_RUNTIME_PASSWORD": passwords["RUNTIME"],
            "APP_REPLICATION_PASSWORD": passwords["REPLICATION"],
            "APP_BOOTSTRAP_SQL": sql,
        },
        "entrypoint": ["/bin/bash", "-ec"],
        "command": [
            'printf "%s\\n" "$$APP_BOOTSTRAP_SQL" '
            "> /docker-entrypoint-initdb.d/001-roles.sql\n"
            "unset APP_BOOTSTRAP_SQL\n"
            "exec /usr/local/bin/docker-entrypoint.sh postgres "
            "-c wal_level=logical -c max_replication_slots=4 -c max_wal_senders=4 "
            "-c max_slot_wal_keep_size=256MB -c shared_buffers=64MB -c max_connections=30\n"
        ],
        "volumes": [
            {
                "type": "bind",
                "source": data_directory,
                "target": "/var/lib/postgresql/data",
                "bind": {"create_host_path": False},
            }
        ],
        "healthcheck": {
            "test": ["CMD-SHELL", "pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB"],
            "interval": "5s",
            "timeout": "3s",
            "retries": 12,
        },
        "mem_limit": "512m",
    }
    return {"name": "app-data-service", "services": {"postgres": service}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-directory", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "deploy" / "compose.synology.yaml")
    args = parser.parse_args()
    if args.output.exists():
        parser.exit(
            1, "Existing deployment configuration retained; refusing to rotate credentials.\n"
        )
    result = compose(
        args.data_directory,
        {
            role: secrets.token_urlsafe(32)
            for role in ("ADMIN", "MIGRATOR", "RUNTIME", "REPLICATION")
        },
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8", newline="\n") as file:
        yaml.safe_dump(result, file, allow_unicode=True, sort_keys=False)
    args.output.chmod(0o600)
    print("Created self-contained Compose. Store it privately; no NAS changes were made.")


if __name__ == "__main__":
    main()
