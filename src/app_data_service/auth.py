"""RS256 application and sync tokens. DSM session exchange is a later integration."""

import hashlib
import json
from datetime import UTC, datetime, timedelta

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from .applications import application_id

API_AUDIENCE = "app-data-api"
SYNC_AUDIENCE = "app-data-powersync"


class TokenAuthority:
    def __init__(self, private_pem: bytes, issuer: str):
        key = serialization.load_pem_private_key(private_pem, password=None)
        if not isinstance(key, RSAPrivateKey) or key.key_size < 2048:
            raise ValueError("An RSA key of at least 2048 bits is required")
        self.key = key
        self.issuer = issuer
        public = key.public_key()
        self.kid = hashlib.sha256(
            public.public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        ).hexdigest()[:24]
        self.jwk = {
            **json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public)),
            "kid": self.kid,
            "alg": "RS256",
            "use": "sig",
        }

    def issue(
        self,
        user_id: str,
        audience: str,
        *,
        app_id: str,
        seconds: int = 300,
        extra: dict | None = None,
    ) -> str:
        now = datetime.now(UTC)
        return jwt.encode(
            {
                **(extra or {}),
                "sub": user_id,
                "app_id": application_id(app_id),
                "iss": self.issuer,
                "aud": audience,
                "iat": now,
                "exp": now + timedelta(seconds=seconds),
            },
            self.key,
            algorithm="RS256",
            headers={"kid": self.kid},
        )

    def claims(
        self,
        token: str,
        *,
        audience: str = API_AUDIENCE,
        maximum_age: int = 3600,
        app_id: str | None = None,
    ) -> dict:
        header = jwt.get_unverified_header(token)
        if header.get("kid") != self.kid:
            raise jwt.InvalidTokenError("Unknown signing key")
        claims = jwt.decode(
            token,
            self.key.public_key(),
            algorithms=["RS256"],
            audience=audience,
            issuer=self.issuer,
            options={"require": ["exp", "iat", "sub", "iss", "aud", "app_id"], "strict_aud": True},
        )
        try:
            application_id(claims["app_id"])
        except (KeyError, TypeError, ValueError) as error:
            raise jwt.InvalidTokenError("Unknown application") from error
        if app_id is not None and claims["app_id"] != app_id:
            raise jwt.InvalidTokenError("Wrong application")
        actor = claims["sub"]
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 200 or "\x00" in actor:
            raise jwt.InvalidTokenError("Invalid subject")
        if type(claims["exp"]) is not int or type(claims["iat"]) is not int:
            raise jwt.InvalidTokenError("Timestamps must be integers")
        if not 0 < claims["exp"] - claims["iat"] <= maximum_age:
            raise jwt.InvalidTokenError("Token lifetime exceeds one hour")
        return claims

    def verify(self, token: str, *, app_id: str) -> str:
        return self.claims(token, app_id=app_id)["sub"]
