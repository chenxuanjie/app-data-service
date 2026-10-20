"""Validate the full imported stack against a strict TLS DSM substitute."""

import argparse
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from render_stack import stack

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-image", default="app-data-service:0.1.0-test")
    parser.add_argument("--dart", default=shutil.which("dart") or "dart")
    parser.add_argument(
        "--docker", choices=["native", "wsl"], default="wsl" if os.name == "nt" else "native"
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    docker = ["wsl", "-d", "Ubuntu", "--exec", "docker"] if args.docker == "wsl" else ["docker"]

    def linux(path):
        return (
            subprocess.check_output(
                ["wsl", "-d", "Ubuntu", "--exec", "wslpath", "-a", str(path)], text=True, timeout=10
            ).strip()
            if args.docker == "wsl"
            else str(path)
        )

    directory = ROOT / ".local" / ("stack-" + uuid4().hex[:12])
    directory.mkdir(parents=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "dsm.test")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=5))
        .not_valid_after(datetime.now(UTC) + timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("dsm.test")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    (directory / "ca.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (directory / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    configuration = stack(
        "/volume2/friend/apps/app_data_service_test/postgres",
        "http://127.0.0.1:18380",
        "https://dsm.test:9443",
        api_image=args.api_image,
    )
    configuration["name"] = directory.name
    configuration["services"]["postgres"]["volumes"] = ["test_pgdata:/var/lib/postgresql/data"]
    configuration["volumes"] = {"test_pgdata": {}}
    api = configuration["services"]["api"]
    api["ports"] = ["127.0.0.1:18380:8000"]
    api["volumes"] = [f"{linux(directory / 'ca.pem')}:/tls/ca.pem:ro"]
    api["environment"]["APP_DATA_DSM_CA"] = "/tls/ca.pem"
    configuration["services"]["mock-dsm"] = {
        "image": args.api_image,
        "command": ["python", "/fixture/mock_dsm.py", "/tls/ca.pem", "/tls/key.pem"],
        "volumes": [
            f"{linux(directory)}:/tls:ro",
            f"{linux(ROOT / 'tests/fixtures/mock_dsm.py')}:/fixture/mock_dsm.py:ro",
        ],
        "networks": {"default": {"aliases": ["dsm.test"]}},
    }
    compose_path = directory / "compose.yaml"
    compose_path.write_text(
        yaml.safe_dump(configuration, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    compose = [*docker, "compose", "-p", directory.name, "-f", linux(compose_path)]
    report = {"mode": "full Docker stack with strict TLS DSM substitute", "real_nas_tested": False}
    started = False
    try:
        started = True
        subprocess.run(
            [*compose, "up", "-d", "--wait", "--wait-timeout", "60", "--pull", "never", "postgres"],
            check=True,
            timeout=80,
        )
        failed_start = subprocess.run(
            [
                *compose,
                "run",
                "--rm",
                "-T",
                "--no-deps",
                "-e",
                "APP_DATA_MIGRATION_URL=postgresql+psycopg://app_migrator:fixture-invalid@postgres:5432/app_data",
                "api",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        (directory / "migration-failure.log").write_text(
            failed_start.stdout + failed_start.stderr, encoding="utf-8"
        )
        assert failed_start.returncode != 0
        assert "Database operation failed" in failed_start.stderr
        assert "Uvicorn running" not in failed_start.stderr
        report["migration_failure_prevents_api_start"] = True
        subprocess.run(
            [*compose, "up", "-d", "--wait", "--wait-timeout", "90", "--pull", "never"],
            check=True,
            timeout=110,
        )
        services = subprocess.check_output(
            [*compose, "ps", "--services", "--filter", "status=running"], text=True, timeout=15
        ).splitlines()
        assert set(services) == {"postgres", "api", "powersync", "mock-dsm"}
        all_services = subprocess.check_output(
            [*compose, "ps", "--all", "--services"], text=True, timeout=15
        ).splitlines()
        assert set(all_services) == set(services)
        report["production_services_all_running"] = ["postgres", "api", "powersync"]
        subprocess.run(
            [
                *compose,
                "exec",
                "-T",
                "api",
                "python",
                "-c",
                "from pathlib import Path; "
                "assert not any(v.startswith(b'APP_DATA_MIGRATION_URL=') "
                "for v in Path('/proc/1/environ').read_bytes().split(b'\\0'))",
            ],
            check=True,
            timeout=15,
        )
        report["migration_credentials_removed_from_api_process"] = True
        with httpx.Client(trust_env=False, timeout=35) as client:
            users = {}
            for account in ["alice", "bob"]:
                response = client.post(
                    "http://127.0.0.1:18380/v1/auth/login",
                    json={"account": account, "password": "fixture-password"},
                )
                response.raise_for_status()
                users[account] = response.json()
            assert users["alice"]["dataset_id"] != users["bob"]["dataset_id"]
            assert client.get("http://127.0.0.1:18380/v1/datasets").status_code == 401
            config = {
                "directory": str(directory),
                "api": "http://127.0.0.1:18380",
                "tokens": {name: result["access_token"] for name, result in users.items()},
                "datasets": {name: result["dataset_id"] for name, result in users.items()},
            }
            client_path = directory / "client.json"
            client_path.write_text(json.dumps(config), encoding="utf-8")
            result = subprocess.run(
                [args.dart, "run", "bin/live_probe.dart", str(client_path)],
                cwd=ROOT / "experiments/powersync_probe",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            (directory / "dart.log").write_text(result.stdout + result.stderr, encoding="utf-8")
            if result.returncode:
                raise RuntimeError("Live sync failed; inspect ignored dart.log")
            report.update(
                next(
                    json.loads(line)
                    for line in result.stdout.splitlines()
                    if line.startswith('{"passed":')
                )
            )
            # A database container replacement must retain the sample tombstone.
            subprocess.run(
                [
                    *compose,
                    "up",
                    "-d",
                    "--force-recreate",
                    "--no-deps",
                    "postgres",
                    "--wait",
                    "--wait-timeout",
                    "40",
                ],
                check=True,
                timeout=55,
            )
            stored = subprocess.check_output(
                [
                    *compose,
                    "exec",
                    "-T",
                    "postgres",
                    "psql",
                    "-U",
                    "app_admin",
                    "-d",
                    "app_data",
                    "-tAc",
                    "SELECT row_version || ':' || (deleted_at IS NOT NULL)::text "
                    "FROM zheri.expenses WHERE id='live-existing-id'",
                ],
                text=True,
                timeout=15,
            ).strip()
            assert stored == "5:true"
            report["database_container_recreation_retains_data"] = True
            # API sessions survive container replacement as well.
            subprocess.run(
                [
                    *compose,
                    "up",
                    "-d",
                    "--force-recreate",
                    "--no-deps",
                    "api",
                    "--wait",
                    "--wait-timeout",
                    "40",
                ],
                check=True,
                timeout=55,
            )
            refreshed = client.post(
                "http://127.0.0.1:18380/v1/auth/refresh",
                json={"refresh_token": users["alice"]["refresh_token"]},
            )
            assert refreshed.status_code == 200
            report["api_restart_retains_session_and_signing_key"] = True
            logout = client.post(
                "http://127.0.0.1:18380/v1/auth/logout",
                json={"refresh_token": users["alice"]["refresh_token"]},
            )
            assert logout.status_code == 200
            assert (
                client.get(
                    "http://127.0.0.1:18380/v1/datasets",
                    headers={"Authorization": "Bearer " + refreshed.json()["access_token"]},
                ).status_code
                == 401
            )
            report["logout_revokes_api_access"] = True
        (directory / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report), flush=True)
    finally:
        if started:
            with (directory / "docker.log").open("w", encoding="utf-8") as log:
                subprocess.run(
                    [*compose, "logs", "--no-color"],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=20,
                    check=False,
                )
            # Only this internally generated stack's disposable named volume.
            subprocess.run(
                [*compose, "down", "--volumes", "--timeout", "10"], check=True, timeout=40
            )


if __name__ == "__main__":
    main()
