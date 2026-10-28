"""Anonymous installer reads; publishing uses an app-scoped machine secret, never a user."""

import asyncio
import hashlib
import hmac
import json
import os
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import anyio
import sqlalchemy as sa
from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.dialects.postgresql import insert
from starlette.responses import FileResponse

from . import release_models as m
from .dependencies import Application

MAX_ARTIFACT = 1024**3
DIGEST = re.compile(r"^[a-f0-9]{64}$")
NAME = r"^[a-z0-9][a-z0-9_-]{0,39}$"


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    platform: str = Field(pattern=r"^(android|windows)$")
    architecture: str = Field(pattern=r"^(arm64|arm|x64|x86|universal)$")
    path: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.+~-]{0,199}\.(apk|exe)$")
    size: int = Field(gt=0, le=MAX_ARTIFACT)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    mimeType: str


class Publication(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schemaVersion: int = Field(ge=1, le=1)
    appId: str = Field(pattern=NAME)
    channel: str = Field(pattern=NAME)
    versionName: str = Field(min_length=1, max_length=80)
    buildNumber: int = Field(gt=0, le=2**53 - 1)
    minimumSupportedBuild: int = Field(ge=0, le=2**53 - 1)
    forceUpdate: bool
    publishedAt: str = Field(max_length=40)
    releaseNotes: dict[str, list[str]]
    artifacts: list[Artifact] = Field(min_length=1, max_length=1)
    releaseRecord: dict = Field(default_factory=dict)


@contextmanager
def transaction(engine, app_id, *, publishing=False):
    with engine.begin() as c:
        c.exec_driver_sql("SET LOCAL transaction_timeout='20s'")
        c.execute(sa.text("SELECT set_config('app.app_id',:app,true)"), {"app": app_id})
        if publishing:
            c.exec_driver_sql("SELECT set_config('app.release_publisher','on',true)")
        yield c


def root_path(value, app_id):
    if not value:
        raise HTTPException(503, "release_storage_not_configured")
    root = Path(value)
    if not root.is_absolute() or not root.is_dir():
        raise HTTPException(503, "release_storage_unavailable")
    if any(
        p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (root, *root.parents)
    ):
        raise HTTPException(503, "release_storage_unavailable")
    return root / app_id


def object_path(root, digest):
    path = root / "objects" / digest[:2] / digest
    if not DIGEST.fullmatch(digest) or any(
        p.is_symlink() or getattr(p, "is_junction", lambda: False)() for p in (path, *path.parents)
    ):
        raise HTTPException(503, "release_storage_unavailable")
    return path


def file_digest(path):
    with path.open("rb") as content:
        return hashlib.file_digest(content, "sha256").hexdigest(), path.stat().st_size


def manifest(row):
    extension = "exe" if row["platform"] == "windows" else "apk"
    return {
        "schemaVersion": 1,
        "appId": row["app_id"],
        "channel": row["channel"],
        "versionName": row["version_name"],
        "buildNumber": row["build_number"],
        "minimumSupportedBuild": row["minimum_build"],
        "forceUpdate": row["force_update"],
        "publishedAt": row["published_at"].isoformat(),
        "releaseNotes": row["notes"],
        "artifacts": [
            {
                "platform": row["platform"],
                "architecture": row["architecture"],
                "path": f"{row['sha256']}.{extension}",
                "size": row["size"],
                "sha256": row["sha256"],
                "mimeType": row["mime_type"],
            }
        ],
    }


def install(router, runtime, prefix):
    raw_keys = json.loads(os.environ.get("APP_DATA_RELEASE_PUBLISH_KEY_HASHES", "{}"))
    if not isinstance(raw_keys, dict) or any(
        app not in {d.id for d in runtime.applications.definitions}
        or not isinstance(key, str)
        or not DIGEST.fullmatch(key)
        for app, key in raw_keys.items()
    ):
        raise ValueError("Invalid application release publishing configuration")
    uploads, downloads = asyncio.Semaphore(2), asyncio.Semaphore(8)
    verified = {}

    def storage(app_id):
        settings = runtime.application_settings.get(app_id, {})
        return root_path(settings.get("public_file_roots", {}).get("releases"), app_id)

    def publishing_key(request, app_id):
        expected = raw_keys.get(app_id)
        candidate = request.headers.get("x-release-publish-key", "")
        if not expected:
            raise HTTPException(503, "release_publishing_not_configured")
        if not 32 <= len(candidate) <= 256 or not hmac.compare_digest(
            hashlib.sha256(candidate.encode()).hexdigest(), expected
        ):
            raise HTTPException(403, "release_publish_denied")

    async def verify(path, digest, size):
        try:
            status = path.stat()
            fingerprint = (status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)
            if status.st_size != size:
                raise ValueError()
            if verified.get(str(path)) != fingerprint:
                if await anyio.to_thread.run_sync(file_digest, path) != (digest, size):
                    raise ValueError()
                if len(verified) >= 1024:
                    verified.clear()
                verified[str(path)] = fingerprint
        except (OSError, ValueError) as error:
            raise HTTPException(503, "release_artifact_integrity_failure") from error

    @router.get(prefix + "/releases/manifest")
    def latest(
        app_id: Application,
        platform: str = Query(pattern=r"^(android|windows)$"),
        architecture: str = Query(pattern=r"^(arm64|arm|x64|x86|universal)$"),
        channel: str = Query(default="stable", pattern=NAME),
    ):
        storage(app_id)
        with transaction(runtime.engine, app_id) as c:
            row = (
                c.execute(
                    sa.select(m.releases)
                    .where(
                        m.releases.c.channel == channel,
                        m.releases.c.platform == platform,
                        m.releases.c.architecture.in_([architecture, "universal"]),
                    )
                    .order_by(
                        m.releases.c.build_number.desc(),
                        (m.releases.c.architecture == architecture).desc(),
                    )
                    .limit(1)
                )
                .mappings()
                .first()
            )
        if row is None:
            raise HTTPException(404, "release_not_published")
        return JSONResponse(manifest(row), headers={"Cache-Control": "no-store"})

    @router.api_route(prefix + "/releases/artifacts/{artifact}", methods=["GET", "HEAD"])
    async def download(artifact: str, app_id: Application):
        match = re.fullmatch(r"([a-f0-9]{64})\.(apk|exe)", artifact)
        if not match:
            raise HTTPException(404, "release_not_published")
        digest, extension = match.groups()
        with transaction(runtime.engine, app_id) as c:
            row = (
                c.execute(
                    sa.select(m.releases)
                    .where(
                        m.releases.c.sha256 == digest,
                        m.releases.c.platform == ("windows" if extension == "exe" else "android"),
                    )
                    .limit(1)
                )
                .mappings()
                .first()
            )
        if row is None:
            raise HTTPException(404, "release_not_published")
        path = object_path(storage(app_id), digest)
        try:
            await asyncio.wait_for(downloads.acquire(), 1)
        except TimeoutError as error:
            raise HTTPException(503, "release_download_busy") from error

        class Download(FileResponse):
            async def __call__(self, scope, receive, send):
                try:
                    async with asyncio.timeout(600):
                        await super().__call__(scope, receive, send)
                finally:
                    downloads.release()

        try:
            await verify(path, digest, row["size"])
            return Download(
                path,
                filename=artifact,
                media_type=row["mime_type"],
                headers={
                    "ETag": f'"{digest}"',
                    "Cache-Control": "public, max-age=31536000, immutable",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        except BaseException:
            downloads.release()
            raise

    @router.put(prefix + "/releases/objects/{digest}")
    async def upload(digest: str, request: Request, app_id: Application):
        publishing_key(request, app_id)
        if not DIGEST.fullmatch(digest):
            raise HTTPException(422, "release_digest_invalid")
        size = request.headers.get("content-length", "")
        if not size.isdigit() or not 0 < int(size) <= MAX_ARTIFACT:
            raise HTTPException(413, "release_size_invalid")
        size = int(size)
        root = storage(app_id)
        target = object_path(root, digest)
        try:
            await asyncio.wait_for(uploads.acquire(), 1)
        except TimeoutError as error:
            raise HTTPException(503, "release_upload_busy") from error
        staged = target.with_name(f"{digest}.{uuid4().hex}.upload")
        try:
            async with asyncio.timeout(900):
                target.parent.mkdir(parents=True, exist_ok=True)
                object_path(root, digest)
                count, checksum = 0, hashlib.sha256()
                with staged.open("xb") as output:
                    async for chunk in request.stream():
                        count += len(chunk)
                        if count > size:
                            raise HTTPException(413, "release_size_invalid")
                        checksum.update(chunk)
                        await anyio.to_thread.run_sync(output.write, chunk)
                    await anyio.to_thread.run_sync(output.flush)
                    await anyio.to_thread.run_sync(os.fsync, output.fileno())
                if count != size or checksum.hexdigest() != digest:
                    raise HTTPException(422, "release_artifact_mismatch")
                try:
                    os.link(staged, target, follow_symlinks=False)
                except FileExistsError:
                    await verify(target, digest, size)
                with transaction(runtime.engine, app_id, publishing=True) as c:
                    c.execute(
                        insert(m.blobs)
                        .values(app_id=app_id, sha256=digest, size=size)
                        .on_conflict_do_nothing()
                    )
                return {"sha256": digest, "size": size, "verified": True}
        except TimeoutError as error:
            raise HTTPException(408, "release_upload_timeout") from error
        finally:
            staged.unlink(missing_ok=True)
            uploads.release()

    @router.post(prefix + "/releases/publish")
    async def publish(body: Publication, request: Request, app_id: Application):
        publishing_key(request, app_id)
        artifact = body.artifacts[0]
        mime = (
            "application/octet-stream"
            if artifact.platform == "windows"
            else "application/vnd.android.package-archive"
        )
        extension = ".exe" if artifact.platform == "windows" else ".apk"
        try:
            published_at = datetime.fromisoformat(body.publishedAt.replace("Z", "+00:00"))
            if published_at.tzinfo is None:
                raise ValueError()
        except ValueError as error:
            raise HTTPException(422, "release_timestamp_invalid") from error
        if (
            body.appId != app_id
            or body.minimumSupportedBuild > body.buildNumber
            or artifact.mimeType != mime
            or not artifact.path.endswith(extension)
            or artifact.platform == "windows"
            and artifact.architecture != "x64"
            or len(json.dumps(body.releaseNotes).encode()) > 65536
        ):
            raise HTTPException(422, "release_metadata_invalid")
        audit = body.releaseRecord
        audit_artifacts = audit.get("artifacts")
        if audit and (
            not isinstance(audit_artifacts, list)
            or len(audit_artifacts) != 1
            or not isinstance(audit_artifacts[0], dict)
        ):
            raise HTTPException(422, "release_record_invalid")
        if audit and (
            len(json.dumps(audit).encode()) > 128 * 1024
            or any(
                audit.get(name) != record
                for name, record in (
                    ("schemaVersion", 1),
                    ("appId", app_id),
                    ("channel", body.channel),
                    ("versionName", body.versionName),
                    ("buildNumber", body.buildNumber),
                )
            )
            or any(
                audit["artifacts"][0].get(name) != value
                for name, value in (
                    ("sha256", artifact.sha256),
                    ("size", artifact.size),
                    ("platform", artifact.platform),
                    ("architecture", artifact.architecture),
                )
            )
        ):
            raise HTTPException(422, "release_record_invalid")
        await verify(object_path(storage(app_id), artifact.sha256), artifact.sha256, artifact.size)
        record = body.model_dump()
        with transaction(runtime.engine, app_id, publishing=True) as c:
            c.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key,1515))"),
                {
                    "key": f"{app_id}|{body.channel}|{artifact.platform}|{artifact.architecture}",
                },
            )
            query = (
                sa.select(m.releases)
                .where(
                    m.releases.c.channel == body.channel,
                    m.releases.c.platform == artifact.platform,
                    m.releases.c.architecture == artifact.architecture,
                )
                .order_by(m.releases.c.build_number.desc())
            )
            previous = c.execute(query.limit(1)).mappings().first()
            if previous and previous["build_number"] >= body.buildNumber:
                if previous["record"] != record:
                    raise HTTPException(409, "release_version_conflict")
                return JSONResponse(manifest(previous), headers={"Cache-Control": "no-store"})
            blob = (
                c.execute(sa.select(m.blobs).where(m.blobs.c.sha256 == artifact.sha256))
                .mappings()
                .first()
            )
            if blob is None or blob["size"] != artifact.size:
                raise HTTPException(409, "release_object_not_verified")
            row = {
                "app_id": app_id,
                "channel": body.channel,
                "platform": artifact.platform,
                "architecture": artifact.architecture,
                "build_number": body.buildNumber,
                "version_name": body.versionName,
                "minimum_build": body.minimumSupportedBuild,
                "force_update": body.forceUpdate,
                "published_at": published_at,
                "notes": body.releaseNotes,
                "sha256": artifact.sha256,
                "size": artifact.size,
                "mime_type": mime,
                "record": record,
            }
            c.execute(m.releases.insert().values(**row))
        return JSONResponse(manifest(row), headers={"Cache-Control": "no-store"})
