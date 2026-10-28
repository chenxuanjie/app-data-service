import importlib
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError


def test_task_failure_and_timeout_keep_output_and_durations(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    diagnostics = importlib.import_module("task_diagnostics")
    log = diagnostics.TaskLog(tmp_path, "failure-test")
    with pytest.raises(subprocess.CalledProcessError):
        log.run("failure", [sys.executable, "-c", "print('evidence'); exit(7)"], timeout=5)
    with pytest.raises(subprocess.TimeoutExpired):
        log.run(
            "timeout",
            [sys.executable, "-u", "-c", "import time; print('before timeout'); time.sleep(5)"],
            timeout=0.2,
        )
    stages = json.loads((log.directory / "stages.json").read_text())["stages"]
    assert stages[0]["exitCode"] == 7
    assert stages[1]["errorType"] == "TimeoutExpired"
    assert all(item["seconds"] >= 0 and item["status"] == "failed" for item in stages)
    assert "evidence" in (log.directory / "01-failure.log").read_text()
    assert "before timeout" in (log.directory / "02-timeout.log").read_text()


def test_database_preflight_exits_once_without_cleanup_reconnect(monkeypatch):
    import conftest

    calls = []

    def connect():
        calls.append("connect")
        raise OperationalError("private details", {}, OSError("unavailable"))

    engine = SimpleNamespace(connect=connect, dispose=lambda: calls.append("dispose"))
    monkeypatch.setattr(conftest, "local_config", lambda: {"urls": {"admin": "private"}})
    monkeypatch.setattr(conftest, "make_engine", lambda url: engine)
    with pytest.raises(pytest.exit.Exception, match="PostgreSQL preflight failed"):
        next(conftest.db.__wrapped__())
    assert calls == ["connect", "dispose"]


def test_report_uses_unique_temp_and_retries_windows_sharing_lock(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    diagnostics = importlib.import_module("task_diagnostics")
    log = diagnostics.TaskLog(tmp_path, "sharing-test")
    stale = log.directory / "stages.tmp"
    stale.write_text("other writer", encoding="utf-8")
    original = Path.replace
    attempts = []

    def replace(path, target):
        attempts.append(path)
        assert path != stale
        if len(attempts) < 3:
            error = PermissionError("simulated Windows sharing conflict")
            error.winerror = 32
            raise error
        return original(path, target)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(diagnostics.time, "sleep", lambda seconds: None)
    log.save()
    assert len(attempts) == 3
    assert json.loads((log.directory / "stages.json").read_text()) == {"stages": []}
    assert stale.read_text() == "other writer"
    assert not list(log.directory.glob(".stages-*.tmp"))
