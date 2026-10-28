import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def environment(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    return importlib.import_module("local_environment")


def test_stopped_service_is_started_once_without_restart(environment, monkeypatch):
    calls = []

    def linux(*command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="inactive" if len(calls) == 1 else "active")

    monkeypatch.setattr(environment, "linux", linux)
    environment.ensure_service("docker")
    assert calls == [
        ("systemctl", "is-active", "docker"),
        ("sudo", "-n", "systemctl", "start", "docker"),
        ("systemctl", "is-active", "docker"),
    ]


def test_cold_database_waits_for_health_before_windows_connection(environment, monkeypatch):
    states = iter(
        [
            {"Status": "exited"},
            {"Status": "running", "Health": {"Status": "starting"}},
            {"Status": "running", "Health": {"Status": "healthy"}},
        ]
    )
    calls = []
    monkeypatch.setattr(environment, "database_state", lambda: next(states))
    monkeypatch.setattr(environment.time, "sleep", lambda seconds: None)

    def linux(*command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(environment, "linux", linux)
    monkeypatch.setattr(environment.socket, "create_connection", lambda *a, **k: Connection())
    environment.ensure_database("127.0.0.1", 55432)
    assert calls[0] == ("docker", "start", environment.DEV_DATABASE)
    assert calls[1][:2] == ("python3", "-c")


def test_database_identity_mismatch_cannot_start_unknown_container(environment, monkeypatch):
    monkeypatch.setattr(
        environment,
        "linux",
        lambda *a, **k: SimpleNamespace(
            returncode=0, stdout=json.dumps({"Status": "exited"}) + "|production|postgres"
        ),
    )
    with pytest.raises(environment.EnvironmentFailure, match="IDENTITY_MISMATCH"):
        environment.database_state()


def test_forwarding_failure_is_distinguished_from_database_failure(environment, monkeypatch):
    monkeypatch.setattr(
        environment,
        "database_state",
        lambda: {"Status": "running", "Health": {"Status": "healthy"}},
    )
    monkeypatch.setattr(environment, "linux", lambda *a, **k: SimpleNamespace(returncode=0))
    ticks = iter([0, 0, 31])
    monkeypatch.setattr(environment.time, "monotonic", lambda: next(ticks))

    def unavailable(*args, **kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(environment.socket, "create_connection", unavailable)
    with pytest.raises(environment.EnvironmentFailure, match="WINDOWS_WSL_FORWARDING_FAILED"):
        environment.ensure_database("127.0.0.1", 55432)
