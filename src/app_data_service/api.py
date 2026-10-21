"""Authenticated minimum API; there is deliberately no development login route."""

import asyncio
import base64
import os
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID

import httpx
import jwt
import sqlalchemy as sa
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from .auth import SYNC_AUDIENCE, TokenAuthority
from .config import database_url, make_engine
from .dsm import DsmFailure, DsmProvider
from .entities import ENTITY_MODELS
from .models import Dataset, ExpenseConflict, SyncConflict, User
from .sessions import REFRESH_AUDIENCE, REFRESH_LIFETIME, SessionManager
from .store import user_transaction
from .sync import (
    ReplayError,
    Resolution,
    ResolutionV2,
    ScopeError,
    Upload,
    UploadV2,
    apply_upload,
    current_record,
    resolve_conflict,
    snapshot,
)


class BoundedBody:
    """Bound body reads before FastAPI parses JSON (chunked requests included)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        body = bytearray()
        try:
            async with asyncio.timeout(10):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > 512 * 1024:
                        return await JSONResponse({"detail": "Request too large"}, 413)(
                            scope, receive, send
                        )
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            return await JSONResponse({"detail": "Request body timeout"}, 408)(scope, receive, send)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(
    engine: Engine,
    authority: TokenAuthority,
    sync_endpoint: str,
    *,
    dispose_engine: bool = False,
    sessions: SessionManager | None = None,
    proxy_upstream: str | None = None,
    allowed_origins: list[str] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_):
        yield
        if dispose_engine:
            engine.dispose()

    app = FastAPI(title="App Data Service", version="0.2.0", lifespan=lifespan)
    app.add_middleware(BoundedBody)
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "X-User-Agent",
                "X-PowerSync-User-Agent",
            ],
            allow_credentials=False,
        )
    bearer = HTTPBearer(auto_error=False)

    def identity(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> str:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                401, "Authentication required", headers={"WWW-Authenticate": "Bearer"}
            )
        try:
            return (
                sessions.verify_access(credentials.credentials)
                if sessions
                else authority.verify(credentials.credentials)
            )
        except jwt.InvalidTokenError as error:
            raise HTTPException(401, "Invalid or expired token") from error

    Actor = Annotated[str, Depends(identity)]

    if proxy_upstream:

        @app.api_route("/powersync/{path:path}", methods=["GET", "POST"])
        async def sync_proxy(path: str, request: Request):
            # Fixed container upstream; no caller-supplied host or DSM credentials.
            client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(90, connect=5))
            target = proxy_upstream.rstrip("/") + "/" + path
            headers = {
                key: value
                for key, value in request.headers.items()
                if key.lower()
                in {
                    "authorization",
                    "content-type",
                    "accept",
                    "x-user-agent",
                    "x-powersync-user-agent",
                    "user-agent",
                }
            }
            try:
                upstream = await client.send(
                    client.build_request(
                        request.method,
                        target,
                        params=request.query_params,
                        headers=headers,
                        content=await request.body(),
                    ),
                    stream=True,
                )
            except httpx.HTTPError:
                await client.aclose()
                return JSONResponse({"detail": "Sync service unavailable"}, 503)

            async def close():
                await upstream.aclose()
                await client.aclose()

            return StreamingResponse(
                upstream.aiter_raw(),
                status_code=upstream.status_code,
                headers={
                    "X-Accel-Buffering": "no",
                    "Cache-Control": "no-store",
                    **{
                        key: value
                        for key, value in upstream.headers.items()
                        if key.lower() in {"content-type", "content-encoding"}
                    },
                },
                background=BackgroundTask(close),
            )

    @app.exception_handler(DsmFailure)
    async def dsm_error(_, error):
        return JSONResponse({"detail": error.code}, error.status)

    @app.exception_handler(jwt.InvalidTokenError)
    async def invalid_token(_, error):
        return JSONResponse({"detail": "session_rejected"}, 401)

    if sessions:
        quota: OrderedDict[str, tuple[float, int]] = OrderedDict()
        capacity = asyncio.Semaphore(4)

        @app.post("/v1/auth/login")
        async def login(body: Login, request: Request):
            key = request.client.host if request.client else "unknown"
            now = time.monotonic()
            start, count = quota.get(key, (now, 0))
            if now - start >= 60:
                start, count = now, 0
            quota[key] = (start, count + 1)
            quota.move_to_end(key)
            if len(quota) > 1024:
                quota.popitem(last=False)
            if count >= 10:
                raise HTTPException(429, "rate_limited")
            try:
                await asyncio.wait_for(capacity.acquire(), timeout=1)
            except TimeoutError as error:
                raise HTTPException(429, "rate_limited") from error
            try:
                async with asyncio.timeout(30):
                    verified = await sessions.provider.login(body.account, body.password, body.otp)
                    try:
                        return await asyncio.to_thread(sessions.create, verified)
                    except Exception:
                        await sessions.provider.logout(verified.context)
                        raise
            except TimeoutError as error:
                raise DsmFailure("provider_unavailable", 503) from error
            finally:
                capacity.release()

        @app.post("/v1/auth/refresh")
        async def refresh(body: Refresh):
            try:
                async with asyncio.timeout(15):
                    return await sessions.refresh(body.refresh_token)
            except TimeoutError as error:
                raise DsmFailure() from error

        @app.post("/v1/auth/logout")
        async def logout(body: Refresh):
            claims = authority.claims(
                body.refresh_token, audience=REFRESH_AUDIENCE, maximum_age=REFRESH_LIFETIME
            )
            context = await asyncio.to_thread(
                sessions.revoke, claims["sub"], claims.get("session_id")
            )
            await sessions.provider.logout(context)
            return {"revoked": True}

    @app.exception_handler(RequestValidationError)
    async def invalid_body(_, error):
        # Do not echo raw authentication, payload or SQL values into errors.
        return JSONResponse({"detail": "Invalid request payload"}, 422)

    @app.exception_handler(ScopeError)
    async def forbidden(_, error):
        return JSONResponse({"detail": str(error)}, 404)

    @app.exception_handler(ReplayError)
    async def stale(_, error):
        return JSONResponse({"detail": str(error)}, 409)

    @app.exception_handler(DBAPIError)
    async def database_failure(_, error):
        code = getattr(error.orig, "sqlstate", None)
        status = 409 if code in {"23505", "23503", "23514"} else 503
        return JSONResponse(
            {"detail": "Write rejected" if status == 409 else "Database unavailable"}, status
        )

    @app.exception_handler(PoolTimeout)
    async def pool_failure(_, error):
        return JSONResponse({"detail": "Database busy"}, 503)

    @app.get("/health")
    def health():
        with engine.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        return {"status": "ok"}

    @app.get("/.well-known/jwks.json")
    def jwks():
        return {"keys": [authority.jwk]}

    @app.post("/v1/sync/credentials")
    def credentials(actor: Actor):
        with user_transaction(engine, actor) as connection:
            if connection.scalar(sa.select(User.id).where(User.id == actor)) is None:
                raise ScopeError("Account unavailable")
        return {
            "endpoint": sync_endpoint,
            "token": authority.issue(actor, SYNC_AUDIENCE),
            "user_id": actor,
        }

    @app.get("/v1/datasets")
    def datasets(actor: Actor):
        with user_transaction(engine, actor) as connection:
            return {
                "datasets": [
                    dict(row)
                    for row in connection.execute(
                        sa.select(Dataset.id, Dataset.name).where(Dataset.app_id == "zheri")
                    ).mappings()
                ]
            }

    @app.get("/v1/capabilities")
    def capabilities(actor: Actor):
        return {"sync_protocols": [1, 2], "entities": list(ENTITY_MODELS), "max_mutations": 100}

    @app.post("/v2/sync/upload")
    def upload_v2(body: UploadV2, actor: Actor):
        with user_transaction(engine, actor) as connection:
            result = apply_upload(connection, actor, body)
        return result

    @app.get("/v2/conflicts")
    def conflicts_v2(actor: Actor, dataset_id: str):
        result = []
        with user_transaction(engine, actor) as connection:
            for entity_type in ENTITY_MODELS:
                table = (ExpenseConflict if entity_type == "expenses" else SyncConflict).__table__
                conditions = [table.c.dataset_id == dataset_id, table.c.resolved_at.is_(None)]
                if entity_type != "expenses":
                    conditions.append(table.c.entity_type == entity_type)
                rows = connection.execute(
                    sa.select(table)
                    .where(*conditions)
                    .order_by(table.c.created_at, table.c.id)
                    .limit(100)
                ).mappings()
                for row in rows:
                    current = current_record(
                        connection,
                        dataset_id,
                        row["record_id"],
                        lock=False,
                        entity_type=entity_type,
                    )
                    result.append(
                        {
                            **dict(row),
                            "entity_type": entity_type,
                            "current_record": snapshot(current),
                            "current_version": current["row_version"] if current else 0,
                        }
                    )
        result.sort(key=lambda row: (row["created_at"], str(row["id"])))
        return {"conflicts": result[:100]}

    @app.post("/v2/conflicts/{conflict_id}/resolve")
    def resolve_v2(conflict_id: UUID, body: ResolutionV2, actor: Actor):
        with user_transaction(engine, actor) as connection:
            result = resolve_conflict(connection, conflict_id, body, entity_type=body.entity_type)
        return result

    @app.post("/v1/sync/upload")
    def upload(body: Upload, actor: Actor):
        with user_transaction(engine, actor) as connection:
            result = apply_upload(connection, actor, body)
        # HTTP acknowledgement happens only after the transaction commits.
        return result

    @app.get("/v1/conflicts")
    def conflicts(actor: Actor, dataset_id: str):
        table = ExpenseConflict.__table__
        with user_transaction(engine, actor) as connection:
            rows = (
                connection.execute(
                    sa.select(table)
                    .where(table.c.dataset_id == dataset_id, table.c.resolved_at.is_(None))
                    .order_by(table.c.created_at, table.c.id)
                    .limit(100)
                )
                .mappings()
                .all()
            )
            result = []
            for row in rows:
                current = current_record(connection, dataset_id, row["record_id"], lock=False)
                result.append(
                    {
                        **dict(row),
                        "current_record": snapshot(current),
                        "current_version": current["row_version"] if current else 0,
                    }
                )
            return {"conflicts": result}

    @app.post("/v1/conflicts/{conflict_id}/resolve")
    def resolve(conflict_id: UUID, body: Resolution, actor: Actor):
        with user_transaction(engine, actor) as connection:
            result = resolve_conflict(connection, conflict_id, body)
        return result

    return app


def from_environment() -> FastAPI:
    # No insecure fallback signing key, dev identity or automatic migration.
    key = (
        base64.b64decode(os.environ["APP_DATA_SIGNING_KEY_B64"], validate=True)
        if "APP_DATA_SIGNING_KEY_B64" in os.environ
        else Path(os.environ["APP_DATA_SIGNING_KEY"]).read_bytes()
    )
    authority = TokenAuthority(key, os.environ["APP_DATA_ISSUER"])
    engine = make_engine(database_url())
    mode = os.environ.get("APP_DATA_AUTH_MODE", "dsm")
    if mode == "dsm":
        provider = DsmProvider(
            os.environ["APP_DATA_DSM_ORIGIN"],
            identity_origin=os.environ.get("APP_DATA_DSM_IDENTITY_ORIGIN"),
            verify=os.environ.get("APP_DATA_DSM_CA") or True,
        )
        sessions = SessionManager(engine, authority, provider, key)
    elif mode == "probe" and authority.issuer == "local-sync-probe":
        sessions = None
    else:
        raise ValueError("Unsupported authentication mode")
    return create_app(
        engine,
        authority,
        os.environ["APP_DATA_SYNC_ENDPOINT"],
        dispose_engine=True,
        sessions=sessions,
        proxy_upstream=os.environ.get("APP_DATA_POWERSYNC_UPSTREAM"),
        allowed_origins=[
            value.strip()
            for value in os.environ.get("APP_DATA_ALLOWED_ORIGINS", "").split(",")
            if value.strip()
        ],
    )


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=4096)
    otp: str | None = Field(default=None, max_length=32)


class Refresh(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str = Field(min_length=1, max_length=8192)
