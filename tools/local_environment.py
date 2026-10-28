"""Diagnose and start known local prerequisites without resetting WSL or data."""

import argparse
import json
import socket
import subprocess
import time

WSL = ["wsl", "-d", "Ubuntu", "--exec"]
DEV_DATABASE = "app-data-service-dev-postgres-1"


class EnvironmentFailure(RuntimeError):
    pass


def linux(*command, timeout=30):
    try:
        return subprocess.run([*WSL, *command], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise EnvironmentFailure(
            f"WSL_COMMAND_UNRESPONSIVE: {command[0]}; WSL was not restarted."
        ) from error


def wait_ready(probe, description, timeout=180):
    deadline = time.monotonic() + timeout
    while True:
        if probe():
            print(f"READY {description}", flush=True)
            return
        if time.monotonic() >= deadline:
            raise EnvironmentFailure(f"NOT_READY: {description}")
        time.sleep(1)


def ensure_service(name):
    status = linux("systemctl", "is-active", name)
    state = status.stdout.strip()
    if state in {"inactive", "failed"}:
        print(f"RECOVER starting local service {name}", flush=True)
        result = linux("sudo", "-n", "systemctl", "start", name, timeout=180)
        if result.returncode:
            raise EnvironmentFailure(
                f"SERVICE_START_FAILED: {name}; check local service permissions/journal. "
                "No interactive sudo, service installation or WSL reset was attempted."
            )
    elif state not in {"active", "activating", "reloading"}:
        raise EnvironmentFailure(f"SERVICE_UNAVAILABLE: {name} ({state or 'no response'})")
    wait_ready(
        lambda: linux("systemctl", "is-active", name).stdout.strip() == "active",
        f"service {name}",
    )


def ensure_docker():
    ensure_service("docker")
    wait_ready(
        lambda: (
            (result := linux("docker", "info", "--format", "{{.OSType}}")).stdout.strip() == "linux"
            and result.returncode == 0
        ),
        "Linux Docker API",
    )


def ensure_build_proxy():
    result = linux("docker", "info", "--format", "{{.HTTPSProxy}}")
    if result.returncode:
        raise EnvironmentFailure("DOCKER_PROXY_CONFIG_UNAVAILABLE")
    # This machine's registered relay only; do not start unrelated proxy units.
    if result.stdout.strip() in {"http://127.0.0.1:18080", "http://localhost:18080"}:
        ensure_service("bga-docker-proxy")


def database_state():
    result = linux(
        "docker",
        "inspect",
        "--format",
        '{{json .State}}|{{index .Config.Labels "com.docker.compose.project"}}|'
        '{{index .Config.Labels "com.docker.compose.service"}}',
        DEV_DATABASE,
    )
    if result.returncode:
        raise EnvironmentFailure(
            "DEV_DATABASE_MISSING: initialize the local development instance explicitly; "
            "no container or database volume was created."
        )
    state, project, service = result.stdout.strip().split("|")
    if (project, service) != ("app-data-service-dev", "postgres"):
        raise EnvironmentFailure("DEV_DATABASE_IDENTITY_MISMATCH: refusing to change container")
    return json.loads(state)


def ensure_database(host, port, *, check_windows=True):
    if host != "127.0.0.1" or port != 55432:
        raise EnvironmentFailure(
            "UNEXPECTED_DATABASE_ENDPOINT: local recovery only supports 127.0.0.1:55432"
        )
    state = database_state()
    if state["Status"] in {"exited", "created"}:
        print("RECOVER starting existing local development PostgreSQL container", flush=True)
        if linux("docker", "start", DEV_DATABASE, timeout=120).returncode:
            raise EnvironmentFailure("DEV_DATABASE_START_FAILED")
    elif state["Status"] != "running":
        raise EnvironmentFailure(f"DEV_DATABASE_STATE: {state['Status']}; no forced restart")

    def ready():
        state = database_state()
        if state["Status"] != "running":
            raise EnvironmentFailure("DEV_DATABASE_EXITED_DURING_STARTUP")
        if state.get("Health", {}).get("Status") == "unhealthy":
            raise EnvironmentFailure("DEV_DATABASE_UNHEALTHY: inspect local PostgreSQL logs")
        return state.get("Health", {}).get("Status") == "healthy"

    wait_ready(ready, "development PostgreSQL health", timeout=300)
    result = linux(
        "python3",
        "-c",
        "import socket; socket.create_connection(('127.0.0.1',55432),timeout=5).close()",
    )
    if result.returncode:
        raise EnvironmentFailure(
            "WSL_DATABASE_PORT_UNREACHABLE: PostgreSQL is healthy but Docker's local "
            "55432 publication is unavailable; no volume reset or forced restart."
        )

    if not check_windows:
        return

    def windows_ready():
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except OSError:
            return False

    try:
        wait_ready(windows_ready, "Windows to WSL PostgreSQL forwarding", timeout=30)
    except EnvironmentFailure as error:
        raise EnvironmentFailure(
            "WINDOWS_WSL_FORWARDING_FAILED: PostgreSQL and WSL-local 55432 work; "
            "Windows loopback forwarding does not. Check Windows port ownership and "
            "WSL localhost forwarding; do not reinstall pytest or recreate the database."
        ) from error


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--purpose", choices=["test", "build"], default="build")
    args = parser.parse_args()
    try:
        ensure_docker()
        if args.purpose == "build":
            ensure_build_proxy()
        else:
            from sqlalchemy.engine import make_url

            from app_data_service.cli import local_config

            url = make_url(local_config()["urls"]["admin"])
            ensure_database(url.host, url.port or 5432, check_windows=False)
    except EnvironmentFailure as error:
        print(str(error), flush=True)
        raise SystemExit(1) from None
