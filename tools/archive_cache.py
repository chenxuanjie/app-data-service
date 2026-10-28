"""Reuse only previously imported archives matching current image identity and bytes."""

import hashlib
from pathlib import Path


def cached_dependency(previous, output, reference, identity, filename):
    if not previous or previous.get("platform") != "linux/amd64":
        return None, "no compatible release metadata"
    record = next(
        (item for item in previous.get("archives", []) if item.get("image") == reference), None
    )
    if not record or record.get("imageId") != identity or record.get("importVerified") is not True:
        return None, "image identity or prior import proof changed"
    if record.get("file") != filename or Path(filename).name != filename:
        return None, "archive filename changed"
    path = Path(output) / filename
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        return None, "archive is a link"
    if not path.is_file() or path.stat().st_size != record.get("size"):
        return None, "archive missing or size changed"
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != record.get("sha256"):
        return None, "archive checksum changed"
    return dict(record), "image identity, platform, import proof and SHA-256 matched"
