"""Validate the full imported stack against a strict TLS DSM substitute."""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
import yaml
from configure_public_releases import configure as configure_releases
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from docker_runtime import docker_runtime
from render_stack import stack

ROOT = Path(__file__).resolve().parents[1]


def capture_stack_state(docker, compose):
    """Inspect only this test stack, never export Config.Env or credentials."""
    try:
        identifiers = subprocess.check_output(
            [*compose, "ps", "--all", "--quiet"], text=True, timeout=15
        ).splitlines()
        if not identifiers:
            return {"containers": []}
        template = (
            '{"name":{{json .Name}},"image":{{json .Config.Image}},'
            '"status":{{json .State.Status}},"exitCode":{{.State.ExitCode}},'
            '"oomKilled":{{.State.OOMKilled}},"restartCount":{{.RestartCount}},'
            '"startedAt":{{json .State.StartedAt}},"finishedAt":{{json .State.FinishedAt}}}'
        )
        output = subprocess.check_output(
            [*docker, "inspect", "--format", template, *identifiers], text=True, timeout=15
        )
        return {"containers": [json.loads(line) for line in output.splitlines()]}
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return {"diagnosticError": type(error).__name__}


def verification_stack(api_image, release_key="isolated-fixture-release-key-not-production"):
    config = stack(
        "/volume2/friend/apps/app_data_service_test/postgres",
        "http://127.0.0.1:18380",
        "https://dsm.test:9443",
        api_image=api_image,
        application_settings={
            "zheri": {
                "browser_origin": "http://127.0.0.1:18380",
                "file_roots": {"updates": "/fixture/updates"},
            },
            "board_game_agent": {"browser_origin": "http://localhost:18380"},
        },
    )
    configure_releases(config, "board_game_agent", hashlib.sha256(release_key.encode()).hexdigest())
    return config


def verify_public_release(client, base, key, artifact_path=None):
    """Exercise public release IO with a fixture or an explicitly selected local APK."""
    prefix = base + "/v1/apps/board_game_agent/releases"
    if artifact_path:
        with artifact_path.open("rb") as content:
            digest = hashlib.file_digest(content, "sha256").hexdigest()
        size = artifact_path.stat().st_size
    else:
        data = b"PK\x03\x04" + b"isolated-release-fixture" * 4096
        digest, size = hashlib.sha256(data).hexdigest(), len(data)
    headers = {"X-Release-Publish-Key": key}
    assert client.get(prefix + "/manifest?platform=android&architecture=arm64").status_code == 404
    assert client.put(prefix + "/objects/" + digest, content=b"x").status_code == 403
    if artifact_path:

        def chunks():
            with artifact_path.open("rb") as content:
                while chunk := content.read(256 * 1024):
                    yield chunk

        response = client.put(
            prefix + "/objects/" + digest,
            content=chunks(),
            headers={**headers, "Content-Length": str(size)},
        )
    else:
        response = client.put(prefix + "/objects/" + digest, content=data, headers=headers)
    assert response.status_code == 200, "Installer streaming upload failed"
    assert response.json() == {"sha256": digest, "size": size, "verified": True}
    assert client.get(prefix + "/artifacts/" + digest + ".apk").status_code == 404
    payload = {
        "schemaVersion": 1,
        "appId": "board_game_agent",
        "channel": "stable",
        "versionName": "1.0.0",
        "buildNumber": 1,
        "minimumSupportedBuild": 0,
        "forceUpdate": False,
        "publishedAt": datetime.now(UTC).isoformat(),
        "releaseNotes": {},
        "artifacts": [
            {
                "platform": "android",
                "architecture": "any",
                "path": "probe.apk",
                "size": size,
                "sha256": digest,
                "mimeType": "application/vnd.android.package-archive",
            }
        ],
    }
    assert client.post(prefix + "/publish", json=payload).status_code == 403
    response = client.post(prefix + "/publish", json=payload, headers=headers)
    assert response.status_code == 200, "SQL release publication failed"
    published = response.json()
    for architecture in ("any", "arm64", "arm", "x64", "x86"):
        result = client.get(
            prefix + "/manifest",
            params={
                "platform": "android",
                "architecture": architecture,
            },
        )
        assert result.status_code == 200 and result.json() == published
    artifact = prefix + "/artifacts/" + digest + ".apk"
    head = client.head(artifact)
    assert head.status_code == 200 and int(head.headers["content-length"]) == size
    assert head.headers["etag"] == '"' + digest + '"'
    checksum, received = hashlib.sha256(), 0
    with client.stream("GET", artifact) as downloaded:
        assert downloaded.status_code == 200
        for chunk in downloaded.iter_bytes():
            checksum.update(chunk)
            received += len(chunk)
    assert received == size and checksum.hexdigest() == digest
    partial = client.get(artifact, headers={"Range": "bytes=0-31"})
    assert partial.status_code == 206 and len(partial.content) == 32
    return {
        "streamUpload": True,
        "anonymousWriteDenied": True,
        "unpublishedObjectPrivate": True,
        "genericAnySelection": True,
        "anonymousDownloadSha256": True,
        "range": True,
        "size": size,
        "sha256": digest,
    }


def write_report(report, directory, destination):
    (directory / "result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if destination:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-image", default="app-data-service:0.5.1-test")
    parser.add_argument("--dart", default=shutil.which("dart") or "dart")
    parser.add_argument(
        "--docker", choices=["native", "wsl"], default="wsl" if os.name == "nt" else "native"
    )
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--release-artifact",
        type=Path,
        help="Use this local APK for isolated release upload/download validation",
    )
    parser.add_argument(
        "--smoke-only",
        action="store_true",
        help="Startup and artifact checks without business tests",
    )
    args = parser.parse_args()
    with docker_runtime(args.docker):
        verify(args)


def verify(args):
    docker = ["wsl", "-d", "Ubuntu", "--exec", "docker"] if args.docker == "wsl" else ["docker"]
    subprocess.run(
        [
            *docker,
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python",
            args.api_image,
            "-c",
            "from pathlib import Path; import os; "
            "assert not Path('/app/web').exists(); "
            "assert 'APP_DATA_WEB_DIRECTORY' not in os.environ",
        ],
        check=True,
        timeout=20,
    )

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
    release_key = secrets.token_urlsafe(48)
    configuration = verification_stack(args.api_image, release_key)
    configuration["name"] = directory.name
    configuration["services"]["postgres"]["volumes"] = ["test_pgdata:/var/lib/postgresql/data"]
    configuration.setdefault("volumes", {})["test_pgdata"] = {}
    api = configuration["services"]["api"]
    api["ports"] = ["127.0.0.1:18380:8000"]
    api.setdefault("volumes", []).append(f"{linux(directory / 'ca.pem')}:/tls/ca.pem:ro")
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
    report = {
        "mode": "startup smoke"
        if args.smoke_only
        else "full Docker stack with strict TLS DSM substitute",
        "validationScope": "artifact-and-startup" if args.smoke_only else "full-business",
        "businessTests": "not_run" if args.smoke_only else "requested",
        "real_nas_tested": False,
        "apiImage": args.api_image,
    }
    started = False
    stage = "postgres_startup"
    try:
        started = True
        subprocess.run(
            [
                *compose,
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "300",
                "--pull",
                "never",
                "postgres",
            ],
            check=True,
            timeout=600,
        )
        stage = "migration_failure_guard"
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
        stage = "stack_startup"
        subprocess.run(
            [*compose, "up", "-d", "--wait", "--wait-timeout", "300", "--pull", "never"],
            check=True,
            timeout=600,
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
        stage = "api_contract"
        with httpx.Client(trust_env=False, timeout=35) as client:
            base = "http://127.0.0.1:18380"
            for path in ["/", "/settings/account", "/index.html", "/main.dart.js"]:
                assert client.get(base + path).status_code == 404
            assert client.get(base + "/missing.js").status_code == 404
            assert client.get(base + "/v1/client-config").json()["browser_sessions"]
            assert client.get(base + "/v1/datasets").status_code == 401
            report["api_only_runtime_without_bundled_frontend"] = True
            if args.smoke_only:
                assert client.get(base + "/health").json() == {"status": "ok"}
                assert client.get(base + "/.well-known/jwks.json").json()["keys"]
                report["api_health_and_public_key"] = True
                stage = "public_release_permissions_and_io"
                subprocess.run(
                    [
                        *compose,
                        "exec",
                        "-T",
                        "api",
                        "python",
                        "-c",
                        "import os; from pathlib import Path; "
                        "assert os.getuid()==10001 and os.getgid()==10001; "
                        "assert Path('/data/app-releases').stat().st_uid==10001",
                    ],
                    check=True,
                    timeout=15,
                )
                report["non_root_release_volume_permissions"] = True
                report["publicRelease"] = verify_public_release(
                    client, base, release_key, getattr(args, "release_artifact", None)
                )
                saved = subprocess.check_output(
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
                        "SELECT count(*) FROM core.public_releases "
                        "WHERE app_id='board_game_agent' AND architecture='any'",
                    ],
                    text=True,
                    timeout=15,
                )
                assert saved.strip() == "1"
                report["publicRelease"]["sqlPublication"] = True
                stage = "public_release_restart_persistence"
                subprocess.run([*compose, "restart", "api"], check=True, timeout=60)
                deadline = time.monotonic() + 45
                while True:
                    try:
                        restored = client.get(
                            base + "/v1/apps/board_game_agent/releases/manifest",
                            params={"platform": "android", "architecture": "arm64"},
                        )
                        if restored.status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if time.monotonic() >= deadline:
                        raise TimeoutError("API restart did not restore the published release")
                    time.sleep(0.25)
                assert (
                    restored.json()["artifacts"][0]["sha256"] == report["publicRelease"]["sha256"]
                )
                digest = report["publicRelease"]["sha256"]
                again = client.get(
                    base + f"/v1/apps/board_game_agent/releases/artifacts/{digest}.apk",
                    headers={"Range": "bytes=0-31"},
                )
                assert again.status_code == 206 and len(again.content) == 32
                report["publicRelease"]["persistedAfterRestart"] = True
                write_report(report, directory, args.report)
                return
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
            board_path = base + "/v1/apps/board_game_agent"
            board_response = client.post(
                board_path + "/auth/login",
                json={"account": "alice", "password": "fixture-password"},
            )
            board_response.raise_for_status()
            board = board_response.json()
            assert board["user_id"] == users["alice"]["user_id"]
            assert board["dataset_id"] != users["alice"]["dataset_id"]
            board_headers = {"Authorization": "Bearer " + board["access_token"]}
            assert client.get(board_path + "/datasets", headers=board_headers).status_code == 200
            assert client.get(base + "/v1/datasets", headers=board_headers).status_code == 401
            assert (
                client.get(
                    board_path + "/datasets",
                    headers={"Authorization": "Bearer " + users["alice"]["access_token"]},
                ).status_code
                == 401
            )
            board_refreshed = client.post(
                board_path + "/auth/refresh", json={"refresh_token": board["refresh_token"]}
            )
            board_refreshed.raise_for_status()
            assert (
                client.post(
                    board_path + "/auth/logout",
                    json={"refresh_token": board["refresh_token"]},
                ).status_code
                == 200
            )
            assert client.get(board_path + "/datasets", headers=board_headers).status_code == 401
            assert (
                client.get(
                    base + "/v1/datasets",
                    headers={"Authorization": "Bearer " + users["alice"]["access_token"]},
                ).status_code
                == 200
            )
            report["multi_application_login_refresh_logout_and_isolation"] = True
            for account, status in [("alice", 200), ("bob", 403)]:
                headers = {"Authorization": "Bearer " + users[account]["access_token"]}
                response = client.get(base + "/v1/updates/manifest", headers=headers)
                assert response.status_code == status
                if status == 200:
                    assert response.json()["buildNumber"] == 999
                    full = client.get(base + "/v1/files/updates/fixture.apk", headers=headers)
                    partial = client.get(
                        base + "/v1/files/updates/fixture.apk",
                        headers={**headers, "Range": "bytes=4-"},
                    )
                    assert partial.status_code == 206 and partial.content == full.content[4:]
            report["account_files_permissions_and_range"] = True
            browser_headers = {"Origin": base, "Content-Type": "application/json"}
            bootstrap = client.post(
                base + "/v1/auth/browser/bootstrap", headers=browser_headers, json={}
            )
            assert bootstrap.status_code == 200
            cookie = bootstrap.headers["set-cookie"]
            assert "HttpOnly" in cookie and "Secure" in cookie
            # This HTTPX loopback fixture explicitly sends the Secure cookie;
            # it verifies the protocol, not a real browser's TLS behavior.
            browser_headers["Cookie"] = cookie.split(";", 1)[0]
            browser_headers["X-CSRF-Token"] = bootstrap.json()["csrf_token"]
            signed = client.post(
                base + "/v1/auth/browser/login",
                headers=browser_headers,
                json={"account": "alice", "password": "fixture-password"},
            )
            assert signed.status_code == 200 and "refresh_token" not in signed.json()
            browser_token = signed.json()["access_token"]
            assert (
                client.post(
                    base + "/v1/auth/browser/restore", headers=browser_headers, json={}
                ).status_code
                == 200
            )
            config = {
                "directory": str(directory),
                "multi_entity": True,
                "full_business": True,
                "api": "http://127.0.0.1:18380",
                "tokens": {name: result["access_token"] for name, result in users.items()},
                "datasets": {name: result["dataset_id"] for name, result in users.items()},
            }
            client_path = directory / "client.json"
            client_path.write_text(json.dumps(config), encoding="utf-8")
            stage = "live_sync"
            result = subprocess.run(
                [args.dart, "run", "bin/live_probe.dart", str(client_path)],
                cwd=ROOT / "experiments/powersync_probe",
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=7200,
            )
            (directory / "dart.log").write_text(result.stdout + result.stderr, encoding="utf-8")
            if result.returncode:
                raise RuntimeError(f"Live sync failed; inspect {directory / 'dart.log'}")
            report.update(
                next(
                    json.loads(line)
                    for line in result.stdout.splitlines()
                    if line.startswith('{"passed":')
                )
            )
            # A database container replacement must retain the sample tombstone.
            stage = "database_recreation"
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
                    "300",
                ],
                check=True,
                timeout=600,
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
            stage = "api_recreation_and_logout"
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
                    "300",
                ],
                check=True,
                timeout=600,
            )
            refreshed = client.post(
                "http://127.0.0.1:18380/v1/auth/refresh",
                json={"refresh_token": users["alice"]["refresh_token"]},
            )
            assert refreshed.status_code == 200
            report["api_restart_retains_session_and_signing_key"] = True
            assert (
                client.post(
                    base + "/v1/auth/browser/restore", headers=browser_headers, json={}
                ).status_code
                == 200
            )
            assert (
                client.post(
                    base + "/v1/auth/browser/logout", headers=browser_headers, json={}
                ).status_code
                == 200
            )
            assert (
                client.get(
                    base + "/v1/datasets", headers={"Authorization": "Bearer " + browser_token}
                ).status_code
                == 401
            )
            report["browser_cookie_restore_restart_logout"] = True
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
        report["businessTests"] = "passed"
        write_report(report, directory, args.report)
    except BaseException as error:
        failure = {
            "status": "failed",
            "stage": stage,
            "errorType": type(error).__name__,
            **capture_stack_state(docker, compose),
        }
        failure_path = directory / "failure.json"
        failure_path.write_text(json.dumps(failure, indent=2), encoding="utf-8")
        print(f"Stack verification failed at {stage}; diagnostics: {failure_path}", flush=True)
        raise
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
