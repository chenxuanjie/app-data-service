"""Publish an explicit local resource snapshot; never connects to DSM or edits the source."""

import hashlib
import json
import shutil
from pathlib import Path

from .library import resource_path


def publish(source: Path, destination: Path):
    source, destination = source.resolve(), destination.resolve()
    if not source.is_dir() or destination.exists() or source in destination.parents:
        raise ValueError("Use an existing source and a new destination outside it")
    files = {}
    paths = sorted((source / "assets").rglob("*"))
    for path in paths:
        relative = path.relative_to(source).as_posix()
        if path.is_symlink():
            raise ValueError("Symlinks are not publication inputs")
        if not path.is_file():
            continue
        resource_path(relative)
        if not 0 < path.stat().st_size <= 512 * 1024 * 1024:
            raise ValueError("Resources must be nonempty and at most 512 MiB")
        with path.open("rb") as stream:
            files[relative] = {
                "size": path.stat().st_size,
                "sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
            }
    if "assets/catalog.json" not in files or len(files) > 10000:
        raise ValueError("Publication requires a catalog and at most 10000 files")
    # Validate before creating output. A failed staging directory is never live.
    destination.mkdir(parents=True)
    for relative in files:
        output = destination / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, output)
        with output.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != files[relative]["sha256"]:
                raise ValueError("Input changed while preparing the unpublished snapshot")
    (destination / "publication.json").write_text(
        json.dumps({"version": 1, "files": files}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"files": len(files), "destination": str(destination), "deployed": False}


def register_commands(commands):
    command = commands.add_parser(
        "board-publish-library", help="Prepare a new local public snapshot"
    )
    command.add_argument("--source", type=Path, required=True)
    command.add_argument("--destination", type=Path, required=True)
    command.set_defaults(
        needs_database=False, handler=lambda args, _: publish(args.source, args.destination)
    )
