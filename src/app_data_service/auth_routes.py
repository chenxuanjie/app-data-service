"""Application-independent authentication routes and bounded DSM exchange."""

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .browser_sessions import BrowserSessions
from .dependencies import Application
from .dsm import DsmFailure
from .sessions import REFRESH_AUDIENCE, REFRESH_LIFETIME

logger = logging.getLogger(__name__)
LOGIN_TIMEOUT_SECONDS = 30.0


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=4096)
    otp: str | None = Field(default=None, max_length=32)


class Refresh(BaseModel):
    model_config = ConfigDict(extra="forbid")
    refresh_token: str = Field(min_length=1, max_length=8192)


def build_router(runtime, *, router=None, prefix="/v1/apps/{application_id}"):
    router = APIRouter() if router is None else router
    sessions, authority, browsers = runtime.sessions, runtime.authority, runtime.browsers
    if runtime.auth_quota is None:
        runtime.auth_quota = OrderedDict()
        runtime.auth_capacity = asyncio.Semaphore(4)
    quota, capacity = runtime.auth_quota, runtime.auth_capacity

    async def authenticate(body: Login, request: Request, app_id: str):
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
            async with asyncio.timeout(LOGIN_TIMEOUT_SECONDS):
                verified = await sessions.provider.login(body.account, body.password, body.otp)
                started = time.monotonic()
                try:
                    return await asyncio.to_thread(sessions.create, verified, app_id)
                except Exception as error:
                    logger.warning(
                        "authentication_failed phase=create_session error_type=%s elapsed_ms=%d",
                        type(error).__name__,
                        round((time.monotonic() - started) * 1000),
                    )
                    await sessions.provider.logout(verified.context)
                    raise
        except TimeoutError as error:
            logger.warning(
                "authentication_failed phase=login reason=total_deadline budget_ms=%d",
                round(LOGIN_TIMEOUT_SECONDS * 1000),
            )
            raise DsmFailure("provider_timeout", 504, reason="login_deadline") from error
        finally:
            capacity.release()

    @router.post(prefix + "/auth/login")
    async def login(body: Login, request: Request, app_id: Application):
        return JSONResponse(
            await authenticate(body, request, app_id), headers={"Cache-Control": "no-store"}
        )

    if browsers:

        def browser_for(app_id: Application) -> BrowserSessions:
            browser = browsers.get(app_id)
            if browser is None:
                raise HTTPException(404, "browser_service_not_configured")
            return browser

        Browser = Annotated[BrowserSessions, Depends(browser_for)]

        @router.post(prefix + "/auth/browser/bootstrap")
        async def browser_bootstrap(request: Request, browser: Browser):
            # Same quota as authentication; no uncontrolled anonymous session allocation.
            key = "browser:" + (request.client.host if request.client else "unknown")
            now = time.monotonic()
            start, count = quota.get(key, (now, 0))
            if now - start >= 60:
                start, count = now, 0
            quota[key] = (start, count + 1)
            quota.move_to_end(key)
            if len(quota) > 1024:
                quota.popitem(last=False)
            if count >= 30:
                raise HTTPException(429, "rate_limited")
            return await asyncio.to_thread(browser.bootstrap, request)

        @router.post(prefix + "/auth/browser/login")
        async def browser_login(body: Login, request: Request, browser: Browser):
            identifier = browser.check(request)
            await asyncio.to_thread(browser.begin_login, identifier)
            result = None
            try:
                result = await authenticate(body, request, browser.app_id)
                data = await asyncio.to_thread(browser.finish_login, identifier, result)
                return JSONResponse(data, headers={"Cache-Control": "no-store"})
            except Exception:
                if result:
                    claims = authority.claims(
                        result["refresh_token"],
                        audience=REFRESH_AUDIENCE,
                        maximum_age=REFRESH_LIFETIME,
                    )
                    context = await asyncio.to_thread(
                        sessions.revoke, claims["sub"], claims["session_id"], browser.app_id
                    )
                    await sessions.provider.logout(context)
                await asyncio.to_thread(browser.reset_login, identifier)
                raise

        @router.post(prefix + "/auth/browser/restore")
        async def browser_restore(request: Request, browser: Browser):
            try:
                return await browser.restore(request)
            except TimeoutError as error:
                raise DsmFailure("provider_timeout", 504, reason="restore_deadline") from error

        @router.post(prefix + "/auth/browser/logout")
        async def browser_logout(request: Request, browser: Browser):
            return await browser.logout(request)

    @router.post(prefix + "/auth/refresh")
    async def refresh(body: Refresh, app_id: Application):
        try:
            async with asyncio.timeout(15):
                return await sessions.refresh(body.refresh_token, app_id)
        except TimeoutError as error:
            raise DsmFailure("provider_timeout", 504, reason="refresh_deadline") from error

    @router.post(prefix + "/auth/logout")
    async def logout(body: Refresh, app_id: Application):
        claims = authority.claims(
            body.refresh_token,
            audience=REFRESH_AUDIENCE,
            maximum_age=REFRESH_LIFETIME,
            app_id=app_id,
        )
        context = await asyncio.to_thread(
            sessions.revoke, claims["sub"], claims.get("session_id"), app_id
        )
        await sessions.provider.logout(context)
        return {"revoked": True}

    return router
