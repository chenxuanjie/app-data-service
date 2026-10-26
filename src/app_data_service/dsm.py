"""Fixed-origin DSM Auth v6 + FileStation.Info v2 identity verification."""

import asyncio
import hashlib
import logging
import re
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)


class DsmFailure(Exception):
    def __init__(self, code="provider_unavailable", status=503, *, reason="upstream_error"):
        self.code, self.status = code, status
        self.reason = reason
        super().__init__(code)


@dataclass
class VerifiedDsm:
    user_id: str
    display_name: str
    context: dict


class DsmProvider:
    # The authentication route still bounds the entire login at 30 seconds.
    # Give a cold DSM Auth/FileStation response room inside that shared budget;
    # refresh/file/logout requests retain their shorter normal deadlines.
    LOGIN_READ_TIMEOUT = 20.0
    LOGIN_CALL_TIMEOUT = 25.0

    def __init__(
        self,
        origin: str,
        *,
        identity_origin: str | None = None,
        verify: bool | str | ssl.SSLContext = True,
        transport=None,
    ):
        self.origin = self.validate_origin(origin)
        self.identity_origin = self.validate_origin(identity_origin or origin)
        self.verify, self.transport = verify, transport

    @staticmethod
    def validate_origin(origin):
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("A fixed DSM HTTPS origin is required")
        return origin.rstrip("/")

    async def call(self, client, path: str, fields: dict, *, timeout=10.0) -> dict:
        started = time.monotonic()
        phase = {
            ("SYNO.API.Info", "query"): "discovery",
            ("SYNO.API.Auth", "login"): "login",
            ("SYNO.FileStation.Info", "get"): "identity_check",
            ("SYNO.API.Auth", "logout"): "logout",
        }.get((fields.get("api"), fields.get("method")), "other")
        try:
            async with asyncio.timeout(timeout):
                return await self._call(client, path, fields)
        except TimeoutError as error:
            logger.warning(
                "dsm_request_failed phase=%s reason=deadline elapsed_ms=%d",
                phase,
                round((time.monotonic() - started) * 1000),
            )
            raise DsmFailure("provider_timeout", 504, reason="deadline") from error
        except DsmFailure as error:
            logger.warning(
                "dsm_request_failed phase=%s reason=%s code=%s elapsed_ms=%d",
                phase,
                error.reason,
                error.code,
                round((time.monotonic() - started) * 1000),
            )
            raise

    @staticmethod
    def session_headers(context):
        sid, token = context.get("sid"), context.get("synotoken")
        if (
            not isinstance(sid, str)
            or not sid
            or not sid.isascii()
            or any(c in sid for c in "\r\n;")
        ):
            raise DsmFailure("session_rejected", 401)
        headers = {"Cookie": "id=" + sid}
        if token:
            if not isinstance(token, str) or not token.isascii() or any(c in token for c in "\r\n"):
                raise DsmFailure("session_rejected", 401)
            headers["X-SYNO-TOKEN"] = token
        return headers

    async def _call(self, client, path: str, fields: dict) -> dict:
        if not isinstance(path, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\.cgi", path
        ):
            raise DsmFailure()
        try:
            fields = dict(fields)
            headers = {}
            sid, synotoken = fields.pop("_sid", None), fields.pop("SynoToken", None)
            if sid:
                if not isinstance(sid, str) or not sid.isascii() or any(c in sid for c in "\r\n;"):
                    raise DsmFailure()
                headers["Cookie"] = "id=" + sid
            if synotoken:
                if (
                    not isinstance(synotoken, str)
                    or not synotoken.isascii()
                    or any(c in synotoken for c in "\r\n")
                ):
                    raise DsmFailure()
                headers["X-SYNO-TOKEN"] = synotoken
            async with client.stream(
                "POST", self.origin + "/webapi/" + path, data=fields, headers=headers
            ) as response:
                if response.status_code != 200:
                    raise (
                        DsmFailure("rate_limited", 429)
                        if response.status_code == 429
                        else DsmFailure(reason=f"http_status_{response.status_code}")
                    )
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 65536:
                        raise DsmFailure()
                import json

                body = json.loads(data)
            if body.get("success") is not True:
                code = body.get("error", {}).get("code")
                if fields.get("method") == "login":
                    mapped = {
                        400: ("invalid_credentials", 401),
                        401: ("account_denied", 403),
                        402: ("account_denied", 403),
                        403: ("otp_required", 401),
                        406: ("otp_required", 401),
                        404: ("invalid_otp", 401),
                        407: ("rate_limited", 429),
                        408: ("password_change_required", 403),
                        409: ("password_change_required", 403),
                        410: ("password_change_required", 403),
                    }
                    raise DsmFailure(*mapped.get(code, ("provider_unavailable", 503)))
                raise DsmFailure("session_rejected", 401)
            result = body.get("data", {})
            if not isinstance(result, dict):
                raise DsmFailure()
            return result
        except DsmFailure:
            raise
        except httpx.TimeoutException as error:
            raise DsmFailure("provider_timeout", 504, reason=type(error).__name__) from error
        except httpx.HTTPError as error:
            raise DsmFailure(reason=type(error).__name__) from error
        except (ValueError, TypeError, AttributeError) as error:
            raise DsmFailure(reason="invalid_response") from error

    def client(self, *, for_login=False):
        return httpx.AsyncClient(
            verify=self.verify,
            transport=self.transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(self.LOGIN_READ_TIMEOUT if for_login else 8, connect=4),
        )

    async def profile(self, context: dict, *, for_login=False) -> str:
        async with self.client(for_login=for_login) as client:
            profile = await self.call(
                client,
                context["info_path"],
                {
                    "api": "SYNO.FileStation.Info",
                    "version": "2",
                    "method": "get",
                    "_sid": context["sid"],
                    **({"SynoToken": context["synotoken"]} if context.get("synotoken") else {}),
                },
                timeout=self.LOGIN_CALL_TIMEOUT if for_login else 10,
            )
        uid = profile.get("uid")
        if type(uid) is not int or uid <= 0:
            raise DsmFailure("session_rejected", 401)
        return hashlib.sha256(
            f"synology-direct-v1|{self.identity_origin}|{uid}".encode()
        ).hexdigest()

    async def login(self, account: str, password: str, otp: str | None) -> VerifiedDsm:
        context = None
        try:
            async with self.client(for_login=True) as client:
                specs = await self.call(
                    client,
                    "entry.cgi",
                    {
                        "api": "SYNO.API.Info",
                        "version": "1",
                        "method": "query",
                        "query": "SYNO.API.Auth,SYNO.FileStation.Info",
                    },
                    timeout=self.LOGIN_CALL_TIMEOUT,
                )
                for name, version in [("SYNO.API.Auth", 6), ("SYNO.FileStation.Info", 2)]:
                    spec = specs.get(name, {})
                    if (
                        not isinstance(spec, dict)
                        or not isinstance(spec.get("minVersion"), int)
                        or not isinstance(spec.get("maxVersion"), int)
                        or not spec["minVersion"] <= version <= spec["maxVersion"]
                    ):
                        raise DsmFailure()
                auth_path, info_path = (
                    specs["SYNO.API.Auth"].get("path"),
                    specs["SYNO.FileStation.Info"].get("path"),
                )
                data = await self.call(
                    client,
                    auth_path,
                    {
                        "api": "SYNO.API.Auth",
                        "version": "6",
                        "method": "login",
                        "account": account,
                        "passwd": password,
                        "session": "FileStation",
                        "format": "sid",
                        "enable_syno_token": "yes",
                        **({"otp_code": otp} if otp else {}),
                    },
                    timeout=self.LOGIN_CALL_TIMEOUT,
                )
                if not isinstance(data.get("sid"), str) or not 1 <= len(data["sid"]) <= 8192:
                    raise DsmFailure()
                context = {
                    "sid": data["sid"],
                    "synotoken": data.get("synotoken"),
                    "auth_path": auth_path,
                    "info_path": info_path,
                }
            user_id = await self.profile(context, for_login=True)
            return VerifiedDsm(user_id, account, context)
        except Exception:
            if context:
                await self.logout(context)
            raise

    async def logout(self, context: dict):
        try:
            async with self.client() as client:
                await self.call(
                    client,
                    context["auth_path"],
                    {
                        "api": "SYNO.API.Auth",
                        "version": "6",
                        "method": "logout",
                        "session": "FileStation",
                        "_sid": context["sid"],
                        **({"SynoToken": context["synotoken"]} if context.get("synotoken") else {}),
                    },
                )
        except DsmFailure:
            pass  # App session is independently revoked even if DSM cannot be reached.
