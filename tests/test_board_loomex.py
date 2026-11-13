"""Real downstream HTTP covers the Loomex header/stream ownership handoff.

Only the provider is simulated. Uvicorn, HTTPX and Starlette use the installed
dependencies, including the receive/cancellation behavior missing in ASGI mocks.
"""

import asyncio
import json

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request
from http_helpers import serving, until

from app_data_service.dependencies import RequestScope, identity
from app_data_service.modules.board_game_agent import loomex

CLIENT = httpx.AsyncClient
PATH = loomex.PREFIX + "/responses"
BODY = {"model": "gpt-6-sol", "input": "fixture"}


class Provider:
    def __init__(self):
        self.mode = "success"
        self.delay = 0
        self.clients = []
        self.streams = []
        self.started = 0
        self.requests = []
        self.gate = asyncio.Event()

    async def request(self, request):
        self.started += 1
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
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.mode == "headers":
            await self.gate.wait()
        if self.mode == "failure":
            raise httpx.ConnectError("fixture provider failure")
        stream = ProviderStream(self.mode, self.gate)
        self.streams.append(stream)
        return httpx.Response(
            429 if self.mode == "rejected" else 200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    def all_closed(self):
        return all(c.is_closed for c in self.clients) and all(s.closed for s in self.streams)


class ProviderStream(httpx.AsyncByteStream):
    def __init__(self, mode, gate):
        self.mode, self.gate, self.closed = mode, gate, False

    async def __aiter__(self):
        yield b"data: first\n\n"
        if self.mode == "stream":
            await self.gate.wait()
        if self.mode == "stream-error":
            raise httpx.ReadError("fixture stream failure")
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True


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


async def successful_batch(client, count=9):
    responses = await asyncio.gather(*(client.post(PATH, json=BODY) for _ in range(count)))
    assert [r.status_code for r in responses] == [200] * count
    assert all(r.content.endswith(b"data: [DONE]\n\n") for r in responses)


@pytest.mark.parametrize("delay", [0, 0.05])
def test_fast_and_delayed_success_release_monitor_stream_and_capacity(monkeypatch, delay):
    async def scenario():
        provider = Provider()
        provider.delay = delay
        app = application(monkeypatch, provider)
        async with serving(app) as origin, CLIENT(base_url=origin, timeout=5) as client:
            await successful_batch(client)
            await until(provider.all_closed)
            await successful_batch(client)
            await until(provider.all_closed)
            assert len(provider.clients) == 18
            assert not any(t.get_name() == "loomex-header-disconnect" for t in asyncio.all_tasks())

    asyncio.run(scenario())


@pytest.mark.parametrize("mode,status", [("failure", 503), ("rejected", 429)])
def test_provider_failures_release_capacity_and_recover(monkeypatch, mode, status):
    async def scenario():
        provider = Provider()
        provider.mode = mode
        async with serving(application(monkeypatch, provider)) as origin:
            async with CLIENT(base_url=origin, timeout=5) as client:
                for _ in range(9):
                    response = await client.post(PATH, json=BODY)
                    assert response.status_code == status
                    assert "fixture provider failure" not in response.text
                await until(provider.all_closed)
                provider.mode = "success"
                await successful_batch(client)
                await until(provider.all_closed)

    asyncio.run(scenario())


def test_all_eight_slots_saturate_then_downstream_cancellation_restores_them(monkeypatch):
    async def scenario():
        provider = Provider()
        provider.mode = "headers"
        async with serving(application(monkeypatch, provider)) as origin:
            async with CLIENT(base_url=origin, timeout=6) as client:
                requests = [asyncio.create_task(client.post(PATH, json=BODY)) for _ in range(8)]
                try:
                    await until(lambda: provider.started == 8)
                    busy = await client.post(PATH, json=BODY)
                    assert busy.status_code == 503
                    assert busy.json()["detail"] == "loomex_busy"
                    assert provider.started == 8
                finally:
                    for task in requests:
                        task.cancel()
                    await asyncio.gather(*requests, return_exceptions=True)
                await until(provider.all_closed)
                provider.mode = "success"
                await successful_batch(client)
                await until(provider.all_closed)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["stream", "stream-error"])
def test_disconnect_and_stream_failure_close_resources_and_allow_new_requests(monkeypatch, mode):
    async def scenario():
        provider = Provider()
        provider.mode = mode
        async with serving(application(monkeypatch, provider)) as origin:
            async with CLIENT(base_url=origin, timeout=5) as client:
                if mode == "stream":
                    async with client.stream(
                        "POST", PATH, json={**BODY, "stream": True}
                    ) as response:
                        assert response.status_code == 200
                        assert await anext(response.aiter_bytes()) == b"data: first\n\n"
                        # Close the actual downstream HTTP socket before the
                        # provider gate opens: this is not Task.cancel alone.
                else:
                    with pytest.raises(httpx.RemoteProtocolError):
                        await client.post(PATH, json=BODY)
                await until(provider.all_closed)
                provider.mode = "success"
                await successful_batch(client)
                await until(provider.all_closed)

    asyncio.run(scenario())


def test_header_handoff_has_a_bounded_monitor_join(monkeypatch):
    async def scenario():
        async def blocked_probe(self):
            await asyncio.Event().wait()

        monkeypatch.setattr(Request, "is_disconnected", blocked_probe)
        monkeypatch.setattr(loomex, "DISCONNECT_CLEANUP_SECONDS", 0.05)
        provider = Provider()
        async with serving(application(monkeypatch, provider)) as origin:
            async with CLIENT(base_url=origin, timeout=2) as client:
                await successful_batch(client)
                await until(provider.all_closed)
                await until(
                    lambda: (
                        not any(
                            t.get_name() == "loomex-header-disconnect" for t in asyncio.all_tasks()
                        )
                    )
                )

    asyncio.run(scenario())


def test_discovers_upstream_models_and_forwards_new_model_ids(monkeypatch):
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
                response = await client.post(PATH, json={**BODY, "model": "gpt-6.1-sol"})
                assert response.status_code == 200
                assert json.loads(provider.requests[1].content)["model"] == "gpt-6.1-sol"
                invalid = await client.post(PATH, json={**BODY, "model": []})
                assert invalid.status_code == 422
                assert len(provider.requests) == 2
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
