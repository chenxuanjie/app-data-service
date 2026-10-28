"""Bounded HTTP body reads before JSON parsing."""

import asyncio
import re

from fastapi.responses import JSONResponse


class BoundedBody:
    """Bound body reads before FastAPI parses JSON (chunked requests included)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        # Installer uploads authenticate before consuming their bounded streaming
        # body. JSON routes retain their small body limit and ten-second budget.
        if scope["method"] == "PUT" and re.fullmatch(
            r"/v1/apps/[a-z][a-z0-9_]{1,63}/releases/objects/[a-f0-9]{64}", scope["path"]
        ):
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
