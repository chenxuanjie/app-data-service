"""Authenticated minimum API; there is deliberately no development login route."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID

import jwt
import sqlalchemy as sa
from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from .auth import SYNC_AUDIENCE, TokenAuthority
from .config import database_url, make_engine
from .models import Dataset, ExpenseConflict, User
from .store import user_transaction
from .sync import ReplayError, Resolution, ScopeError, Upload, apply_upload, resolve_conflict


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
    engine: Engine, authority: TokenAuthority, sync_endpoint: str, *, dispose_engine: bool = False
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_):
        yield
        if dispose_engine:
            engine.dispose()

    app = FastAPI(title="App Data Service", version="0.1.0", lifespan=lifespan)
    app.add_middleware(BoundedBody)
    bearer = HTTPBearer(auto_error=False)

    def identity(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> str:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                401, "Authentication required", headers={"WWW-Authenticate": "Bearer"}
            )
        try:
            return authority.verify(credentials.credentials)
        except jwt.InvalidTokenError as error:
            raise HTTPException(401, "Invalid or expired token") from error

    Actor = Annotated[str, Depends(identity)]

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
            return {
                "conflicts": [
                    dict(row)
                    for row in connection.execute(
                        sa.select(table)
                        .where(table.c.dataset_id == dataset_id, table.c.resolved_at.is_(None))
                        .order_by(table.c.created_at, table.c.id)
                        .limit(100)
                    ).mappings()
                ]
            }

    @app.post("/v1/conflicts/{conflict_id}/resolve")
    def resolve(conflict_id: UUID, body: Resolution, actor: Actor):
        with user_transaction(engine, actor) as connection:
            result = resolve_conflict(connection, conflict_id, body)
        return result

    return app


def from_environment() -> FastAPI:
    # No insecure fallback signing key, dev identity or automatic migration.
    key = Path(os.environ["APP_DATA_SIGNING_KEY"]).read_bytes()
    authority = TokenAuthority(key, os.environ["APP_DATA_ISSUER"])
    return create_app(
        make_engine(database_url()),
        authority,
        os.environ["APP_DATA_SYNC_ENDPOINT"],
        dispose_engine=True,
    )
