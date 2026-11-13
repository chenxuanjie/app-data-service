"""Authenticated AG-UI transport for the board-game Graph."""

import asyncio
import logging
import os
import re
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import dataclass, field
from time import monotonic, time
from typing import Annotated

import anyio
import httpx
import jwt
from ag_ui.core import RunAgentInput, RunErrorEvent, RunFinishedEvent, RunStartedEvent, UserMessage
from ag_ui.encoder import EventEncoder
from ag_ui_langgraph import LangGraphAgent
from fastapi import Depends, HTTPException, Request, Response
from langgraph.checkpoint.memory import InMemorySaver
from langsmith import tracing_context
from psycopg import Error as PostgresError
from psycopg_pool import PoolTimeout
from starlette.responses import StreamingResponse

from app_data_service.dependencies import Actor, Application, Credential, RequestScope, identity
from app_data_service.sessions import REFRESH_AUDIENCE, REFRESH_LIFETIME

from .agent_checkpoint import PostgresThreads
from .agent_runtime import (
    OPEN_RANKINGS,
    RUN_STEP_LIMIT,
    RUN_TIMEOUT_SECONDS,
    build_agent,
    loomex_model,
    run_stop,
)

logger = logging.getLogger(__name__)
ID = re.compile(r"[A-Za-z0-9_-]{1,96}")
THREAD_TTL = 1800
INTERRUPT_TTL = 300
MAX_THREADS = 64
MAX_RUNS = 8
MAX_REQUESTS_PER_THREAD = 32
NAVIGATION_STATUSES = ("opened", "already_open", "unavailable")


def _agent_identity(request: Request, credentials: Credential, app_id: Application):
    if credentials is not None:
        return identity(request, credentials, app_id)
    runtime = request.app.state.runtime
    browser = runtime.browsers.get(app_id)
    if browser is None or browser.cookie not in request.cookies:
        return identity(request, None, app_id)
    identifier = browser.check(request)
    try:
        refresh = browser.refresh_token(identifier)
        claims = runtime.authority.claims(
            refresh, audience=REFRESH_AUDIENCE, maximum_age=REFRESH_LIFETIME, app_id=app_id
        )
        runtime.sessions.session(claims["sub"], claims["session_id"], app_id, refresh=refresh)
    except jwt.InvalidTokenError as error:
        raise HTTPException(401, "session_rejected") from error
    return RequestScope(claims["sub"], app_id, claims["session_id"])


AgentActor = Annotated[RequestScope, Depends(_agent_identity)]


@dataclass
class Thread:
    owner: str
    touched: float
    runs: set[str] = field(default_factory=set)
    messages: set[str] = field(default_factory=set)
    active: str | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    pending: str | None = None
    paused_run: str | None = None
    expires: float = 0
    closed: bool = False

    def snapshot(self):
        return {
            "owner": self.owner,
            "touched": self.touched,
            "runs": sorted(self.runs),
            "messages": sorted(self.messages),
            "active": self.active,
            "pending": self.pending,
            "paused_run": self.paused_run,
            "expires": self.expires,
            "closed": self.closed,
        }

    @classmethod
    def restore(cls, state):
        return cls(**{**state, "runs": set(state["runs"]), "messages": set(state["messages"])})


def _validate(body):
    if not all(ID.fullmatch(value) for value in (body.thread_id, body.run_id)):
        raise HTTPException(422, "invalid_agent_id")
    if body.state or body.context or body.forwarded_props or body.parent_run_id:
        raise HTTPException(422, "unsupported_agent_input")
    if any(tool.name != OPEN_RANKINGS for tool in body.tools or ()) or len(body.tools or ()) > 1:
        raise HTTPException(422, "unsupported_client_tool")
    if body.resume:
        if len(body.resume) != 1 or body.messages:
            raise HTTPException(422, "invalid_agent_resume")
        entry = body.resume[0]
        value = entry.payload
        if entry.status == "cancelled" and value is None:
            return
        if (
            entry.status != "resolved"
            or not isinstance(value, dict)
            or set(value) != {"status"}
            or value["status"] not in NAVIGATION_STATUSES
        ):
            raise HTTPException(422, "invalid_client_tool_result")
        return
    if len(body.messages) != 1 or not isinstance(body.messages[0], UserMessage):
        raise HTTPException(422, "one_user_message_required")
    message = body.messages[0]
    if (
        not ID.fullmatch(message.id)
        or not isinstance(message.content, str)
        or not 1 <= len(message.content.strip()) <= 4000
    ):
        raise HTTPException(422, "invalid_user_message")


class AgentStream(StreamingResponse):
    """Stop the producer on either ASGI disconnect or an authenticated stop."""

    def __init__(self, content, stop, model_stop, release):
        super().__init__(
            content,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )
        self.stop = stop
        self.model_stop = model_stop
        self.release = release

    async def __call__(self, scope, receive, send):
        try:
            async with anyio.create_task_group() as tasks:
                finished = False

                async def watch(wait):
                    await wait()
                    if not finished:
                        self.stop.set()
                        self.model_stop.set()
                        tasks.cancel_scope.cancel()

                tasks.start_soon(watch, self.stop.wait)
                tasks.start_soon(watch, lambda: self.listen_for_disconnect(receive))
                try:
                    await self.stream_response(send)
                except OSError:
                    pass  # The peer may disappear between a send and http.disconnect.
                finally:
                    finished = True
                    tasks.cancel_scope.cancel()
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await self.body_iterator.aclose()
                finally:
                    await self.release()


class BoardGraphAdapter(LangGraphAgent):
    async def prepare_stream(self, input, agent_state, config):
        prepared = await super().prepare_stream(input, agent_state, config)
        self.graph_stream = prepared.get("stream")
        return prepared

    async def run(self, input):
        self.graph_stream = None
        try:
            async with aclosing(super().run(input)) as source:
                async for event in source:
                    yield event
        finally:
            # The adapter consumes the graph iterator with async for. Closing
            # the outer iterator alone can leave the model's producer running.
            if self.graph_stream is not None:
                with anyio.CancelScope(shield=True):
                    await self.graph_stream.aclose()


class AgentService:
    def __init__(self, engine):
        self.engine = engine
        self.saver = InMemorySaver()
        self.graph = None
        self.threads = {}
        self.storage = None
        self.lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(self, _app):
        async with AsyncExitStack() as resources:
            if os.environ.get("APP_DATA_BOARD_LOOMEX_API_KEY", "").strip():
                dsn = os.environ.get("APP_DATA_BOARD_AGENT_CHECKPOINT_DSN")
                try:
                    if dsn:
                        self.storage = await resources.enter_async_context(
                            PostgresThreads.open(dsn)
                        )
                        self.saver = self.storage.saver
                        self.threads = {
                            key: Thread.restore(value)
                            for key, value in (await self.storage.load()).items()
                        }
                        # A live SSE connection is process-owned. Any run
                        # without a committed terminal outcome is abandoned.
                        for key, thread in tuple(self.threads.items()):
                            if thread.active:
                                await self._close_thread(key, thread)
                        await self._prune(time())
                    client = await resources.enter_async_context(
                        httpx.AsyncClient(
                            trust_env=False,
                            follow_redirects=False,
                            timeout=httpx.Timeout(connect=8, read=45, write=15, pool=5),
                        )
                    )
                    self.graph = build_agent(
                        self.engine,
                        loomex_model(client),
                        checkpointer=self.saver,
                        client_tools=True,
                    )
                except (ValueError, PostgresError, PoolTimeout, OSError) as error:
                    logger.error("board_agent startup_error=%s", type(error).__name__)
                    await resources.aclose()
            try:
                yield
            finally:
                self.graph = None
                self.storage = None
                self.threads.clear()
                self.saver = InMemorySaver()

    async def _save(self, thread_id, thread):
        if self.storage is not None:
            await self.storage.save(thread_id, thread.snapshot())

    async def _close_thread(self, thread_id, thread):
        thread.active = thread.pending = thread.paused_run = None
        thread.closed = True
        thread.touched = time()
        await self._save(thread_id, thread)
        await self.saver.adelete_thread(thread_id)

    async def _prune(self, now):
        for key, thread in tuple(self.threads.items()):
            if thread.active is None and now - thread.touched >= THREAD_TTL:
                await self.saver.adelete_thread(key)
                if self.storage is not None:
                    await self.storage.delete(key)
                self.threads.pop(key)

    async def _admit(self, body, owner):
        now = time()
        await self._prune(now)
        thread = self.threads.get(body.thread_id)
        if thread and thread.owner != owner:
            raise HTTPException(404, "agent_thread_not_found")
        if thread and thread.closed:
            raise HTTPException(410, "agent_thread_closed")
        if thread and thread.active:
            raise HTTPException(409, "agent_thread_busy")
        if thread and body.run_id in thread.runs:
            raise HTTPException(409, "duplicate_agent_run")
        if body.resume:
            if not thread or thread.pending != body.resume[0].interrupt_id:
                raise HTTPException(409, "agent_interrupt_not_pending")
            if now >= thread.expires:
                await self._close_thread(body.thread_id, thread)
                raise HTTPException(410, "agent_interrupt_expired")
        elif thread and thread.pending:
            if now < thread.expires:
                raise HTTPException(409, "agent_interrupt_pending")
            await self._close_thread(body.thread_id, thread)
            raise HTTPException(410, "agent_interrupt_expired")
        if thread and len(thread.runs) >= MAX_REQUESTS_PER_THREAD:
            raise HTTPException(409, "agent_thread_limit")
        if thread and body.messages and body.messages[0].id in thread.messages:
            raise HTTPException(409, "duplicate_agent_message")
        if sum(t.active is not None for t in self.threads.values()) >= MAX_RUNS:
            raise HTTPException(503, "agent_busy")
        if thread is None:
            if len(self.threads) >= MAX_THREADS:
                raise HTTPException(503, "agent_thread_capacity")
            thread = self.threads[body.thread_id] = Thread(owner, now)
        thread.active = body.run_id
        thread.stop = asyncio.Event()
        thread.runs.add(body.run_id)
        if body.messages:
            thread.messages.add(body.messages[0].id)
        await self._save(body.thread_id, thread)
        return thread

    def _response(self, body, thread):
        client_tools = bool(body.tools or body.resume)
        # Tool descriptions and client state never reach the model. The
        # advertised tool name opts in to the single server-owned schema.
        body = body.model_copy(update={"tools": None})
        adapter = BoardGraphAdapter(
            name="board-game-agent",
            graph=self.graph,
            config={
                "recursion_limit": RUN_STEP_LIMIT,
                "configurable": {OPEN_RANKINGS: client_tools},
            },
            emit_interrupt_outcome=True,
            enable_legacy_on_interrupt_event=False,
            emit_raw_events=False,
        )
        encoder = EventEncoder()
        terminal = None
        committed = False
        model_stop = asyncio.Event()

        async def events():
            nonlocal terminal, committed
            started = monotonic()
            status = "cancelled"
            sent_start = False
            stop_token = run_stop.set(model_stop)
            try:
                # Keep the deadline and tracing scopes inside the streaming
                # task. Starlette writes each yielded event before the next one.
                with tracing_context(enabled=False):
                    async with asyncio.timeout(RUN_TIMEOUT_SECONDS):
                        async with aclosing(adapter.run(body)) as source:
                            async for event in source:
                                if isinstance(event, (RunFinishedEvent, RunErrorEvent)):
                                    terminal = event
                                    break
                                sent_start |= isinstance(event, RunStartedEvent)
                                yield encoder.encode(event)
                if isinstance(terminal, RunFinishedEvent):
                    outcome = terminal.outcome
                    if (
                        outcome is None
                        or outcome.type != "interrupt"
                        or len(outcome.interrupts) == 1
                    ):
                        async with self.lock:
                            if thread.stop.is_set():
                                return
                            thread.pending = None
                            thread.paused_run = None
                            if outcome is not None and outcome.type == "interrupt":
                                thread.pending = outcome.interrupts[0].id
                                thread.paused_run = body.run_id
                                thread.expires = time() + INTERRUPT_TTL
                            thread.touched = time()
                            thread.active = None
                            await self._save(body.thread_id, thread)
                            committed = True
                        status = "ok"
                        yield encoder.encode(terminal)
                        return
                status = "agent_run_failed"
            except TimeoutError:
                model_stop.set()
                status = "agent_timeout"
            except Exception as error:
                status = "agent_run_failed"
                logger.warning("board_agent stream_error=%s", type(error).__name__)
            finally:
                run_stop.reset(stop_token)
                logger.info(
                    "board_agent run_id=%s thread_id=%s status=%s elapsed_ms=%d",
                    body.run_id,
                    body.thread_id,
                    status,
                    (monotonic() - started) * 1000,
                )
            if not sent_start:
                yield encoder.encode(RunStartedEvent(thread_id=body.thread_id, run_id=body.run_id))
            yield encoder.encode(RunErrorEvent(code=status, message="Agent run failed."))

        async def release():
            if not committed:
                async with self.lock:
                    try:
                        await self._close_thread(body.thread_id, thread)
                    except (PostgresError, PoolTimeout, OSError) as error:
                        self.graph = None
                        logger.error("board_agent checkpoint_error=%s", type(error).__name__)

        return AgentStream(events(), thread.stop, model_stop, release)

    def install(self, router, path):
        @router.get(path + "/health")
        async def health(actor: Actor):
            if self.graph is None:
                raise HTTPException(503, "agent_unavailable")
            return {"status": "ready", "checkpoint": "postgres" if self.storage else "memory"}

        @router.post(path)
        async def run(body: RunAgentInput, actor: AgentActor):
            if self.graph is None:
                raise HTTPException(503, "agent_unavailable")
            _validate(body)
            async with self.lock:
                if self.graph is None:
                    raise HTTPException(503, "agent_unavailable")
                try:
                    thread = await self._admit(body, actor.user_id)
                except (PostgresError, PoolTimeout, OSError) as error:
                    self.graph = None
                    logger.error("board_agent checkpoint_error=%s", type(error).__name__)
                    raise HTTPException(503, "agent_unavailable") from None
            return self._response(body, thread)

        @router.post(path + "/{thread_id}/runs/{run_id}/cancel", status_code=204)
        async def cancel(thread_id: str, run_id: str, actor: AgentActor):
            async with self.lock:
                thread = self.threads.get(thread_id)
                if thread is None or thread.owner != actor.user_id:
                    raise HTTPException(404, "agent_thread_not_found")
                if thread.active == run_id:
                    thread.stop.set()
                elif thread.active is None and thread.paused_run == run_id:
                    try:
                        await self._close_thread(thread_id, thread)
                    except (PostgresError, PoolTimeout, OSError) as error:
                        self.graph = None
                        logger.error("board_agent checkpoint_error=%s", type(error).__name__)
                        raise HTTPException(503, "agent_unavailable") from None
                else:
                    raise HTTPException(409, "agent_run_not_active")
            return Response(status_code=204)

        @router.delete(path + "/{thread_id}", status_code=204)
        async def delete(thread_id: str, actor: AgentActor):
            async with self.lock:
                thread = self.threads.get(thread_id)
                if thread is None or thread.owner != actor.user_id:
                    raise HTTPException(404, "agent_thread_not_found")
                if thread.active:
                    raise HTTPException(409, "agent_thread_busy")
                try:
                    await self._close_thread(thread_id, thread)
                except (PostgresError, PoolTimeout, OSError) as error:
                    self.graph = None
                    logger.error("board_agent checkpoint_error=%s", type(error).__name__)
                    raise HTTPException(503, "agent_unavailable") from None
            return Response(status_code=204)
