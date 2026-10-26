"""Same-origin opaque browser cookies; no refresh credential is exposed to JS."""

import asyncio
import hashlib
import hmac
import re
import secrets
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import jwt
import sqlalchemy as sa
from cryptography.fernet import InvalidToken
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from .applications import application_id
from .origins import browser_origin
from .sessions import REFRESH_AUDIENCE, REFRESH_LIFETIME


class BrowserSessions:
    def __init__(self, sessions, public_origin, app_id):
        self.sessions = sessions
        self.app_id = application_id(app_id)
        self.cookie = "__Host-app-session-" + app_id
        self.origin = browser_origin(public_origin)
        # Domain-separated secret derived from the existing session encryption key.
        # A stable HMAC key across API restarts/replicas, never published.
        self.csrf_key = sessions.browser_csrf_key

    def origin_check(self, request: Request):
        if request.headers.get("origin") != self.origin:
            raise HTTPException(403, "origin_rejected")
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise HTTPException(415, "json_required")

    def identifier(self, request):
        value = request.cookies.get(self.cookie, "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
            raise HTTPException(401, "session_rejected")
        return value

    def csrf(self, identifier):
        return hmac.new(
            self.csrf_key,
            (self.app_id + "|" + identifier).encode(),
            hashlib.sha256,
        ).hexdigest()

    def check(self, request):
        self.origin_check(request)
        identifier = self.identifier(request)
        candidate = request.headers.get("x-csrf-token", "")
        if not re.fullmatch(r"[a-f0-9]{64}", candidate) or not hmac.compare_digest(
            self.csrf(identifier), candidate
        ):
            raise HTTPException(403, "csrf_rejected")
        return identifier

    @contextmanager
    def transaction(self, identifier):
        digest = hashlib.sha256(identifier.encode()).hexdigest()
        with self.sessions.engine.begin() as c:
            c.exec_driver_sql("SET LOCAL transaction_timeout = '20s'")
            c.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
            c.execute(sa.text("SELECT set_config('app.browser_id', :id, true)"), {"id": digest})
            c.execute(
                sa.text("SELECT set_config('app.app_id', :app_id, true)"), {"app_id": self.app_id}
            )
            yield c, digest

    def bootstrap(self, request):
        self.origin_check(request)
        try:
            identifier = self.identifier(request)
            record = self.record(identifier)
            authenticated = record["refresh_ciphertext"] is not None
        except HTTPException:
            authenticated = False
            identifier = secrets.token_urlsafe(32)
            with self.transaction(identifier) as (c, digest):
                c.execute(
                    sa.text(
                        "INSERT INTO core.browser_sessions(id_hash,app_id,expires_at) "
                        "VALUES(:id,:app_id,now()+interval '10 minutes')"
                    ),
                    {"id": digest, "app_id": self.app_id},
                )
        response = JSONResponse(
            {"csrf_token": self.csrf(identifier), "authenticated": authenticated},
            headers={"Cache-Control": "no-store"},
        )
        response.set_cookie(
            self.cookie,
            identifier,
            max_age=REFRESH_LIFETIME,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    def record(self, identifier):
        with self.transaction(identifier) as (c, digest):
            row = (
                c.execute(
                    sa.text(
                        "SELECT * FROM core.browser_sessions WHERE id_hash=:id "
                        "AND revoked_at IS NULL AND expires_at>now()"
                    ),
                    {"id": digest},
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise HTTPException(401, "session_rejected")
        return dict(row)

    def begin_login(self, identifier):
        with self.transaction(identifier) as (c, digest):
            found = c.execute(
                sa.text(
                    "UPDATE core.browser_sessions SET login_started=true "
                    "WHERE id_hash=:id AND revoked_at IS NULL AND expires_at>now() "
                    "AND refresh_ciphertext IS NULL AND NOT login_started RETURNING id_hash"
                ),
                {"id": digest},
            ).scalar_one_or_none()
        if found is None:
            raise HTTPException(409, "session_busy")

    def finish_login(self, identifier, result):
        if result.get("app_id") != self.app_id:
            raise HTTPException(401, "session_rejected")
        encrypted = self.sessions.cipher.encrypt(result["refresh_token"].encode()).decode()
        with self.transaction(identifier) as (c, digest):
            found = c.execute(
                sa.text(
                    "UPDATE core.browser_sessions SET refresh_ciphertext=:value, "
                    "expires_at=:expires WHERE id_hash=:id AND revoked_at IS NULL "
                    "AND expires_at>now() AND login_started AND refresh_ciphertext IS NULL "
                    "RETURNING id_hash"
                ),
                {
                    "id": digest,
                    "value": encrypted,
                    "expires": datetime.now(UTC) + timedelta(seconds=REFRESH_LIFETIME),
                },
            ).scalar_one_or_none()
        if found is None:
            raise HTTPException(401, "session_rejected")
        return {k: v for k, v in result.items() if k != "refresh_token"}

    def reset_login(self, identifier):
        with self.transaction(identifier) as (c, digest):
            c.execute(
                sa.text(
                    "UPDATE core.browser_sessions SET login_started=false "
                    "WHERE id_hash=:id AND revoked_at IS NULL AND refresh_ciphertext IS NULL"
                ),
                {"id": digest},
            )

    def refresh_token(self, identifier):
        row = self.record(identifier)
        if row["refresh_ciphertext"] is None:
            raise HTTPException(401, "session_rejected")
        try:
            return self.sessions.cipher.decrypt(row["refresh_ciphertext"].encode()).decode()
        except InvalidToken as error:
            raise HTTPException(401, "session_rejected") from error

    async def restore(self, request):
        identifier = self.check(request)
        token = await asyncio.to_thread(self.refresh_token, identifier)
        async with asyncio.timeout(15):
            result = await self.sessions.refresh(token, self.app_id)
        # Concurrent logout must also invalidate a response already in flight.
        await asyncio.to_thread(self.record, identifier)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    def revoke(self, identifier):
        with self.transaction(identifier) as (c, digest):
            encrypted = c.execute(
                sa.text(
                    "SELECT refresh_ciphertext FROM core.browser_sessions "
                    "WHERE id_hash=:id AND revoked_at IS NULL FOR UPDATE"
                ),
                {"id": digest},
            ).scalar_one_or_none()
            context = None
            if encrypted:
                try:
                    token = self.sessions.cipher.decrypt(encrypted.encode()).decode()
                    claims = self.sessions.authority.claims(
                        token,
                        audience=REFRESH_AUDIENCE,
                        maximum_age=REFRESH_LIFETIME,
                        app_id=self.app_id,
                    )
                    # Revoke the app session before discarding the only server
                    # credential; on DB failure this browser transaction rolls back.
                    context = self.sessions.revoke(claims["sub"], claims["session_id"], self.app_id)
                except (jwt.InvalidTokenError, InvalidToken):
                    pass
            c.execute(
                sa.text(
                    "UPDATE core.browser_sessions SET revoked_at=now(), "
                    "refresh_ciphertext=NULL WHERE id_hash=:id"
                ),
                {"id": digest},
            )
            return context

    async def logout(self, request):
        identifier = self.check(request)
        context = await asyncio.to_thread(self.revoke, identifier)
        if context:
            await self.sessions.provider.logout(context)
        response = JSONResponse({"revoked": True}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(self.cookie, path="/", secure=True, httponly=True, samesite="strict")
        return response
