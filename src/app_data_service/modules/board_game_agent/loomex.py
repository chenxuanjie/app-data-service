"""Authenticated, fixed-upstream AI relay; never return the deployment credential.

Reuse httpx 0.28.1 manual streaming and Starlette StreamingResponse. The existing
Flutter openai_dart adapter owns protocol parsing, tools, retries and cancellation.
Reference: https://www.python-httpx.org/async/#streaming-responses
"""

import asyncio
import json
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
MODEL_LIST_MAX_BYTES = 512 * 1024
ALLOWED_OPERATIONS = {"responses", "responses/compact", "chat/completions"}
DISCONNECT_CLEANUP_SECONDS = 1.0


def loomex_base_url():
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
    return base


def install(router):
    # Settings belong to this application module, not authentication or another app.
    key = os.environ.get("APP_DATA_BOARD_LOOMEX_API_KEY", "").strip()
    base = loomex_base_url()
    capacity = asyncio.Semaphore(8)

    def configured():
        if not key:
            raise HTTPException(503, "loomex_not_configured")

    async def acquire_capacity():
        try:
            await asyncio.wait_for(capacity.acquire(), timeout=2)
        except TimeoutError:
            raise HTTPException(503, "loomex_busy") from None

    @router.get(PREFIX + "/models")
    async def list_models(actor: Actor):
        configured()
        await acquire_capacity()
        try:
            async with (
                asyncio.timeout(15),
                httpx.AsyncClient(trust_env=False, follow_redirects=False) as client,
                client.stream(
                    "GET",
                    base + "/models",
                    headers={
                        "Authorization": "Bearer " + key,
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                ) as upstream,
            ):
                if upstream.status_code == 429:
                    raise HTTPException(429, "loomex_upstream_unavailable")
                upstream.raise_for_status()
                body = bytearray()
                async for chunk in upstream.aiter_bytes():
                    if len(body) + len(chunk) > MODEL_LIST_MAX_BYTES:
                        raise HTTPException(502, "loomex_model_list_invalid")
                    body.extend(chunk)
            try:
                payload = json.loads(body)
            except (ValueError, RecursionError):
                raise HTTPException(502, "loomex_model_list_invalid") from None
            entries = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(entries, list):
                raise HTTPException(502, "loomex_model_list_invalid")
            models = []
            for entry in entries:
                model = entry.get("id") if isinstance(entry, dict) else None
                if not isinstance(model, str) or not model.strip():
                    raise HTTPException(502, "loomex_model_list_invalid")
                owner = entry.get("owned_by")
                models.append(
                    {
                        "id": model,
                        "object": "model",
                        "owned_by": owner if isinstance(owner, str) else "loomex",
                    }
                )
        except (httpx.HTTPError, TimeoutError):
            raise HTTPException(503, "loomex_unavailable") from None
        finally:
            capacity.release()
        return JSONResponse(
            {"object": "list", "data": models}, headers={"Cache-Control": "no-store"}
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
        model = body.get("model") if isinstance(body, dict) else None
        if not isinstance(model, str) or not model.strip():
            raise HTTPException(422, "ai_payload_invalid")
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
        await acquire_capacity()
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

        stop_watching = asyncio.Event()

        async def disconnected():
            # is_disconnected() uses an AnyIO cancellation scope. An asyncio
            # cancel arriving inside that probe may be consumed by the scope,
            # so the monitor needs an independent, cooperative exit condition.
            while not stop_watching.is_set():
                if await request.is_disconnected():
                    return
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop_watching.wait(), timeout=0.2)

        async def stop_watcher():
            stop_watching.set()
            try:
                # wait_for(task) can itself wait indefinitely for cancellation
                # acknowledgement. asyncio.wait bounds only this join instead.
                await asyncio.wait({watcher}, timeout=DISCONNECT_CLEANUP_SECONDS)
            finally:
                if not watcher.done():
                    watcher.cancel()

                def observe(task):
                    with suppress(asyncio.CancelledError, Exception):
                        task.result()

                watcher.add_done_callback(observe)

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
        watcher = asyncio.create_task(disconnected(), name="loomex-header-disconnect")
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
            try:
                await stop_watcher()
            except BaseException:
                # Cancellation during the header-to-stream handoff must not
                # orphan the successful response or retain its capacity slot.
                await close()
                raise

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
