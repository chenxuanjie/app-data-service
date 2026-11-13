import asyncio
import json
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from http_helpers import serving, until
from psycopg import Connection, sql
from sqlalchemy.engine import make_url
from test_board_agent_runtime import model_stream
from test_sessions import authenticated as authenticated_fixture

from app_data_service.api import create_app
from app_data_service.dependencies import (
    RequestScope,
    application_router,
    identity,
    request_application,
)
from app_data_service.modules.board_game_agent import agent_http, agent_runtime
from app_data_service.modules.board_game_agent.agent_checkpoint import setup_checkpoint_database

PATH = "/v1/apps/board_game_agent/ai/agent"


@pytest.fixture(name="accounts")
def account_fixture(db):
    yield from authenticated_fixture.__wrapped__(db)


def body(run, *, thread="thread", navigation=False):
    return {
        "threadId": thread,
        "runId": run,
        "messages": [{"id": run, "role": "user", "content": "fixture"}],
        "tools": [{"name": "open_rankings", "description": "fixture", "parameters": {}}]
        if navigation
        else [],
    }


def resume(run, interrupt_id, *, thread="thread"):
    return {
        "threadId": thread,
        "runId": run,
        "messages": [],
        "resume": [
            {
                "interruptId": interrupt_id,
                "status": "resolved",
                "payload": {"status": "opened"},
            }
        ],
    }


def events(response):
    return [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]


@asynccontextmanager
async def endpoint(monkeypatch, provider):
    monkeypatch.setenv("APP_DATA_BOARD_LOOMEX_API_KEY", "fixture")
    model = agent_runtime.loomex_model
    app = FastAPI()
    app.dependency_overrides[request_application] = lambda: "board_game_agent"
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
        monkeypatch.setattr(agent_http, "loomex_model", lambda _: model(upstream))
        service = agent_http.AgentService(None)
        router = application_router("board_game_agent", lifespan=service.lifespan)
        service.install(router, PATH)
        app.include_router(router)

        def authenticated(request: Request):
            user = request.headers.get("authorization")
            if not user:
                raise HTTPException(401)
            return RequestScope(user, "board_game_agent", "session")

        app.dependency_overrides[identity] = authenticated
        app.dependency_overrides[agent_http._agent_identity] = authenticated
        async with app.router.lifespan_context(app), serving(app) as url:
            async with httpx.AsyncClient(
                base_url=url, headers={"authorization": "alice"}, trust_env=False
            ) as client:
                yield client, service


def navigation_stream():
    response = model_stream({})
    response.read()
    return httpx.Response(
        200,
        headers=response.headers,
        content=response.content.replace(b"get_favorite_rankings", b"open_rankings"),
    )


def test_agui_stream_tool_and_resume(monkeypatch):
    requests = []

    def provider(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return next(responses)

    responses = iter(
        [
            model_stream({"limit": 1, "locale": "en"}),
            model_stream(["First ", "is Cabo."]),
            navigation_stream(),
            model_stream(["Rankings opened."]),
            navigation_stream(),
            model_stream(error="private provider diagnostics"),
        ]
    )
    monkeypatch.setattr(agent_runtime, "read_favorite_rankings", lambda *_: {"items": ["Cabo"]})

    async def scenario():
        async with endpoint(monkeypatch, provider) as (client, _):
            assert (await client.get(PATH + "/health")).json()["status"] == "ready"
            assert (
                await client.post(PATH, json=body("missing"), headers={"authorization": ""})
            ).status_code == 401
            response = await client.post(PATH, json=body("read"))
            assert response.headers["content-type"].startswith("text/event-stream")
            read = events(response)
            kinds = [event["type"] for event in read]
            assert kinds[0] == "RUN_STARTED" and kinds[-1] == "RUN_FINISHED", read
            assert {
                "TOOL_CALL_START",
                "TOOL_CALL_ARGS",
                "TOOL_CALL_END",
                "TOOL_CALL_RESULT",
                "TEXT_MESSAGE_START",
                "TEXT_MESSAGE_CONTENT",
                "TEXT_MESSAGE_END",
            } <= set(kinds), read
            assert (await client.post(PATH, json=body("read"))).status_code == 409

            payload = body("navigation", navigation=True)
            paused = events(await client.post(PATH, json=payload))
            outcome = paused[-1]["outcome"]
            assert outcome["type"] == "interrupt"
            pending = outcome["interrupts"][0]
            assert pending["reason"] == "client_tool"
            assert pending["toolCallId"] == next(
                e["toolCallId"] for e in paused if e["type"] == "TOOL_CALL_START"
            )
            assert (await client.post(PATH, json=resume("bad", "wrong"))).status_code == 409
            assert (
                await client.post(
                    PATH, json=resume("foreign", pending["id"]), headers={"authorization": "bob"}
                )
            ).status_code == 404
            result = resume("continued", pending["id"])
            resumed = events(await client.post(PATH, json=result))
            assert resumed[-1]["type"] == "RUN_FINISHED"
            assert "Rankings opened." in str(resumed)
            assert (await client.post(PATH, json=result)).status_code == 409

            paused_again = events(await client.post(PATH, json=body("stop", navigation=True)))
            pending_id = paused_again[-1]["outcome"]["interrupts"][0]["id"]
            assert (await client.post(PATH + "/thread/runs/stop/cancel")).status_code == 204
            assert (await client.post(PATH, json=resume("late", pending_id))).status_code == 410
            failure = events(await client.post(PATH, json=body("failed", thread="new-thread")))
            assert failure[-1]["type"] == "RUN_ERROR"
            assert failure[-1]["code"] == "agent_run_failed"
            assert "private provider diagnostics" not in str(failure)

    asyncio.run(scenario())
    assert [tool["name"] for tool in requests[0]["tools"]] == ["get_favorite_rankings"]
    assert any(
        item["type"] == "function_call_output" and "opened" in item["output"]
        for item in requests[3]["input"]
    )


def test_postgres_checkpoint_survives_restart_and_isolates_threads(db, monkeypatch):
    database = "agent_test_" + uuid4().hex

    def dsn(role):
        return (
            make_url(db.urls[role])
            .set(drivername="postgresql", database=database if role != "admin" else None)
            .render_as_string(hide_password=False)
        )

    monkeypatch.setenv("APP_DATA_BOARD_AGENT_CHECKPOINT_DSN", dsn("runtime"))
    payloads = []
    responses = iter(
        [
            model_stream(["Remember Cabo."]),
            navigation_stream(),
            model_stream(["Opened."]),
            model_stream(["Cabo."]),
        ]
    )

    def provider(request):
        payloads.append(json.loads(request.content))
        return next(responses)

    async def scenario():
        setup_checkpoint_database(dsn("migrator"), "app_runtime")
        async with endpoint(monkeypatch, provider) as (client, service):
            assert (await client.get(PATH + "/health")).json()["checkpoint"] == "postgres"
            assert (
                events(await client.post(PATH, json=body("remember")))[-1]["type"] == "RUN_FINISHED"
            )
            outcome = events(await client.post(PATH, json=body("pause", navigation=True)))[-1]
            pending = outcome["outcome"]["interrupts"][0]["id"]
            assert service.threads["thread"].pending == pending

        async with endpoint(monkeypatch, provider) as (client, service):
            result = resume("resume", pending)
            assert (
                await client.post(PATH, json=result, headers={"authorization": "bob"})
            ).status_code == 404
            assert events(await client.post(PATH, json=result))[-1]["type"] == "RUN_FINISHED"
            assert (await client.post(PATH, json=result)).status_code == 409
            assert (
                events(await client.post(PATH, json=body("follow-up")))[-1]["type"]
                == "RUN_FINISHED"
            )
            assert (
                await client.delete(PATH + "/thread", headers={"authorization": "bob"})
            ).status_code == 404
            assert (await client.delete(PATH + "/thread")).status_code == 204
            assert not await service.saver.aget_tuple({"configurable": {"thread_id": "thread"}})
            service.threads["thread"].touched = 0
            await service._save("thread", service.threads["thread"])

        async with endpoint(monkeypatch, provider) as (client, service):
            assert "thread" not in service.threads
            assert not await service.saver.aget_tuple({"configurable": {"thread_id": "thread"}})
            assert (await client.post(PATH, json=resume("late", pending))).status_code == 409

    with Connection.connect(dsn("admin"), dbname="postgres", autocommit=True) as connection:
        name = sql.Identifier(database)
        connection.execute(sql.SQL("CREATE DATABASE {} OWNER app_migrator").format(name))
        try:
            connection.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(name))
            connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO app_runtime").format(name))
            asyncio.run(
                scenario(), loop_factory=asyncio.SelectorEventLoop if os.name == "nt" else None
            )
        finally:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(name))
    assert "Remember Cabo." in json.dumps(payloads[-1]["input"])
    assert any(
        item["type"] == "function_call_output" and "opened" in item["output"]
        for item in payloads[2]["input"]
    )


def test_agui_disconnect_and_cancel_release_active_run(monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def provider(_request):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    async def scenario():
        async with endpoint(monkeypatch, provider) as (client, service):
            async with client.stream("POST", PATH, json=body("disconnect")) as response:
                assert response.status_code == 200
                await asyncio.wait_for(started.wait(), 4)
                assert (await client.post(PATH, json=body("concurrent"))).status_code == 409
            await asyncio.wait_for(cancelled.wait(), 4)
            await until(lambda: service.threads["thread"].closed)
            started.clear()
            cancelled.clear()
            async with client.stream("POST", PATH, json=body("cancel", thread="other")) as response:
                assert response.status_code == 200
                await asyncio.wait_for(started.wait(), 4)
                assert (await client.post(PATH + "/other/runs/cancel/cancel")).status_code == 204
                await asyncio.wait_for(cancelled.wait(), 4)
            await until(lambda: service.threads["other"].closed)
            started.clear()
            cancelled.clear()
            monkeypatch.setattr(agent_http, "RUN_TIMEOUT_SECONDS", 0.3)
            timed_out = events(await client.post(PATH, json=body("timeout", thread="deadline")))
            assert timed_out[-1]["code"] == "agent_timeout"
            assert started.is_set()
            await asyncio.wait_for(cancelled.wait(), 4)
            await until(lambda: service.threads["deadline"].closed)

    asyncio.run(scenario())


def test_agui_real_application_bearer_and_browser_csrf(db, accounts, monkeypatch):
    _, _, authority, manager = accounts
    monkeypatch.setenv("APP_DATA_BOARD_LOOMEX_API_KEY", "fixture")
    model = agent_runtime.loomex_model
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: model_stream(["Ready."])))
    monkeypatch.setattr(agent_http, "loomex_model", lambda _: model(upstream))
    origin = "https://games.test"
    app = create_app(
        db.runtime,
        authority,
        origin + "/powersync",
        sessions=manager,
        application_settings={"board_game_agent": {"browser_origin": origin}},
    )
    auth = PATH.split("/ai/")[0] + "/auth"
    with TestClient(app, base_url=origin, headers={"origin": origin}) as client:
        try:
            assert client.post(PATH, json=body("unauthenticated")).status_code == 401
            boot = client.post(auth + "/browser/bootstrap", json={})
            client.headers["X-CSRF-Token"] = boot.json()["csrf_token"]
            login = client.post(
                auth + "/browser/login", json={"account": "alice", "password": "fixture"}
            )
            assert login.status_code == 200, login.text
            token = {"Authorization": "Bearer " + login.json()["access_token"]}
            assert client.get(PATH + "/health", headers=token).json()["status"] == "ready"
            assert events(client.post(PATH, json=body("cookie")))[-1]["type"] == "RUN_FINISHED"
            csrf = client.headers.pop("X-CSRF-Token")
            assert client.post(PATH, json=body("no-csrf")).status_code == 403
            assert (
                events(client.post(PATH, json=body("bearer"), headers=token))[-1]["type"]
                == "RUN_FINISHED"
            )
            client.headers["X-CSRF-Token"] = csrf
            assert client.post(auth + "/browser/logout", json={}).status_code == 200
            assert client.post(PATH, json=body("revoked"), headers=token).status_code == 401
        finally:
            client.portal.call(upstream.aclose)
