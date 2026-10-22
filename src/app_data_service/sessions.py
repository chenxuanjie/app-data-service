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

from .auth import API_AUDIENCE, TokenAuthority
from .dsm import DsmFailure, DsmProvider, VerifiedDsm
from .models import AppSession, Dataset
from .store import user_transaction

REFRESH_AUDIENCE = "app-data-refresh"
REFRESH_LIFETIME = 30 * 86400


class SessionManager:
    def __init__(
        self, engine, authority: TokenAuthority, provider: DsmProvider, encryption_key: bytes
    ):
        self.engine, self.authority, self.provider = engine, authority, provider
        self.browser_csrf_key = hashlib.sha256(b"browser-csrf-v1|" + encryption_key).digest()
        self.cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(encryption_key).digest()))

    def access(self, actor: str, session_id) -> str:
        return self.authority.issue(
            actor, API_AUDIENCE, seconds=900, extra={"session_id": str(session_id)}
        )

    def create(self, verified: VerifiedDsm) -> dict:
        actor, session_id = verified.user_id, uuid4()
        refresh = self.authority.issue(
            actor,
            REFRESH_AUDIENCE,
            seconds=REFRESH_LIFETIME,
            extra={"session_id": str(session_id), "nonce": str(uuid4())},
        )
        with user_transaction(self.engine, actor) as c:
            dataset_id = c.scalar(
                sa.text("SELECT core.ensure_personal_account(:name)"),
                {"name": verified.display_name},
            )
            dataset_kind = c.scalar(sa.select(Dataset.kind).where(Dataset.id == dataset_id))
            c.execute(
                sa.insert(AppSession).values(
                    id=session_id,
                    user_id=actor,
                    refresh_hash=hashlib.sha256(refresh.encode()).hexdigest(),
                    dsm_context=self.cipher.encrypt(json.dumps(verified.context).encode()).decode(),
                    expires_at=datetime.now(UTC) + timedelta(seconds=REFRESH_LIFETIME),
                )
            )
        return {
            "access_token": self.access(actor, session_id),
            "refresh_token": refresh,
            "user_id": actor,
            "display_name": verified.display_name,
            "dataset_id": dataset_id,
            "dataset_kind": dataset_kind,
        }

    def session(self, actor: str, session_id, *, refresh: str | None = None) -> dict:
        try:
            identifier = UUID(str(session_id))
        except ValueError as error:
            raise jwt.InvalidTokenError("Invalid session") from error
        with user_transaction(self.engine, actor) as c:
            row = (
                c.execute(
                    sa.select(AppSession).where(
                        AppSession.id == identifier,
                        AppSession.user_id == actor,
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

    def verify_access(self, token: str) -> str:
        claims = self.authority.claims(token)
        self.session(claims["sub"], claims.get("session_id"))
        return claims["sub"]

    async def refresh(self, token: str) -> dict:
        import asyncio

        claims = self.authority.claims(
            token, audience=REFRESH_AUDIENCE, maximum_age=REFRESH_LIFETIME
        )
        actor = claims["sub"]
        row = await asyncio.to_thread(self.session, actor, claims.get("session_id"), refresh=token)
        try:
            context = json.loads(self.cipher.decrypt(row["dsm_context"].encode()))
        except InvalidToken as error:
            raise jwt.InvalidTokenError("Session key changed; login required") from error
        verified = await self.provider.profile(context)
        if verified != actor:
            raise DsmFailure("session_rejected", 401)
        # Recheck after network I/O: a concurrent logout must not revive a session.
        await asyncio.to_thread(self.session, actor, row["id"], refresh=token)
        return {"access_token": self.access(actor, row["id"]), "user_id": actor}

    def revoke(self, actor, session_id):
        row = self.session(actor, session_id)
        with user_transaction(self.engine, actor) as c:
            c.execute(
                sa.update(AppSession)
                .where(AppSession.id == row["id"])
                .values(revoked_at=datetime.now(UTC), dsm_context="")
            )
        return json.loads(self.cipher.decrypt(row["dsm_context"].encode()))
