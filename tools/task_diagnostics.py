"""Bounded local stages with durable output and timings, including failures."""

import json
import os
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4


class TaskLog:
    def __init__(self, root, name):
        self.directory = (
            Path(root)
            / ".local"
            / "task-logs"
            / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + name + "-" + uuid4().hex[:8])
        )
        self.directory.mkdir(parents=True)
        self.stages = []
        print(f"Task logs: {self.directory}", flush=True)

    def run(self, name, command, *, timeout, **kwargs):
        record = {"stage": name, "status": "running", "timeoutSeconds": timeout}
        self.stages.append(record)
        self.save()
        started = time.monotonic()
        print(f"START {name} (deadline {timeout}s)", flush=True)
        try:
            # Callers can explicitly redirect sensitive resolved Compose output
            # to their private file; never include command arguments in reports.
            if "stdout" in kwargs:
                result = subprocess.run(command, timeout=timeout, check=True, **kwargs)
            else:
                filename = f"{len(self.stages):02d}-{name}.log"
                record["log"] = filename
                self.save()
                with (self.directory / filename).open("wb") as output:
                    result = subprocess.run(
                        command,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        timeout=timeout,
                        check=True,
                        **kwargs,
                    )
            record["status"] = "passed"
            return result
        except BaseException as error:
            record["status"] = "failed"
            record["errorType"] = type(error).__name__
            if isinstance(error, subprocess.CalledProcessError):
                record["exitCode"] = error.returncode
            raise
        finally:
            record["seconds"] = round(time.monotonic() - started, 3)
            self.save()
            print(f"{record['status'].upper()} {name}: {record['seconds']}s", flush=True)

    def save(self):
        target = self.directory / "stages.json"
        # Reusing stages.tmp can race Windows readers/antivirus after rename.
        # Keep every write private, then retry only transient Windows sharing
        # errors while atomically replacing the public report.
        descriptor, name = tempfile.mkstemp(prefix=".stages-", suffix=".tmp", dir=self.directory)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"stages": self.stages}, indent=2) + "\n")
            deadline = time.monotonic() + 30
            while True:
                try:
                    temporary.replace(target)
                    break
                except PermissionError as error:
                    if (
                        os.name != "nt"
                        or getattr(error, "winerror", None) not in (5, 32, 33)
                        or time.monotonic() >= deadline
                    ):
                        raise
                    time.sleep(0.1)
        finally:
            # Only our unique temporary file; never remove another writer's file.
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass
