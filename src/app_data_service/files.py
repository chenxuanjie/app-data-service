"""Read-only, account-authorized File Station gateway. No arbitrary NAS upstream."""

import asyncio
import json
import mimetypes
import re
from dataclasses import dataclass

import httpx
from fastapi import HTTPException


def relative_path(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or any(c in "\\:%?#" or ord(c) < 32 or ord(c) == 127 for c in value)
    ):
        raise HTTPException(400, "invalid_file_path")
    if any(p in {"", ".", ".."} or len(p) > 200 for p in value.split("/")):
        raise HTTPException(400, "invalid_file_path")
    return value


def file_error(code):
    if type(code) is not int:
        return HTTPException(503, "file_unavailable")
    if code in {106, 107, 119, 150}:
        return HTTPException(401, "session_rejected")
    if code in {105, 403, 404, 407, 411}:
        return HTTPException(403, "file_denied")
    if code == 408:
        return HTTPException(404, "file_missing")
    return HTTPException(503, "file_unavailable")


@dataclass
class FileRead:
    status: int
    headers: dict
    chunks: object
    close: object


class AccountFiles:
    max_bytes = 1024 * 1024 * 1024

    def __init__(self, sessions, roots: dict[str, str], app_id: str):
        self.sessions = sessions
        self.app_id = app_id
        self.roots = dict(roots)
        for area, root in self.roots.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{1,31}", area) or not root.startswith("/"):
                raise ValueError("Explicit application file roots required")
            relative_path(root[1:])
        self.capacity = asyncio.Semaphore(4)

    async def open(self, token, area, path, range_header=None):
        path = relative_path(path)
        root = self.roots.get(area)
        if root is None:
            raise HTTPException(503, "file_service_not_configured")
        offset = None
        if range_header:
            match = re.fullmatch(r"bytes=(0|[1-9][0-9]{0,10})-", range_header)
            if not match:
                raise HTTPException(416, "invalid_range")
            offset = int(match[1])
            if offset >= self.max_bytes:
                raise HTTPException(416, "invalid_range")
        try:
            await asyncio.wait_for(self.capacity.acquire(), 1)
        except TimeoutError as error:
            raise HTTPException(429, "file_service_busy") from error
        client = None
        response = None
        closed = False

        async def close():
            nonlocal closed
            if closed:
                return
            closed = True
            try:
                if response:
                    async with asyncio.timeout(5):
                        await response.aclose()
            finally:
                try:
                    if client:
                        async with asyncio.timeout(5):
                            await client.aclose()
                finally:
                    self.capacity.release()

        try:
            context = await asyncio.to_thread(self.sessions.upstream_context, token, self.app_id)
            provider = self.sessions.provider
            client = provider.client()
            specs = await provider.call(
                client,
                "entry.cgi",
                {
                    "api": "SYNO.API.Info",
                    "version": "1",
                    "method": "query",
                    "query": "SYNO.FileStation.Download",
                },
            )
            spec = specs.get("SYNO.FileStation.Download", {})
            if not isinstance(spec, dict):
                raise HTTPException(503, "file_unavailable")
            api_path = spec.get("path", "")
            if (
                not isinstance(api_path, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\.cgi", api_path)
                or not (
                    type(spec.get("minVersion")) is int
                    and type(spec.get("maxVersion")) is int
                    and spec["minVersion"] <= 2 <= spec["maxVersion"]
                )
            ):
                raise HTTPException(503, "file_unavailable")
            headers = provider.session_headers(context)
            headers["Accept-Encoding"] = "identity"
            if range_header:
                headers["Range"] = range_header
            request = client.build_request(
                "POST",
                provider.origin + "/webapi/" + api_path,
                data={
                    "api": "SYNO.FileStation.Download",
                    "version": "2",
                    "method": "download",
                    "path": json.dumps([root + "/" + path]),
                    "mode": json.dumps("download"),
                },
                headers=headers,
            )
            async with asyncio.timeout(15):
                response = await client.send(request, stream=True)
            if response.status_code not in {200, 206}:
                status = response.status_code
                raise HTTPException(
                    status if status in {401, 403, 404, 416, 429} else 503, "file_unavailable"
                )
            # mode=download returns octet-stream; DSM errors instead return JSON.
            if "json" in response.headers.get("content-type", "").lower():
                payload = bytearray()
                async with asyncio.timeout(10):
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > 65536:
                            raise HTTPException(503, "file_unavailable")
                try:
                    code = json.loads(payload).get("error", {}).get("code")
                except (ValueError, AttributeError):
                    code = None
                raise file_error(code)
            if response.headers.get("content-encoding", "identity") != "identity":
                raise HTTPException(503, "invalid_file_response")
            length = response.headers.get("content-length")
            if length is not None and (
                not length.isascii() or not length.isdigit() or int(length) > self.max_bytes
            ):
                raise HTTPException(503, "invalid_file_response")
            result_headers = {
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Content-Type": mimetypes.guess_type(path)[0] or "application/octet-stream",
            }
            if length is not None:
                result_headers["Content-Length"] = length
            if response.status_code == 206:
                content_range = response.headers.get("content-range", "")
                match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", content_range)
                if not match:
                    raise HTTPException(503, "invalid_file_response")
                start, end, total = map(int, match.groups())
                if offset is None or start != offset or not start <= end < total <= self.max_bytes:
                    raise HTTPException(503, "invalid_file_response")
                if length is not None and int(length) != end - start + 1:
                    raise HTTPException(503, "invalid_file_response")
                result_headers["Content-Range"] = content_range
            if response.headers.get("accept-ranges") == "bytes":
                result_headers["Accept-Ranges"] = "bytes"

            async def chunks():
                received = 0
                try:
                    async with asyncio.timeout(600):
                        async for chunk in response.aiter_bytes(chunk_size=65536):
                            received += len(chunk)
                            if received > self.max_bytes or (
                                length is not None and received > int(length)
                            ):
                                raise RuntimeError("File stream exceeded declared size")
                            yield chunk
                        if length is not None and received != int(length):
                            raise RuntimeError("File stream ended before declared size")
                finally:
                    await close()

            return FileRead(response.status_code, result_headers, chunks(), close)
        except (httpx.HTTPError, TimeoutError) as error:
            await close()
            raise HTTPException(503, "file_unavailable") from error
        except BaseException:
            await close()
            raise

    async def manifest(self, token, channel):
        if channel != "stable":
            raise HTTPException(404, "update_channel_missing")
        read = await self.open(token, "updates", "manifest.json")
        payload = bytearray()
        try:
            async with asyncio.timeout(15):
                async for chunk in read.chunks:
                    payload.extend(chunk)
                    if len(payload) > 128 * 1024:
                        raise HTTPException(503, "invalid_update_manifest")
            manifest = json.loads(payload)
            if manifest.get("appId") != self.app_id or manifest.get("channel") != channel:
                raise ValueError()
            if type(manifest.get("buildNumber")) is not int or manifest["buildNumber"] <= 0:
                raise ValueError()
            artifacts = manifest["artifacts"]
            if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 16:
                raise ValueError()
            for artifact in artifacts:
                relative_path(artifact["path"])
                if (
                    type(artifact.get("size")) is not int
                    or not 0 < artifact["size"] <= self.max_bytes
                ):
                    raise ValueError()
                if not re.fullmatch(r"[a-f0-9]{64}", artifact.get("sha256", "")):
                    raise ValueError()
            return manifest
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            HTTPException,
            TimeoutError,
        ) as error:
            raise HTTPException(503, "invalid_update_manifest") from error
        finally:
            await read.close()
