import importlib
import queue
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    return importlib.import_module("docker_runtime")


@pytest.mark.parametrize("fail", [False, True])
def test_wsl_lease_releases_only_its_child_on_success_or_failure(runtime, monkeypatch, fail):
    original = subprocess.Popen
    children = []

    def launch(command, **kwargs):
        assert command[:5] == ["wsl", "-d", "Ubuntu", "--exec", "bash"]
        assert "-t 3600" in command[-1]
        child = original(
            [sys.executable, "-u", "-c", "import sys; print('ready'); sys.stdin.readline()"],
            **kwargs,
        )
        children.append(child)
        return child

    monkeypatch.setattr(runtime.subprocess, "Popen", launch)
    if fail:
        with (
            pytest.raises(ValueError, match="simulated verification failure"),
            runtime.docker_runtime("wsl"),
        ):
            assert children[0].poll() is None
            raise ValueError("simulated verification failure")
    else:
        with runtime.docker_runtime("wsl"):
            assert children[0].poll() is None
    assert len(children) == 1 and children[0].returncode == 0
    assert children[0].stdin.closed and children[0].stdout.closed


def test_native_docker_never_launches_wsl(runtime, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Native Docker must not launch WSL")

    monkeypatch.setattr(runtime.subprocess, "Popen", unexpected)
    with runtime.docker_runtime("native"):
        pass


@pytest.mark.parametrize("silent", [False, True])
def test_broken_wsl_startup_is_bounded_and_releases_child(runtime, monkeypatch, silent):
    original_popen, original_queue = subprocess.Popen, queue.Queue
    children = []

    def launch(command, **kwargs):
        script = "import sys; sys.stdin.readline()" if silent else "print('unexpected')"
        child = original_popen([sys.executable, "-u", "-c", script], **kwargs)
        children.append(child)
        return child

    class FastDeadlineQueue(original_queue):
        def get(self, block=True, timeout=None):
            return super().get(block=block, timeout=0.1 if silent else timeout)

    monkeypatch.setattr(runtime.subprocess, "Popen", launch)
    monkeypatch.setattr(runtime.queue, "Queue", FastDeadlineQueue)
    with pytest.raises(RuntimeError, match="WSL runtime"), runtime.docker_runtime("wsl"):
        pytest.fail("A broken runtime must not start a Docker task")
    assert children[0].returncode == 0


def test_failure_snapshot_limits_inspection_to_test_stack_and_excludes_secrets(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    verifier = importlib.import_module("verify_stack")
    commands = []

    def output(command, **kwargs):
        commands.append(command)
        assert kwargs["timeout"] == 15
        if command[-3:] == ["ps", "--all", "--quiet"]:
            return "owned-container\n"
        assert command[-1] == "owned-container"
        template = command[command.index("--format") + 1]
        assert ".Config.Env" not in template and ".Config.Cmd" not in template
        assert ".State.OOMKilled" in template and ".RestartCount" in template
        return '{"name":"/fixture-api-1","exitCode":137,"oomKilled":true}\n'

    monkeypatch.setattr(verifier.subprocess, "check_output", output)
    snapshot = verifier.capture_stack_state(["docker"], ["docker", "compose", "-p", "fixture"])
    assert snapshot["containers"][0]["oomKilled"] is True
    assert len(commands) == 2


def test_diagnostic_failure_does_not_replace_original_failure(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "tools"))
    verifier = importlib.import_module("verify_stack")

    def unavailable(*args, **kwargs):
        raise OSError("Private daemon details must not enter the report")

    monkeypatch.setattr(verifier.subprocess, "check_output", unavailable)
    assert verifier.capture_stack_state(["docker"], ["docker", "compose"]) == {
        "diagnosticError": "OSError"
    }
