"""Fixed-upstream PowerSync transport; authorization belongs to signed streams."""

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse


def install(app, proxy_upstream):
    @app.api_route("/powersync/{path:path}", methods=["GET", "POST"])
    async def sync_proxy(path: str, request: Request):
        # Fixed container upstream; no caller-supplied host or DSM credentials.
        client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(90, connect=5))
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
        except httpx.HTTPError:
            await client.aclose()
            return JSONResponse({"detail": "Sync service unavailable"}, 503)

        async def close():
            await upstream.aclose()
            await client.aclose()

        return StreamingResponse(
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
            background=BackgroundTask(close),
        )
