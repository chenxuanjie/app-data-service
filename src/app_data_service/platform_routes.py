"""Shared account, data-space, capability and account-file endpoints."""

from urllib.parse import urlsplit

import sqlalchemy as sa
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from .auth import SYNC_AUDIENCE
from .auth_routes import build_router as authentication_router
from .dependencies import Actor, Application, Credential
from .models import Dataset, User
from .public_releases import install as install_public_releases
from .store import user_transaction

PREFIX = "/v1/apps/{application_id}"


def build_router(runtime, *, router=None, prefix=PREFIX):
    router = APIRouter() if router is None else router
    install_public_releases(router, runtime, prefix)
    if runtime.sessions:
        authentication_router(runtime, router=router, prefix=prefix)

    @router.get(prefix + "/client-config")
    def client_config(app_id: Application):
        endpoint = urlsplit(runtime.sync_endpoint)
        return {
            "api_origin": f"{endpoint.scheme}://{endpoint.netloc}",
            "app_id": app_id,
            "browser_sessions": app_id in runtime.browsers,
        }

    @router.get(prefix + "/capabilities")
    def capabilities(actor: Actor):
        definition = runtime.applications.get(actor.app_id)
        return {"app_id": definition.id, **dict(definition.capabilities)}

    @router.get(prefix + "/datasets")
    def datasets(actor: Actor):
        with user_transaction(runtime.engine, actor.user_id, app_id=actor.app_id) as c:
            return {
                "datasets": [
                    dict(row)
                    for row in c.execute(
                        sa.select(Dataset.id, Dataset.name, Dataset.kind).where(
                            Dataset.app_id == actor.app_id
                        )
                    ).mappings()
                ]
            }

    @router.post(prefix + "/sync/credentials")
    def credentials(actor: Actor):
        with user_transaction(runtime.engine, actor.user_id, app_id=actor.app_id) as c:
            if c.scalar(sa.select(User.id).where(User.id == actor.user_id)) is None:
                raise HTTPException(404, "Account unavailable")
        return {
            "endpoint": runtime.sync_endpoint,
            "token": runtime.authority.issue(actor.user_id, SYNC_AUDIENCE, app_id=actor.app_id),
            "user_id": actor.user_id,
            "app_id": actor.app_id,
        }

    @router.get(prefix + "/services")
    def services(actor: Actor):
        service = runtime.files.get(actor.app_id)
        areas = sorted(service.roots) if service else []
        return {"app_id": actor.app_id, "file_areas": areas, "updates": "updates" in areas}

    if runtime.sessions:

        def file_service(actor):
            service = runtime.files.get(actor.app_id)
            if service is None:
                raise HTTPException(404, "application_service_unavailable")
            return service

        @router.get(prefix + "/files/{area}/{path:path}")
        async def read_file(
            area: str, path: str, request: Request, actor: Actor, credentials: Credential
        ):
            read = await file_service(actor).open(
                credentials.credentials, area, path, request.headers.get("range")
            )
            return StreamingResponse(
                read.chunks,
                status_code=read.status,
                headers=read.headers,
                background=BackgroundTask(read.close),
            )

        @router.get(prefix + "/updates/manifest")
        async def manifest(actor: Actor, credentials: Credential, channel: str = "stable"):
            return JSONResponse(
                await file_service(actor).manifest(credentials.credentials, channel),
                headers={"Cache-Control": "no-store"},
            )

    return router
