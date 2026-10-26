"""Revocable application sessions; upstream SID stays encrypted on the server."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import sqlalchemy as sa
from cryptography.fernet import Fernet, InvalidToken

from app_data_service.models import AppSession, Dataset

from .auth import API_AUDIENCE, TokenAuthority
from .dsm import DsmFailure, DsmProvider, VerifiedDsm
from .store import user_transaction

REFRESH_AUDIENCE = "app-data-refresh"
REFRESH_LIFETIME = 30 * 86400


class SessionManager:
    def __init__(
        self,
        engine,
        authority: TokenAuthority,
        provider: DsmProvider,
        encryption_key: bytes,
        *,
        applications=None,
    ):
        self.engine, self.authority, self.provider = engine, authority, provider
        if applications is None:
            from .modules.registry import installed_applications

            applications = installed_applications()
        self.applications = applications
        self.browser_csrf_key = hashlib.sha256(b"browser-csrf-v1|" + encryption_key).digest()
        self.cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(encryption_key).digest()))

    def access(self, actor: str, session_id, app_id: str) -> str:
        return self.authority.issue(
            actor, API_AUDIENCE, app_id=app_id, seconds=900, extra={"session_id": str(session_id)}
        )

    def create(self, verified: VerifiedDsm, app_id: str) -> dict:
        self.applications.get(app_id)
        actor, session_id = verified.user_id, uuid4()
        refresh = self.authority.issue(
            actor,
            REFRESH_AUDIENCE,
            app_id=app_id,
            seconds=REFRESH_LIFETIME,
            extra={"session_id": str(session_id), "nonce": str(uuid4())},
        )
        with user_transaction(self.engine, actor, app_id=app_id) as c:
            self.applications.ensure_enabled(c, app_id)
            dataset_id = c.scalar(
                sa.text("SELECT core.ensure_app_account(:name)"),
                {"name": verified.display_name},
            )
            dataset_kind = c.scalar(sa.select(Dataset.kind).where(Dataset.id == dataset_id))
            c.execute(
                sa.insert(AppSession).values(
                    id=session_id,
                    user_id=actor,
                    app_id=app_id,
                    refresh_hash=hashlib.sha256(refresh.encode()).hexdigest(),
                    dsm_context=self.cipher.encrypt(json.dumps(verified.context).encode()).decode(),
                    expires_at=datetime.now(UTC) + timedelta(seconds=REFRESH_LIFETIME),
                )
            )
        return {
            "access_token": self.access(actor, session_id, app_id),
            "app_id": app_id,
            "refresh_token": refresh,
            "user_id": actor,
            "display_name": verified.display_name,
            "dataset_id": dataset_id,
            "dataset_kind": dataset_kind,
        }

    def session(self, actor: str, session_id, app_id: str, *, refresh: str | None = None) -> dict:
        try:
            identifier = UUID(str(session_id))
        except ValueError as error:
            raise jwt.InvalidTokenError("Invalid session") from error
        with user_transaction(self.engine, actor, app_id=app_id) as c:
            self.applications.ensure_enabled(c, app_id)
            row = (
                c.execute(
                    sa.select(AppSession).where(
                        AppSession.id == identifier,
                        AppSession.user_id == actor,
                        AppSession.app_id == app_id,
                        AppSession.revoked_at.is_(None),
                        AppSession.expires_at > datetime.now(UTC),
                    )
                )
                .mappings()
                .one_or_none()
            )
        if (
            not row
            or refresh is not None
            and not hmac.compare_digest(
                row["refresh_hash"], hashlib.sha256(refresh.encode()).hexdigest()
            )
        ):
            raise jwt.InvalidTokenError("Session unavailable")
        return dict(row)

    def verify_access(self, token: str, app_id: str) -> str:
        claims = self.authority.claims(token, app_id=app_id)
        self.session(claims["sub"], claims.get("session_id"), app_id)
        return claims["sub"]

    def upstream_context(self, token: str, app_id: str) -> dict:
        claims = self.authority.claims(token, app_id=app_id)
        row = self.session(claims["sub"], claims.get("session_id"), app_id)
        try:
            return json.loads(self.cipher.decrypt(row["dsm_context"].encode()))
        except (InvalidToken, ValueError) as error:
            raise jwt.InvalidTokenError("Session unavailable") from error

    async def refresh(self, token: str, app_id: str) -> dict:
        import asyncio

        claims = self.authority.claims(
            token, audience=REFRESH_AUDIENCE, maximum_age=REFRESH_LIFETIME, app_id=app_id
        )
        actor = claims["sub"]
        row = await asyncio.to_thread(
            self.session, actor, claims.get("session_id"), app_id, refresh=token
        )
        try:
            context = json.loads(self.cipher.decrypt(row["dsm_context"].encode()))
        except InvalidToken as error:
            raise jwt.InvalidTokenError("Session key changed; login required") from error
        verified = await self.provider.profile(context)
        if verified != actor:
            raise DsmFailure("session_rejected", 401)
        # Recheck after network I/O: a concurrent logout must not revive a session.
        await asyncio.to_thread(self.session, actor, row["id"], app_id, refresh=token)
        return {
            "access_token": self.access(actor, row["id"], app_id),
            "user_id": actor,
            "app_id": app_id,
        }

    def revoke(self, actor, session_id, app_id):
        row = self.session(actor, session_id, app_id)
        with user_transaction(self.engine, actor, app_id=app_id) as c:
            c.execute(
                sa.update(AppSession)
                .where(AppSession.id == row["id"])
                .values(revoked_at=datetime.now(UTC), dsm_context="")
            )
        return json.loads(self.cipher.decrypt(row["dsm_context"].encode()))
