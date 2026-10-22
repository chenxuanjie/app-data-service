"""Serve the bundled Flutter app and same-origin API in the existing process."""

from pathlib import Path

from starlette.datastructures import MutableHeaders
from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse
from starlette.staticfiles import StaticFiles


class FlutterFiles(StaticFiles):
    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as error:
            # Browser navigation to a SPA route; missing assets must stay 404.
            if error.status_code != 404 or scope["method"] not in {"GET", "HEAD"}:
                raise
            if any(p.startswith(".") for p in Path(path).parts) or Path(path).suffix:
                raise
            return await super().get_response("index.html", scope)


class SameOriginWeb:
    def __init__(self, app, directory):
        self.app = app
        directory = Path(directory).resolve(strict=True)
        if not all((directory / name).is_file() for name in ("index.html", "main.dart.js")):
            raise ValueError("A complete Flutter Web release directory is required")
        self.files = FlutterFiles(directory=directory, html=True, follow_symlink=False)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope["path"]
        if path.startswith("/api/"):
            # Alias the existing API, retaining headers/cookies and streaming.
            scope = dict(scope, path=path[4:], raw_path=scope["raw_path"][4:])
            return await self.app(scope, receive, send)
        first = path.strip("/").split("/", 1)[0]
        if first in {
            "api",
            "v1",
            "v2",
            "powersync",
            "health",
            ".well-known",
            "docs",
            "redoc",
            "openapi.json",
        }:
            return await self.app(scope, receive, send)

        async def static_send(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-cache"
                headers["Cross-Origin-Opener-Policy"] = "same-origin"
                headers["Cross-Origin-Embedder-Policy"] = "require-corp"
                headers["X-Content-Type-Options"] = "nosniff"
            await send(message)

        try:
            await self.files(scope, receive, static_send)
        except HTTPException as error:
            await PlainTextResponse("Not found", status_code=error.status_code)(
                scope, receive, static_send
            )
