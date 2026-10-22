"""Fixed-origin DSM Auth v6 + FileStation.Info v2 identity verification."""

import asyncio
import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


class DsmFailure(Exception):
    def __init__(self, code="provider_unavailable", status=503):
        self.code, self.status = code, status
        super().__init__(code)


@dataclass
class VerifiedDsm:
    user_id: str
    display_name: str
    context: dict


class DsmProvider:
    def __init__(
        self,
        origin: str,
        *,
        identity_origin: str | None = None,
        verify: bool | str = True,
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

    async def call(self, client, path: str, fields: dict) -> dict:
        try:
            async with asyncio.timeout(10):
                return await self._call(client, path, fields)
        except TimeoutError as error:
            raise DsmFailure() from error

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
                        else DsmFailure()
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
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as error:
            raise DsmFailure() from error

    def client(self):
        return httpx.AsyncClient(
            verify=self.verify,
            transport=self.transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(8, connect=4),
        )

    async def profile(self, context: dict) -> str:
        async with self.client() as client:
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
            async with self.client() as client:
                specs = await self.call(
                    client,
                    "entry.cgi",
                    {
                        "api": "SYNO.API.Info",
                        "version": "1",
                        "method": "query",
                        "query": "SYNO.API.Auth,SYNO.FileStation.Info",
                    },
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
                )
                if not isinstance(data.get("sid"), str) or not 1 <= len(data["sid"]) <= 8192:
                    raise DsmFailure()
                context = {
                    "sid": data["sid"],
                    "synotoken": data.get("synotoken"),
                    "auth_path": auth_path,
                    "info_path": info_path,
                }
            user_id = await self.profile(context)
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
