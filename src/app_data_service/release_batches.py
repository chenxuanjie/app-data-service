"""Machine-authorized numbering, source freezing and release target registration."""

import re
from uuid import uuid4

import sqlalchemy as sa
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.dialects.postgresql import insert

from . import release_models as m
from .dependencies import Application

NAME = r"^[a-z0-9][a-z0-9_-]{0,39}$"
SHA = r"^[a-f0-9]{64}$"
REVISION = r"^[a-f0-9]{40}$"
UUID = r"^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$"


class Reservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    requestKey: str = Field(pattern=r"^[A-Za-z0-9_-]{16,100}$")
    versionName: str = Field(
        pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$", max_length=50
    )
    minimumBuild: int = Field(ge=0, lt=2147483647)


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    sourceRevision: str = Field(pattern=REVISION)
    dependencyRevisions: dict[str, str] = Field(default_factory=dict, max_length=128)


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    component: str = Field(pattern=NAME)
    platform: str = Field(pattern=r"^(android|windows|linux)$")
    architecture: str = Field(pattern=r"^(any|arm64|arm|amd64|x64|x86)$")
    channel: str = Field(pattern=NAME)
    status: str = Field(pattern=r"^(built|published|failed)$")
    kind: str = Field(pattern=r"^(installer|image)$")
    sha256: str | None = Field(default=None, pattern=SHA)
    size: int | None = Field(default=None, gt=0)
    reference: str | None = Field(default=None, max_length=300)
    sourceRevision: str = Field(pattern=REVISION)
    reused: bool = False
    inputDigest: str | None = Field(default=None, pattern=SHA)
    errorCode: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")


def batch(c, app_id, identifier, *, lock=False):
    if not re.fullmatch(UUID, identifier):
        raise HTTPException(422, "release_batch_id_invalid")
    query = sa.select(m.batches).where(m.batches.c.app_id == app_id, m.batches.c.id == identifier)
    row = c.execute(query.with_for_update() if lock else query).mappings().first()
    if row is None:
        raise HTTPException(404, "release_batch_not_found")
    return row


def response(row):
    return {
        "batchId": row["id"],
        "appId": row["app_id"],
        "versionName": row["version_name"],
        "buildNumber": row["build_number"],
        "sourceRevision": row["source_revision"],
        "dependencyRevisions": row["dependency_revisions"],
        "state": "frozen" if row["source_revision"] else "reserved",
    }


def require_source(row, version, build, revision):
    if not row["source_revision"]:
        raise HTTPException(409, "release_batch_not_frozen")
    if (row["version_name"], row["build_number"], row["source_revision"]) != (
        version,
        build,
        revision,
    ):
        raise HTTPException(409, "release_batch_source_conflict")


def register(c, row, target):
    if not row["source_revision"]:
        raise HTTPException(409, "release_batch_not_frozen")
    if target.sourceRevision != row["source_revision"] and not (
        target.kind == "image" and target.reused and target.inputDigest
    ):
        raise HTTPException(409, "release_batch_source_conflict")
    if (
        (target.platform == "linux") != (target.kind == "image")
        or (target.platform == "windows" and target.architecture != "x64")
        or (target.kind == "image" and target.architecture not in {"amd64", "arm64"})
        or (
            target.platform == "android"
            and target.architecture not in {"any", "arm64", "arm", "x64", "x86"}
        )
        or (target.reused and (target.kind != "image" or not target.inputDigest))
    ):
        raise HTTPException(422, "release_target_invalid")
    if target.status == "failed":
        if not target.errorCode or any((target.sha256, target.size, target.reference)):
            raise HTTPException(422, "release_failure_invalid")
    elif (
        not target.sha256
        or (target.kind == "installer" and not target.size)
        or (
            target.kind == "image"
            and (
                not target.reference
                or not re.fullmatch(
                    r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9._/-]+@sha256:" + target.sha256,
                    target.reference,
                )
            )
        )
        or target.errorCode
    ):
        raise HTTPException(422, "release_target_invalid")
    identity = {
        "app_id": row["app_id"],
        "batch_id": row["id"],
        **{
            name: getattr(target, name)
            for name in ("component", "platform", "architecture", "channel")
        },
    }
    condition = sa.and_(*(m.targets.c[name] == value for name, value in identity.items()))
    previous = c.execute(sa.select(m.targets).where(condition).with_for_update()).mappings().first()
    record = target.model_dump()
    if previous:
        if previous["record"] == record:
            return previous["status"]
        if (
            previous["status"] == "published"
            and target.status == "built"
            and (previous["record"] | {"status": "built"}) == record
        ):
            return "published"
        if previous["status"] == "published" or (
            previous["status"] == "built"
            and target.status != "failed"
            and {k: v for k, v in previous["record"].items() if k != "status"}
            != {k: v for k, v in record.items() if k != "status"}
        ):
            raise HTTPException(409, "release_target_conflict")
        c.execute(
            m.targets.update()
            .where(condition)
            .values(status=target.status, record=record, updated_at=sa.func.clock_timestamp())
        )
    else:
        c.execute(m.targets.insert().values(**identity, status=target.status, record=record))
    return target.status


def install(router, runtime, prefix, authorize, transaction):
    path = prefix + "/releases/batches"

    @router.post(path)
    def reserve(body: Reservation, request: Request, app_id: Application):
        authorize(request, app_id)
        with transaction(runtime.engine, app_id, publishing=True) as c:
            c.exec_driver_sql("SET LOCAL lock_timeout='5s'")
            c.execute(
                sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:app,1516))"),
                {"app": app_id},
            )
            previous = (
                c.execute(
                    sa.select(m.batches).where(
                        m.batches.c.app_id == app_id, m.batches.c.request_key == body.requestKey
                    )
                )
                .mappings()
                .first()
            )
            if previous:
                if (
                    previous["version_name"] != body.versionName
                    or body.minimumBuild > previous["build_number"]
                ):
                    raise HTTPException(409, "release_reservation_conflict")
                return response(previous)
            latest = c.scalar(sa.select(sa.func.max(m.releases.c.build_number))) or 0
            counter = (
                c.scalar(sa.select(m.counters.c.last_build).where(m.counters.c.app_id == app_id))
                or 0
            )
            number = max(latest, counter, body.minimumBuild) + 1
            if number > 2147483647:
                raise HTTPException(409, "release_build_number_exhausted")
            c.execute(
                insert(m.counters)
                .values(app_id=app_id, last_build=number)
                .on_conflict_do_update(
                    index_elements=[m.counters.c.app_id], set_={"last_build": number}
                )
            )
            row = (
                c.execute(
                    m.batches.insert()
                    .values(
                        app_id=app_id,
                        id=str(uuid4()),
                        request_key=body.requestKey,
                        version_name=body.versionName,
                        build_number=number,
                        dependency_revisions={},
                    )
                    .returning(m.batches)
                )
                .mappings()
                .one()
            )
            return response(row)

    @router.get(path + "/{batch_id}")
    def inspect_batch(batch_id: str, request: Request, app_id: Application):
        authorize(request, app_id)
        with transaction(runtime.engine, app_id, publishing=True) as c:
            row = batch(c, app_id, batch_id)
            result = response(row)
            result["targets"] = list(
                c.scalars(
                    sa.select(m.targets.c.record).where(
                        m.targets.c.app_id == app_id, m.targets.c.batch_id == batch_id
                    )
                )
            )
            return result

    @router.put(path + "/{batch_id}/source")
    def freeze(batch_id: str, body: Source, request: Request, app_id: Application):
        authorize(request, app_id)
        if any(
            not re.fullmatch(r"[a-zA-Z0-9_./-]{1,150}", key) or not re.fullmatch(REVISION, value)
            for key, value in body.dependencyRevisions.items()
        ):
            raise HTTPException(422, "release_dependencies_invalid")
        with transaction(runtime.engine, app_id, publishing=True) as c:
            row = batch(c, app_id, batch_id, lock=True)
            if row["source_revision"]:
                if (row["source_revision"], row["dependency_revisions"]) != (
                    body.sourceRevision,
                    body.dependencyRevisions,
                ):
                    raise HTTPException(409, "release_batch_source_conflict")
            else:
                row = (
                    c.execute(
                        m.batches.update()
                        .where(m.batches.c.app_id == app_id, m.batches.c.id == batch_id)
                        .values(
                            source_revision=body.sourceRevision,
                            dependency_revisions=body.dependencyRevisions,
                            frozen_at=sa.func.clock_timestamp(),
                        )
                        .returning(m.batches)
                    )
                    .mappings()
                    .one()
                )
            return response(row)

    @router.put(path + "/{batch_id}/targets")
    def target(batch_id: str, body: Target, request: Request, app_id: Application):
        authorize(request, app_id)
        if body.kind != "image" and body.status != "failed":
            raise HTTPException(422, "release_installer_requires_verified_publish")
        with transaction(runtime.engine, app_id, publishing=True) as c:
            row = batch(c, app_id, batch_id, lock=True)
            status = register(c, row, body)
        return {"batchId": batch_id, "registered": True, "status": status}
