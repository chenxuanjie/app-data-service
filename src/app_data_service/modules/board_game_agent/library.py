"""Read only a bounded, explicitly published allowlist; no DSM or account credentials."""

import hashlib
import json
import mimetypes
import re
from pathlib import Path

from fastapi import HTTPException
from starlette.responses import FileResponse, Response


def resource_path(value):
    if not isinstance(value, str) or not re.fullmatch(r"assets/[A-Za-z0-9_./-]+", value):
        raise ValueError("Invalid published resource path")
    if any(part in ("", ".", "..") or part.startswith(".") for part in value.split("/")):
        raise ValueError("Invalid published resource path")
    if "/docs/others" in value.lower():
        raise ValueError("Unpublished source documents")
    if Path(value).suffix.lower() not in {
        ".json",
        ".pdf",
        ".txt",
        ".md",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".svg",
    }:
        raise ValueError("Unsupported published resource type")
    return value


class PublishedLibrary:
    def __init__(self, root):
        self.root = Path(root) if root else None
        self._verified = {}
        self._publication = None

    @staticmethod
    def fingerprint(path):
        status = path.stat()
        return status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns

    def path(self, relative):
        if self.root is None or not self.root.is_dir():
            raise HTTPException(503, "library_not_configured")
        root = self.root.resolve()
        path = self.root / relative
        if self.root.is_symlink() or any(
            p.is_symlink() for p in (path, *path.parents) if p.is_relative_to(self.root)
        ):
            raise HTTPException(404, "resource_unavailable")
        if not path.resolve().is_relative_to(root) or not path.is_file():
            raise HTTPException(404, "resource_unavailable")
        return path

    def index(self):
        try:
            path = self.path("publication.json")
            fingerprint = self.fingerprint(path)
            if self._publication and self._publication[0] == fingerprint:
                return self._publication[1]
            if path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError()
            index = json.loads(path.read_bytes())
            files = index["files"]
            if index.get("version") != 1 or not isinstance(files, dict) or len(files) > 10000:
                raise ValueError()
            for key, value in files.items():
                resource_path(key)
                if (
                    not re.fullmatch(r"[a-f0-9]{64}", value["sha256"])
                    or type(value["size"]) is not int
                    or not 0 < value["size"] <= 512 * 1024 * 1024
                ):
                    raise ValueError()
            if "assets/catalog.json" not in files:
                raise ValueError()
            index = {
                "version": 1,
                "files": {
                    key: {"sha256": value["sha256"], "size": value["size"]}
                    for key, value in files.items()
                },
            }
            self._publication = fingerprint, index
            return index
        except (ValueError, KeyError, TypeError, OSError) as error:
            raise HTTPException(503, "publication_invalid") from error

    def file(self, relative, request):
        try:
            relative = resource_path(relative)
        except ValueError as error:
            raise HTTPException(404, "resource_unavailable") from error
        record = self.index()["files"].get(relative)
        if record is None:
            raise HTTPException(404, "resource_unavailable")
        path = self.path(relative)
        if path.stat().st_size != record["size"]:
            raise HTTPException(503, "publication_changed")
        # Mounted snapshots are read-only. Verify once per filesystem version,
        # rather than hashing a large PDF again on every byte-range request.
        fingerprint = self.fingerprint(path), record["sha256"]
        if self._verified.get(relative) != fingerprint:
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != record["sha256"]:
                    raise HTTPException(503, "publication_changed")
            self._verified[relative] = fingerprint
            if len(self._verified) > 4096:
                self._verified.pop(next(iter(self._verified), None), None)
        etag = '"' + record["sha256"] + '"'
        headers = {
            "ETag": etag,
            "Cache-Control": "public, max-age=0, must-revalidate",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        }
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        return FileResponse(
            path,
            media_type=mimetypes.guess_type(relative)[0] or "application/octet-stream",
            headers=headers,
        )
