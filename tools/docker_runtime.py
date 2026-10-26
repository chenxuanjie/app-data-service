"""Keep the selected WSL runtime alive only while a local Docker task owns it."""

import os
import queue
import subprocess
import threading
from contextlib import contextmanager


@contextmanager
def docker_runtime(mode):
    """Systemd/Docker alone do not prevent WSL's idle shutdown.

    Keep an attached, hidden WSL process with an owned stdin pipe. Closing that
    pipe ends the shell on success or failure; a one-hour deadline also bounds
    its lifetime if the Windows parent is interrupted. Never shut down WSL or
    kill another task's processes.
    """
    if mode != "wsl":
        yield
        return
    process = subprocess.Popen(
        [
            "wsl",
            "-d",
            "Ubuntu",
            "--exec",
            "bash",
            "-c",
            "printf 'ready\\n'; read -r -t 3600; exit 0",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    ready = queue.Queue(maxsize=1)
    reader = threading.Thread(target=lambda: ready.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        try:
            line = ready.get(timeout=45)
        except queue.Empty as error:
            raise RuntimeError("WSL runtime did not become ready within 45 seconds") from error
        if line.strip() != "ready" or process.poll() is not None:
            raise RuntimeError("WSL runtime exited before the Docker task started")
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
