"""Small maintenance CLI. Never prints database URLs or credentials."""

import argparse
import json
import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy.exc import DBAPIError

from app_data_service.config import database_url, make_engine

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


def inspect_database(url: str, *, applications=None) -> dict:
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
            from .modules.registry import installed_applications

            applications = applications or installed_applications()
            expected = applications.publication_tables
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
    commands.add_parser(
        "register-applications", help="Register installed modules after schema migrations"
    )
    commands.add_parser("check")
    from .modules.registry import installed_applications

    registry = installed_applications()
    for definition in registry.definitions:
        if definition.cli_factory:
            definition.cli_factory(commands)
    args = parser.parse_args()
    try:
        urls = None
        if getattr(args, "needs_database", True):
            urls = (
                local_config()["urls"]
                if args.local
                else {"migrator": database_url(), "runtime": database_url()}
            )
        if hasattr(args, "handler"):
            output = args.handler(args, urls)
        elif args.command == "migrate":
            migrate(urls["migrator"])
            output = {"migration": "head", "completed": True}
        elif args.command == "register-applications":
            from .registration import register_application

            engine = make_engine(urls["migrator"])
            try:
                with engine.begin() as connection:
                    connection.exec_driver_sql("SET LOCAL transaction_timeout = '20s'")
                    for definition in registry.definitions:
                        register_application(connection, definition)
                output = {"registered": [d.id for d in registry.definitions]}
            finally:
                engine.dispose()
        else:
            output = inspect_database(urls["migrator"], applications=registry)
        print(json.dumps(output, ensure_ascii=False))
    except DBAPIError as error:
        state = getattr(error.orig, "sqlstate", "unknown")
        parser.exit(1, f"Database operation failed (SQLSTATE {state}); no credentials printed.\n")
    except (ValueError, PermissionError, OSError) as error:
        # Known input/config errors contain fixed labels, not URLs or private record values.
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
