"""Validate the separately committed Flutter input before a Python image build."""

import hashlib
import json
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath


def verify_web(project, archive, receipt, source_gate):
    project, archive, receipt = project.resolve(), archive.resolve(), receipt.resolve()
    if not archive.is_relative_to(project) or not receipt.is_relative_to(project):
        raise ValueError("Web artifact and receipt must belong to the supplied project")
    subprocess.run(
        [
            sys.executable,
            str(source_gate),
            "verify",
            "--project",
            str(project),
            "--receipt",
            str(receipt),
            "--artifact",
            str(archive),
        ],
        check=True,
        timeout=60,
    )
    record = json.loads(receipt.read_text(encoding="utf-8"))
    with archive.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    return {"archive": archive.name, "sha256": digest, "sources": record["sources"]}


def extract_web(archive: Path, destination: Path):
    destination.mkdir()
    total = 0
    seen = set()
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            path = PurePosixPath(member.name)
            total += member.size
            if (
                not member.isfile()
                or path.is_absolute()
                or ".." in path.parts
                or "\\" in member.name
                or ":" in member.name
                or member.name in seen
                or total > 256 * 1024 * 1024
            ):
                raise ValueError("Unsafe or oversized Web archive")
            seen.add(member.name)
            target = destination.joinpath(*path.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("Web archive path escapes staging")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as data, target.open("xb") as output:
                while chunk := data.read(65536):
                    output.write(chunk)
    version = json.loads((destination / "version.json").read_text(encoding="utf-8"))
    if version.get("app_name") != "zheri" or not all(
        (destination / name).is_file()
        for name in ("index.html", "main.dart.js", "flutter_bootstrap.js")
    ):
        raise ValueError("Complete Zheri Web release required")
    return {"version": version["version"], "buildNumber": version["build_number"]}
