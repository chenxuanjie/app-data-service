"""Verify discovery remains available after retiring the generation relay."""

import asyncio

import httpx
import pytest
from fastapi import APIRouter, FastAPI
from http_helpers import serving, until

from app_data_service.dependencies import RequestScope, identity
from app_data_service.modules.board_game_agent import loomex

CLIENT = httpx.AsyncClient
BODY = {"model": "gpt-6-sol", "input": "fixture"}


class Provider:
    def __init__(self):
        self.mode = "success"
        self.clients = []
        self.requests = []

    async def request(self, request):
        self.requests.append(request)
        if request.method == "GET":
            if self.mode == "invalid-list":
                return httpx.Response(200, json={"data": "invalid"})
            if self.mode == "rejected":
                return httpx.Response(429, json={"error": "private upstream message"})
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "gpt-6.1-sol", "owned_by": "openai", "extra": "private field"},
                        {"id": "codex-auto-review"},
                    ]
                },
            )
        raise AssertionError("Retired generation request reached upstream")

    def all_closed(self):
        return all(client.is_closed for client in self.clients)


def application(monkeypatch, provider):
    monkeypatch.setenv("APP_DATA_BOARD_LOOMEX_API_KEY", "fixture-not-a-real-key")
    monkeypatch.setenv("APP_DATA_BOARD_LOOMEX_BASE_URL", "https://provider.invalid/v1")

    def create_client(**kwargs):
        client = CLIENT(transport=httpx.MockTransport(provider.request), **kwargs)
        provider.clients.append(client)
        return client

    monkeypatch.setattr(loomex.httpx, "AsyncClient", create_client)
    app, router = FastAPI(), APIRouter()
    loomex.install(router)
    app.include_router(router)
    app.dependency_overrides[identity] = lambda: RequestScope("fixture", "board_game_agent", None)
    return app


def test_discovery_remains_and_generation_is_retired(monkeypatch):
    async def scenario():
        monkeypatch.setenv("APP_DATA_BOARD_LOOMEX_MODELS", "stale-model")
        provider = Provider()
        async with serving(application(monkeypatch, provider)) as origin:
            async with CLIENT(base_url=origin, timeout=5) as client:
                response = await client.get(loomex.PREFIX + "/models")
                assert response.status_code == 200
                assert response.headers["cache-control"] == "no-store"
                assert response.json() == {
                    "object": "list",
                    "data": [
                        {"id": "gpt-6.1-sol", "object": "model", "owned_by": "openai"},
                        {"id": "codex-auto-review", "object": "model", "owned_by": "loomex"},
                    ],
                }
                assert str(provider.requests[0].url) == "https://provider.invalid/v1/models"
                assert (
                    provider.requests[0].headers["authorization"] == "Bearer fixture-not-a-real-key"
                )
                for operation in ("responses", "responses/compact", "chat/completions"):
                    retired = await client.post(loomex.PREFIX + "/" + operation, json=BODY)
                    assert retired.status_code == 404
                assert len(provider.requests) == 1
                await until(provider.all_closed)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode,status", [("invalid-list", 502), ("rejected", 429)])
def test_model_discovery_errors_release_capacity(monkeypatch, mode, status):
    async def scenario():
        provider = Provider()
        provider.mode = mode
        async with serving(application(monkeypatch, provider)) as origin:
            async with CLIENT(base_url=origin, timeout=5) as client:
                for _ in range(9):
                    response = await client.get(loomex.PREFIX + "/models")
                    assert response.status_code == status
                    assert "private upstream message" not in response.text
                provider.mode = "success"
                assert (await client.get(loomex.PREFIX + "/models")).status_code == 200
                await until(provider.all_closed)

    asyncio.run(scenario())
