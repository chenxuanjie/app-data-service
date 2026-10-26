"""Compose shared services and explicitly installed application modules."""

import base64
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import jwt
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import Engine
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from .applications import ApplicationRegistry
from .auth import TokenAuthority
from .browser_sessions import BrowserSessions
from .config import database_url, make_engine
from .dsm import DsmFailure, DsmProvider
from .files import AccountFiles
from .middleware import BoundedBody
from .modules.registry import installed_applications
from .platform_routes import build_router
from .runtime import ServiceRuntime
from .sessions import SessionManager
from .sync_gateway import install as install_sync_gateway


def create_app(
    engine: Engine,
    authority: TokenAuthority,
    sync_endpoint: str,
    *,
    dispose_engine: bool = False,
    sessions: SessionManager | None = None,
    proxy_upstream: str | None = None,
    allowed_origins: list[str] | None = None,
    applications: ApplicationRegistry | None = None,
    application_settings: dict | None = None,
) -> FastAPI:
    endpoint = urlsplit(sync_endpoint)
    if (
        endpoint.scheme not in {"https", "http"}
        or not endpoint.hostname
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
    ):
        raise ValueError("A public sync endpoint without credentials is required")
    applications = applications or (sessions.applications if sessions else installed_applications())
    if sessions and sessions.applications is not applications:
        raise ValueError("Authentication and API must use the same installed registry")
    applications.validate_database(engine)
    settings = applications.validate_settings(application_settings or {})

    @asynccontextmanager
    async def lifespan(_):
        yield
        if dispose_engine:
            engine.dispose()

    app = FastAPI(title="App Data Service", version="0.5.0", lifespan=lifespan)
    runtime = ServiceRuntime(app, engine, authority, applications, sync_endpoint, sessions)
    app.state.runtime, app.state.applications, app.state.engine = runtime, applications, engine
    app.add_middleware(BoundedBody)
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "X-User-Agent",
                "X-PowerSync-User-Agent",
                "X-App-ID",
            ],
            allow_credentials=False,
        )
    if sessions:
        for definition in applications.definitions:
            config = settings.get(definition.id, {})
            if config.get("browser_origin"):
                runtime.browsers[definition.id] = BrowserSessions(
                    sessions, config["browser_origin"], definition.id
                )
            if definition.file_areas:
                runtime.files[definition.id] = AccountFiles(
                    sessions, config.get("file_roots", {}), definition.id
                )
        if len({b.origin for b in runtime.browsers.values()}) != len(runtime.browsers):
            raise ValueError("Each application requires its own browser origin")
    runtime.platform_router = build_router(runtime)
    app.include_router(runtime.platform_router)
    for definition in applications.definitions:
        if definition.router_factory:
            router = definition.router_factory(runtime)
            if any(
                getattr(route, "application_id", None) != definition.id for route in router.routes
            ):
                raise ValueError("Every business route must be bound to its application module")
            app.include_router(router)
    if proxy_upstream:
        install_sync_gateway(app, proxy_upstream)

    @app.exception_handler(DsmFailure)
    async def dsm_error(_, error):
        return JSONResponse({"detail": error.code}, error.status)

    @app.exception_handler(jwt.InvalidTokenError)
    async def invalid_token(_, error):
        return JSONResponse({"detail": "session_rejected"}, 401)

    @app.exception_handler(RequestValidationError)
    async def invalid_body(_, error):
        return JSONResponse({"detail": "Invalid request payload"}, 422)

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

    return app


def from_environment() -> FastAPI:
    key = (
        base64.b64decode(os.environ["APP_DATA_SIGNING_KEY_B64"], validate=True)
        if "APP_DATA_SIGNING_KEY_B64" in os.environ
        else Path(os.environ["APP_DATA_SIGNING_KEY"]).read_bytes()
    )
    authority = TokenAuthority(key, os.environ["APP_DATA_ISSUER"])
    engine = make_engine(database_url())
    applications = installed_applications()
    mode = os.environ.get("APP_DATA_AUTH_MODE", "dsm")
    if mode == "dsm":
        provider = DsmProvider(
            os.environ["APP_DATA_DSM_ORIGIN"],
            identity_origin=os.environ.get("APP_DATA_DSM_IDENTITY_ORIGIN"),
            verify=os.environ.get("APP_DATA_DSM_CA") or True,
        )
        sessions = SessionManager(engine, authority, provider, key, applications=applications)
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
        applications=applications,
        application_settings=applications.settings_from_environment(os.environ),
        proxy_upstream=os.environ.get("APP_DATA_POWERSYNC_UPSTREAM"),
        allowed_origins=[
            v.strip()
            for v in os.environ.get("APP_DATA_ALLOWED_ORIGINS", "").split(",")
            if v.strip()
        ],
    )
