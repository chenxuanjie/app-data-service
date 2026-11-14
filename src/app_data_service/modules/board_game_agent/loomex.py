"""Authenticated model discovery for the configured Loomex provider."""

import asyncio
import json
import os
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from app_data_service.dependencies import Actor

PREFIX = "/v1/apps/board_game_agent/ai/loomex/v1"
MODEL_LIST_MAX_BYTES = 512 * 1024


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
