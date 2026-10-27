"""Authenticated, fixed-upstream AI relay; never return the deployment credential.

Reuse httpx 0.28.1 manual streaming and Starlette StreamingResponse. The existing
Flutter openai_dart adapter owns protocol parsing, tools, retries and cancellation.
Reference: https://www.python-httpx.org/async/#streaming-responses
"""

import asyncio
import os
from contextlib import suppress
from hashlib import sha256
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from app_data_service.dependencies import Actor

PREFIX = "/v1/apps/board_game_agent/ai/loomex/v1"
DEFAULT_MODEL = "gpt-6-sol"
ALLOWED_OPERATIONS = {"responses", "responses/compact", "chat/completions"}


def install(router):
    # Settings belong to this application module, not authentication or another app.
    key = os.environ.get("APP_DATA_BOARD_LOOMEX_API_KEY", "").strip()
    base = os.environ.get("APP_DATA_BOARD_LOOMEX_BASE_URL", "https://www.loomex.lol/v1").rstrip("/")
    parsed = urlsplit(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Loomex requires a fixed HTTPS upstream without credentials")
    models = tuple(
        dict.fromkeys(
            filter(
                None,
                (
                    item.strip()
                    for item in os.environ.get("APP_DATA_BOARD_LOOMEX_MODELS", DEFAULT_MODEL).split(
                        ","
                    )
                ),
            )
        )
    )
    if not models or DEFAULT_MODEL not in models:
        raise ValueError("Loomex models must include its default model")
    capacity = asyncio.Semaphore(8)

    def configured():
        if not key:
            raise HTTPException(503, "loomex_not_configured")

    @router.get(PREFIX + "/models")
    def list_models(actor: Actor):
        configured()
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {"id": model, "object": "model", "owned_by": "loomex"} for model in models
                ],
            },
            headers={"Cache-Control": "no-store"},
        )

    @router.post(PREFIX + "/{operation:path}")
    async def request_model(operation: str, request: Request, actor: Actor):
        configured()
        if operation not in ALLOWED_OPERATIONS or request.url.query:
            raise HTTPException(404, "ai_operation_unavailable")
        try:
            body = await request.json()
        except (ValueError, UnicodeError) as error:
            raise HTTPException(422, "ai_payload_invalid") from error
        if not isinstance(body, dict) or body.get("model") not in models:
            raise HTTPException(422, "loomex_model_unavailable")
        # A shared provider key must not expose another account's stored responses,
        # arbitrary URLs, provider credentials or asynchronous background jobs.
        if any(
            name in body
            for name in (
                "api_key",
                "base_url",
                "previous_response_id",
                "conversation",
                "client_secret",
            )
        ) or body.get("background"):
            raise HTTPException(422, "ai_payload_invalid")
        if operation == "responses":
            body["store"] = False
            body.setdefault("reasoning", {"effort": "low"})
        elif operation == "chat/completions":
            body.setdefault("reasoning_effort", "low")
        if operation != "responses/compact":
            body.setdefault("service_tier", "default")
        for field in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
            if field in body and (type(body[field]) is not int or not 1 <= body[field] <= 32768):
                raise HTTPException(422, "ai_token_budget_invalid")
        try:
            await asyncio.wait_for(capacity.acquire(), timeout=2)
        except TimeoutError as error:
            raise HTTPException(503, "loomex_busy") from error
        client = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(connect=8, read=60, write=15, pool=5),
        )
        upstream = None
        closed = False

        async def close():
            nonlocal closed
            if closed:
                return
            closed = True
            try:
                try:
                    if upstream is not None:
                        await upstream.aclose()
                finally:
                    await client.aclose()
            finally:
                capacity.release()

        async def disconnected():
            while not await request.is_disconnected():
                await asyncio.sleep(0.2)

        headers = {"Authorization": "Bearer " + key, "Accept-Encoding": "identity"}
        identifier = request.headers.get("idempotency-key")
        if identifier and len(identifier) <= 200:
            headers["Idempotency-Key"] = sha256(
                f"board_game_agent|{actor.user_id}|{identifier}".encode()
            ).hexdigest()
        try:
            outgoing = client.build_request(
                "POST", base + "/" + operation, json=body, headers=headers
            )
        except (ValueError, httpx.InvalidURL):
            await close()
            raise HTTPException(422, "ai_payload_invalid") from None
        pending = asyncio.create_task(client.send(outgoing, stream=True))
        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait(
                {pending, watcher}, timeout=45, return_when=asyncio.FIRST_COMPLETED
            )
            if pending not in done:
                pending.cancel()
                with suppress(asyncio.CancelledError, httpx.HTTPError):
                    await pending
                await close()
                raise HTTPException(499 if watcher in done else 504, "ai_request_interrupted")
            upstream = await pending
            if upstream.status_code >= 300:
                status = upstream.status_code
                await close()
                # Never forward raw provider errors, credentials, redirects or cookies.
                status = 429 if status == 429 else 422 if status in {400, 404, 422} else 503
                return JSONResponse(
                    {
                        "error": {
                            "message": (
                                "Loomex request unavailable; retry or select another model service."
                            ),
                            "type": "loomex_error",
                            "code": "loomex_upstream_unavailable",
                        }
                    },
                    status_code=status,
                    headers={"Cache-Control": "no-store"},
                )
        except (httpx.HTTPError, TimeoutError):
            await close()
            raise HTTPException(503, "loomex_unavailable") from None
        except BaseException:
            pending.cancel()
            with suppress(asyncio.CancelledError, httpx.HTTPError):
                await pending
            await close()
            raise
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher

        async def content():
            size = 0
            try:
                async with asyncio.timeout(180):
                    async for chunk in upstream.aiter_bytes():
                        size += len(chunk)
                        if size > 16 * 1024 * 1024:
                            raise RuntimeError("AI response exceeded its budget")
                        yield chunk
            finally:
                await close()

        class RelayResponse(StreamingResponse):
            async def __call__(self, scope, receive, send):
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    await asyncio.shield(close())

        return RelayResponse(
            content(),
            headers={
                "Content-Type": upstream.headers.get("content-type", "application/json"),
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
            background=BackgroundTask(close),
        )
