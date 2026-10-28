"""Export/validate/import on the Docker host; copy only the final gzip archive out."""

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from image_archives import validate_archive


def export_image(reference, identity, destination, *, workspace_parent=None):
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise ValueError("Archive destination must be a new staging file")
    timings = {}

    def measured(name, action):
        started = time.monotonic()
        print(f"START {name}", flush=True)
        try:
            return action()
        finally:
            timings[name] = round(time.monotonic() - started, 3)
            print(f"END {name}: {timings[name]}s", flush=True)

    parent = workspace_parent or ("/tmp" if os.name == "posix" else tempfile.gettempdir())
    with tempfile.TemporaryDirectory(prefix="app-data-image-", dir=parent) as temporary:
        workspace = Path(temporary)
        raw, archive = workspace / "image.tar", workspace / "image.tar.gz"
        measured(
            "save",
            lambda: subprocess.run(
                ["docker", "image", "save", "--output", str(raw), reference],
                check=True,
                timeout=3600,
            ),
        )

        def compress():
            with raw.open("rb") as source, archive.open("wb") as output:
                with gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as zipped:
                    shutil.copyfileobj(source, zipped, length=4 * 1024 * 1024)

        measured("compress", compress)
        measured("archiveValidation", lambda: validate_archive(archive, reference, identity))
        measured(
            "load",
            lambda: subprocess.run(
                ["docker", "image", "load", "--input", str(archive)],
                check=True,
                timeout=3600,
            ),
        )
        loaded = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", reference],
            text=True,
            timeout=120,
        ).strip()
        if loaded != identity:
            raise RuntimeError("Loaded image identity differs from original")
        digest = measured("sha256", lambda: archive_digest(archive))
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            measured("copyFinalArchive", lambda: shutil.copyfile(archive, destination))
            # Validate the actual Windows deliverable, not just the local source.
            copied = measured("copyVerification", lambda: archive_digest(destination))
            if copied != digest:
                raise RuntimeError("Final archive copy checksum mismatch")
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return {
            "size": destination.stat().st_size,
            "sha256": digest,
            "importVerified": True,
            "timings": timings,
            "workspace": "linux-local" if os.name == "posix" else "native-local",
        }


def archive_digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args()
    result = export_image(arguments.image, arguments.identity, arguments.destination)
    arguments.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
