"""Fixed-upstream PowerSync transport; authorization belongs to signed streams."""

import asyncio
from contextlib import suppress

import anyio
import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse

CLOSE_TIMEOUT_SECONDS = 5.0


def install(app, proxy_upstream):
    @app.api_route("/powersync/{path:path}", methods=["GET", "POST"])
    async def sync_proxy(path: str, request: Request):
        # Fixed container upstream; no caller-supplied host or DSM credentials.
        client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(90, connect=5))
        upstream = None
        cleanup = None

        async def close_resources():
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                    if upstream is not None:
                        await upstream.aclose()
            finally:
                # A broken response close must not skip closing its client.
                async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                    await client.aclose()

        async def close():
            nonlocal cleanup
            if cleanup is None:
                cleanup = asyncio.create_task(close_resources(), name="powersync-proxy-cleanup")

                def observe(task):
                    with suppress(asyncio.CancelledError, Exception):
                        task.result()

                cleanup.add_done_callback(observe)
            # Starlette may cancel an AnyIO scope or the ASGI task directly.
            # Keep one bounded cleanup owner alive through either cancellation.
            with anyio.CancelScope(shield=True):
                await asyncio.shield(cleanup)

        class GatewayResponse(StreamingResponse):
            async def __call__(self, scope, receive, send):
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    await close()

        target = proxy_upstream.rstrip("/") + "/" + path
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower()
            in {
                "authorization",
                "content-type",
                "accept",
                "x-user-agent",
                "x-powersync-user-agent",
                "user-agent",
            }
        }
        try:
            upstream = await client.send(
                client.build_request(
                    request.method,
                    target,
                    params=request.query_params,
                    headers=headers,
                    content=await request.body(),
                ),
                stream=True,
            )
            return GatewayResponse(
                upstream.aiter_raw(),
                status_code=upstream.status_code,
                headers={
                    "X-Accel-Buffering": "no",
                    "Cache-Control": "no-store",
                    **{
                        key: value
                        for key, value in upstream.headers.items()
                        if key.lower() in {"content-type", "content-encoding"}
                    },
                },
            )
        except httpx.HTTPError:
            await close()
            return JSONResponse({"detail": "Sync service unavailable"}, 503)
        except BaseException:
            await close()
            raise
