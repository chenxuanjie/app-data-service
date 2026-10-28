"""Keep the selected WSL runtime alive only while a local Docker task owns it."""

import os
import queue
import subprocess
import tempfile
import threading
from contextlib import contextmanager


@contextmanager
def docker_runtime(mode):
    """Systemd/Docker alone do not prevent WSL's idle shutdown.

    Keep an attached, hidden WSL process with an owned stdin pipe. Closing that
    pipe ends the shell on success or failure; a twenty-four-hour deadline also bounds
    its lifetime if the Windows parent is interrupted. Never shut down WSL or
    kill another task's processes.
    """
    if mode != "wsl":
        yield
        return
    print("START WSL attachment (service/database readiness is checked separately)", flush=True)
    startup_errors = tempfile.TemporaryFile(mode="w+t", encoding="utf-8", errors="replace")
    try:
        process = subprocess.Popen(
            [
                "wsl",
                "-d",
                "Ubuntu",
                "--exec",
                "bash",
                "-c",
                "printf 'ready\\n'; read -r -t 86400; exit 0",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=startup_errors,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except BaseException:
        startup_errors.close()
        raise
    ready = queue.Queue(maxsize=1)
    reader = threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        try:
            line = ready.get(timeout=180)
        except queue.Empty as error:
            startup_errors.seek(0)
            detail = startup_errors.read(2048).strip()
            raise RuntimeError(
                "WSL runtime attachment did not respond within 180 seconds; "
                "Docker/PostgreSQL checks have not started. No WSL reset was attempted. " + detail
            ) from error
        if line.strip() != "ready" or process.poll() is not None:
            startup_errors.seek(0)
            raise RuntimeError(
                "WSL runtime exited before the Docker task started. "
                + startup_errors.read(2048).strip()
            )
        print("READY WSL command channel; now check actual service readiness", flush=True)
        yield
    finally:
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        reader.join(timeout=1)
        process.stdout.close()
        startup_errors.close()
