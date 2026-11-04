"""PowerSync proxy owns both resources through every ASGI response exit."""

import asyncio
from contextlib import suppress

import httpx
import pytest
from fastapi import FastAPI, Request
from http_helpers import serving, until
from starlette.requests import ClientDisconnect

from app_data_service import sync_gateway

CLIENT = httpx.AsyncClient
FIRST = b'{"checkpoint":1}\n'
LAST = b'{"checkpoint":2}\n'


class Source:
    def __init__(self, mode):
        self.mode = mode
        self.clients = []
        self.streams = []
        self.requests = []
        self.gate = asyncio.Event()
        self.closing = asyncio.Event()

    async def request(self, request):
        self.requests.append(request)
        if self.mode == "headers-blocked":
            await self.gate.wait()
        if self.mode == "headers-error":
            raise httpx.ConnectError("fixture connection failure")
        stream = SourceStream(self)
        self.streams.append(stream)
        return httpx.Response(
            206,
            stream=stream,
            headers={"content-type": "application/x-ndjson", "content-encoding": "identity"},
        )

    def all_closed(self):
        return all(c.is_closed for c in self.clients) and all(s.closed for s in self.streams)


class SourceStream(httpx.AsyncByteStream):
    def __init__(self, source):
        self.source = source
        self.closed = False

    async def __aiter__(self):
        yield FIRST
        if self.source.mode in {"stream-blocked", "close-blocked"}:
            await self.source.gate.wait()
        if self.source.mode == "stream-error":
            raise httpx.ReadError("fixture read failure")
        yield LAST

    async def aclose(self):
        self.source.closing.set()
        if self.source.mode == "close-blocked":
            await self.source.gate.wait()
        self.closed = True
        if self.source.mode == "close-error":
            raise RuntimeError("fixture close failure")


def application(monkeypatch, source):
    def create_client(**kwargs):
        client = CLIENT(transport=httpx.MockTransport(source.request), **kwargs)
        source.clients.append(client)
        return client

    monkeypatch.setattr(sync_gateway.httpx, "AsyncClient", create_client)
    app = FastAPI()
    sync_gateway.install(app, "http://powersync.test:8080")
    return app


def test_success_preserves_sync_wire_protocol_and_closes_both_resources(monkeypatch):
    async def scenario():
        source = Source("success")
        async with serving(application(monkeypatch, source)) as origin:
            async with CLIENT(base_url=origin, timeout=3) as client:
                response = await client.post(
                    "/powersync/sync/stream?cursor=fixture",
                    headers={
                        "Authorization": "Bearer fixture",
                        "Content-Type": "application/json",
                        "X-PowerSync-User-Agent": "fixture-sdk",
                        "Cookie": "private=must-not-forward",
                    },
                    content=b'{"checkpoint":0}',
                )
                assert response.status_code == 206
                assert response.content == FIRST + LAST
                assert response.headers["content-type"] == "application/x-ndjson"
                assert response.headers["content-encoding"] == "identity"
                assert response.headers["x-accel-buffering"] == "no"
                assert response.headers["cache-control"] == "no-store"
                sent = source.requests[0]
                assert str(sent.url) == "http://powersync.test:8080/sync/stream?cursor=fixture"
                assert sent.method == "POST" and sent.content == b'{"checkpoint":0}'
                assert sent.headers["Authorization"] == "Bearer fixture"
                assert sent.headers["X-PowerSync-User-Agent"] == "fixture-sdk"
                assert "cookie" not in sent.headers
                await until(source.all_closed)

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["headers-error", "stream-error", "stream-blocked"])
def test_failure_or_real_downstream_disconnect_closes_resources(monkeypatch, mode):
    async def scenario():
        source = Source(mode)
        async with serving(application(monkeypatch, source)) as origin:
            async with CLIENT(base_url=origin, timeout=3) as client:
                if mode == "headers-error":
                    response = await client.get("/powersync/sync/stream")
                    assert response.status_code == 503
                    assert response.json() == {"detail": "Sync service unavailable"}
                elif mode == "stream-error":
                    with pytest.raises(httpx.RemoteProtocolError):
                        await client.get("/powersync/sync/stream")
                else:
                    async with client.stream("GET", "/powersync/sync/stream") as response:
                        assert await anext(response.aiter_bytes()) == FIRST
                        # Close the HTTP socket while the provider is stalled.
                await until(source.all_closed)
                source.mode = "success"
                assert (await client.get("/powersync/sync/stream")).content == FIRST + LAST
                await until(source.all_closed)

    asyncio.run(scenario())


def test_downstream_cancellation_before_response_headers_closes_client(monkeypatch):
    async def scenario():
        source = Source("headers-blocked")
        async with serving(application(monkeypatch, source)) as origin:
            async with CLIENT(base_url=origin, timeout=3) as client:
                pending = asyncio.create_task(client.get("/powersync/sync/stream"))
                try:
                    await until(lambda: bool(source.requests))
                    # HTTPX/Uvicorn only observe this disconnect after their
                    # current header await completes; cancellation itself must
                    # still take the gateway's pre-header cleanup path.
                    handlers = [
                        t
                        for t in asyncio.all_tasks()
                        if t is not asyncio.current_task()
                        and "RequestResponseCycle.run_asgi" in t.get_coro().__qualname__
                    ]
                    assert len(handlers) == 1
                    handlers[0].cancel()
                    await until(source.all_closed)
                finally:
                    pending.cancel()
                    with suppress(asyncio.CancelledError, httpx.HTTPError):
                        await pending

    asyncio.run(scenario())


def request_scope():
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "method": "GET",
        "scheme": "http",
        "path": "/powersync/sync/stream",
        "query_string": b"",
        "headers": [],
    }


def test_request_body_disconnect_closes_client_before_any_upstream_request(monkeypatch):
    async def scenario():
        source = Source("success")
        app = application(monkeypatch, source)
        scope = request_scope()

        async def receive():
            return {"type": "http.disconnect"}

        with pytest.raises(ClientDisconnect):
            await app.router.routes[-1].endpoint("sync/stream", Request(scope, receive))
        assert source.requests == []
        assert len(source.clients) == 1 and source.clients[0].is_closed

    asyncio.run(scenario())


def test_stalled_upstream_close_is_bounded_and_still_closes_client(monkeypatch):
    async def scenario():
        source = Source("close-blocked")
        app = application(monkeypatch, source)
        scope = request_scope()
        monkeypatch.setattr(sync_gateway, "CLOSE_TIMEOUT_SECONDS", 0.05)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            raise OSError("fixture closed before headers")

        response = await app.router.routes[-1].endpoint("sync/stream", Request(scope, receive))
        async with asyncio.timeout(1):
            with pytest.raises(TimeoutError):
                await response(scope, receive, send)
        assert source.closing.is_set()
        assert source.clients[0].is_closed

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["headers-send-error", "close-error", "close-blocked"])
def test_asgi_handoff_and_cleanup_errors_do_not_orphan_the_client(monkeypatch, mode):
    async def scenario():
        source = Source("success" if mode == "headers-send-error" else mode)
        app = application(monkeypatch, source)
        scope = request_scope()

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        first = asyncio.Event()

        async def send(message):
            if mode == "headers-send-error":
                raise OSError("fixture downstream closed before headers")
            if message["type"] == "http.response.body":
                first.set()

        response = await app.router.routes[-1].endpoint("sync/stream", Request(scope, receive))
        if mode == "headers-send-error":
            with pytest.raises(ClientDisconnect):
                await response(scope, receive, send)
        elif mode == "close-error":
            with pytest.raises(RuntimeError, match="fixture close failure"):
                await response(scope, receive, send)
        else:
            task = asyncio.create_task(response(scope, receive, send))
            try:
                await first.wait()
                task.cancel()
                await source.closing.wait()
                task.cancel()  # A second cancellation cannot kill the cleanup owner.
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                source.gate.set()
        await until(source.all_closed)
        await until(
            lambda: not any(t.get_name() == "powersync-proxy-cleanup" for t in asyncio.all_tasks())
        )

    asyncio.run(scenario())
