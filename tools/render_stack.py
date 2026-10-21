"""Generate a private, self-contained Synology test stack. Never contacts NAS."""

import argparse
import base64
import json
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from render_synology import compose
from sqlalchemy.engine import URL

from app_data_service.dsm import DsmProvider

ROOT = Path(__file__).resolve().parents[1]


def api_start_command():
    """Migrate first, then replace the shell with the long-running API process."""
    return [
        "/bin/sh",
        "-ec",
        'APP_DATA_DATABASE_URL="$$APP_DATA_MIGRATION_URL" app-data migrate\n'
        "unset APP_DATA_MIGRATION_URL\n"
        "exec python -m uvicorn app_data_service.api:from_environment --factory "
        "--host 0.0.0.0 --port 8000 --no-access-log --no-proxy-headers",
    ]


def stack(
    data_directory,
    origin,
    dsm_origin,
    *,
    api_image="app-data-service:0.2.0-test",
    port=18280,
    allowed_origins="",
    secrets_bundle=None,
    dsm_identity_origin=None,
    dsm_lan_ip=None,
):
    parsed = urlsplit(origin)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Public service origin required")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("Remote service requires HTTPS")
    DsmProvider.validate_origin(dsm_origin)
    if not 1024 <= port <= 65535:
        raise ValueError("Unprivileged gateway port required")
    passwords = secrets_bundle or {
        role: secrets.token_urlsafe(32)
        for role in ("ADMIN", "MIGRATOR", "RUNTIME", "REPLICATION", "STORAGE")
    }
    configuration = compose(data_directory, passwords)
    configuration["name"] = "app-data-service-test"
    postgres = configuration["services"]["postgres"]
    postgres["environment"]["APP_STORAGE_PASSWORD"] = passwords["STORAGE"]
    postgres["environment"]["APP_BOOTSTRAP_SQL"] += """
\\getenv storage_password APP_STORAGE_PASSWORD
CREATE ROLE app_sync_storage LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  NOBYPASSRLS PASSWORD :'storage_password';
CREATE DATABASE powersync_storage OWNER app_sync_storage;
REVOKE ALL ON DATABASE powersync_storage FROM PUBLIC;
"""

    def url(role, *, driver="postgresql+psycopg", database="app_data"):
        return URL.create(
            driver,
            username="app_" + role.lower(),
            password=passwords[role],
            host="postgres",
            port=5432,
            database=database,
        ).render_as_string(hide_password=False)

    private = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    environment = {
        "APP_DATA_DATABASE_URL": url("RUNTIME"),
        "APP_DATA_MIGRATION_URL": url("MIGRATOR"),
        "APP_DATA_SIGNING_KEY_B64": base64.b64encode(private).decode(),
        "APP_DATA_ISSUER": origin.rstrip("/"),
        "APP_DATA_DSM_ORIGIN": dsm_origin.rstrip("/"),
        "APP_DATA_DSM_IDENTITY_ORIGIN": dsm_identity_origin or dsm_origin.rstrip("/"),
        "APP_DATA_AUTH_MODE": "dsm",
        "APP_DATA_SYNC_ENDPOINT": origin.rstrip("/") + "/powersync/",
        "APP_DATA_POWERSYNC_UPSTREAM": "http://powersync:8080",
        "APP_DATA_ALLOWED_ORIGINS": allowed_origins,
    }
    configuration["services"]["api"] = {
        "image": api_image,
        "command": api_start_command(),
        "restart": "unless-stopped",
        "environment": environment,
        "ports": [f"{port}:8000"],
        "depends_on": {"postgres": {"condition": "service_healthy"}},
        "mem_limit": "384m",
        "healthcheck": {
            "test": [
                "CMD",
                "python",
                "-c",
                "import urllib.request; "
                "urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)",
            ],
            "interval": "5s",
            "timeout": "3s",
            "retries": 12,
        },
    }
    if dsm_lan_ip:
        import ipaddress

        ipaddress.ip_address(dsm_lan_ip)
        configuration["services"]["api"]["extra_hosts"] = [
            f"{urlsplit(dsm_origin).hostname}:{dsm_lan_ip}"
        ]
    powersync = (
        (ROOT / "deploy/powersync/service.yaml")
        .read_text()
        .replace("!env PS_DATA_SOURCE_URI", json.dumps(url("REPLICATION", driver="postgresql")))
    )
    powersync = powersync.replace(
        "!env PS_STORAGE_SOURCE_URI",
        json.dumps(
            URL.create(
                "postgresql",
                username="app_sync_storage",
                password=passwords["STORAGE"],
                host="postgres",
                port=5432,
                database="powersync_storage",
            ).render_as_string(hide_password=False)
        ),
    )
    powersync = powersync.replace("!env PS_JWKS_URL", "http://api:8000/.well-known/jwks.json")
    powersync = powersync.replace(
        "  path: sync-config.yaml",
        "  content: |\n"
        + "\n".join(
            "    " + line
            for line in (ROOT / "deploy/powersync/sync-config.yaml").read_text().splitlines()
        ),
    )
    configuration["services"]["powersync"] = {
        "image": "journeyapps/powersync-service:1.27.0",
        "restart": "unless-stopped",
        "command": ["start", "-r", "unified"],
        "environment": {
            "POWERSYNC_CONFIG_B64": base64.b64encode(powersync.encode()).decode(),
            "NODE_OPTIONS": "--max-old-space-size=400",
        },
        "depends_on": {"api": {"condition": "service_healthy"}},
        "mem_limit": "512m",
        "healthcheck": {
            "test": [
                "CMD",
                "node",
                "-e",
                "fetch('http://localhost:8080/probes/liveness').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))",
            ],
            "interval": "5s",
            "timeout": "2s",
            "retries": 12,
        },
    }
    for service in configuration["services"].values():
        service["platform"] = "linux/amd64"
    return configuration


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-directory", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--dsm-origin", required=True)
    parser.add_argument("--api-image", default="app-data-service:0.2.0-test")
    parser.add_argument("--port", type=int, default=18280)
    parser.add_argument("--allowed-origins", default="")
    parser.add_argument("--dsm-identity-origin")
    parser.add_argument("--dsm-lan-ip")
    parser.add_argument("--output", type=Path, default=ROOT / "deploy/compose.synology-test.yaml")
    args = parser.parse_args()
    if args.output.exists():
        parser.exit(1, "Existing deployment retained; refusing to rotate credentials.\n")
    result = stack(
        args.data_directory,
        args.origin,
        args.dsm_origin,
        api_image=args.api_image,
        port=args.port,
        allowed_origins=args.allowed_origins,
        dsm_identity_origin=args.dsm_identity_origin,
        dsm_lan_ip=args.dsm_lan_ip,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as file:
        yaml.safe_dump(result, file, allow_unicode=True, sort_keys=False)
    args.output.chmod(0o600)
    print("Generated private test Compose; no NAS changes made")


if __name__ == "__main__":
    main()
