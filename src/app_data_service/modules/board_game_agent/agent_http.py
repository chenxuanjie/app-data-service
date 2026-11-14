"""Authenticated AG-UI transport for the board-game Graph."""

import asyncio
import json
import logging
import os
import re
from base64 import b64decode
from binascii import Error as InvalidBase64
from contextlib import AsyncExitStack, aclosing, asynccontextmanager
from dataclasses import dataclass, field
from time import monotonic, time
from typing import Annotated, Literal

import anyio
import httpx
import jwt
from ag_ui.core import (
    AssistantMessage,
    RunAgentInput,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    UserMessage,
)
from ag_ui.encoder import EventEncoder
from ag_ui_langgraph import LangGraphAgent
from fastapi import Depends, HTTPException, Request, Response
from langgraph.checkpoint.memory import InMemorySaver
from langsmith import tracing_context
from psycopg import Error as PostgresError
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import StreamingResponse

from app_data_service.dependencies import Actor, Application, Credential, RequestScope, identity
from app_data_service.sessions import REFRESH_AUDIENCE, REFRESH_LIFETIME

from .agent_checkpoint import PostgresThreads
from .agent_runtime import (
    CLIENT_TOOL_NAMES,
    OPEN_RANKINGS,
    RUN_STEP_LIMIT,
    RUN_TIMEOUT_SECONDS,
    build_agent,
    loomex_model,
    run_files,
    run_stop,
)

logger = logging.getLogger(__name__)
ID = re.compile(r"[A-Za-z0-9_-]{1,96}")
THREAD_TTL = 1800
INTERRUPT_TTL = 300
MAX_THREADS = 64
MAX_RUNS = 8
MAX_TURNS_PER_THREAD = 32
MAX_RESUMES_PER_TURN = 8
MODELS = frozenset(
    {
        "gpt-5.6",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6.1-sol",
        "gpt-6.1-terra",
        "gpt-6-luna",
    }
)


class RuleFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=200, pattern=r"^[^/\\]+\.pdf$")
    data: str = Field(min_length=8, max_length=376_000)


class RunOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str | None = None
    effort: Literal["none", "low", "medium", "high", "xhigh", "max"] | None = None
    tier: Literal["fast", "default"] | None = None
    knowledge_only: bool = False
    game: str | None = Field(default=None, max_length=200)
    indexed_context: str | None = Field(default=None, max_length=48_000)
    transient: bool = False
    rule_files: list[RuleFile] = Field(default_factory=list, max_length=4)


class AgentInput(RunAgentInput):
    options: RunOptions | None = None
    history: list[UserMessage | AssistantMessage] = Field(default_factory=list, max_length=12)


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
    pending_tool: str | None = None
    client_tool_names: list[str] = field(default_factory=list)
    expires: float = 0
    closed: bool = False
    evidence_error: str | None = None
    committed_checkpoint: str | None = None
    paused_checkpoint: str | None = None
    turn: str | None = None
    resumes: int = 0
    options: dict = field(default_factory=dict)
    result: dict | None = None
    receipt: dict | None = None
    finished_receipt: dict | None = None
    partial_text: str = ""
    evidence: list = field(default_factory=list)
    cancelled: bool = False

    def snapshot(self):
        return {
            "owner": self.owner,
            "touched": self.touched,
            "runs": sorted(self.runs),
            "messages": sorted(self.messages),
            "active": self.active,
            "pending": self.pending,
            "paused_run": self.paused_run,
            "pending_tool": self.pending_tool,
            "client_tool_names": self.client_tool_names,
            "expires": self.expires,
            "closed": self.closed,
            "evidence_error": self.evidence_error,
            "committed_checkpoint": self.committed_checkpoint,
            "paused_checkpoint": self.paused_checkpoint,
            "turn": self.turn,
            "resumes": self.resumes,
            "options": self.options,
            "result": self.result,
            "receipt": self.receipt,
            "finished_receipt": self.finished_receipt,
            "partial_text": self.partial_text,
            "evidence": self.evidence,
            "cancelled": self.cancelled,
        }

    @classmethod
    def restore(cls, state):
        return cls(**{**state, "runs": set(state["runs"]), "messages": set(state["messages"])})


def _validate(body):
    if not all(ID.fullmatch(value) for value in (body.thread_id, body.run_id)):
        raise HTTPException(422, "invalid_agent_id")
    if body.history and (
        body.resume
        or any(
            not ID.fullmatch(message.id)
            or not isinstance(message.content, str)
            or len(message.content.encode("utf-8")) > 8000
            for message in body.history
        )
        or sum(len(message.content.encode("utf-8")) for message in body.history) > 24000
        or len({message.id for message in body.history + body.messages})
        != len(body.history) + len(body.messages)
    ):
        raise HTTPException(422, "invalid_agent_history")
    if body.options and (
        body.resume
        or (
            body.options.indexed_context is not None
            and (not body.options.transient or bool(body.tools))
        )
        or (body.options.rule_files and not body.options.indexed_context)
        or (body.options.model is not None and body.options.model not in MODELS)
        or (body.options.model in {"gpt-6-astra", "gpt-6.1-sol"} and body.options.effort == "none")
    ):
        raise HTTPException(422, "unsupported_agent_options")
    if body.options and body.options.rule_files:
        try:
            if sum(len(file.data) for file in body.options.rule_files) > 376_000:
                raise ValueError
            if any(
                not b64decode(file.data, validate=True).startswith(b"%PDF-")
                for file in body.options.rule_files
            ):
                raise ValueError
        except (InvalidBase64, ValueError):
            raise HTTPException(422, "invalid_rule_files") from None
    if body.state or body.context or body.forwarded_props or body.parent_run_id:
        raise HTTPException(422, "unsupported_agent_input")
    names = [tool.name for tool in body.tools or ()]
    if len(names) != len(set(names)) or not CLIENT_TOOL_NAMES.issuperset(names):
        raise HTTPException(422, "unsupported_client_tool")
    if body.resume:
        if len(body.resume) != 1 or body.messages:
            raise HTTPException(422, "invalid_agent_resume")
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


def _validate_result(entry, name):
    value = entry.payload
    if entry.status == "cancelled" and value is None:
        return
    if entry.status == "resolved" and isinstance(value, dict):
        if name in (None, OPEN_RANKINGS):
            if set(value) == {"status"} and value["status"] in NAVIGATION_STATUSES:
                return
        elif (
            value.keys() <= {"status", "code", "data"}
            and value.get("status") in ("succeeded", "failed")
            and isinstance(code := value.get("code"), str)
            and ID.fullmatch(code)
            and ("data" not in value or isinstance(value["data"], dict))
        ):
            try:
                if (
                    len(json.dumps(value, allow_nan=False, ensure_ascii=False).encode())
                    <= 96 * 1024
                ):
                    return
            except (ValueError, RecursionError):
                pass
    raise HTTPException(422, "invalid_client_tool_result")


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
        if prepared.get("config"):
            # Pin the graph input to the committed branch. The adapter must
            # observe the resulting head when it resolves the terminal outcome.
            config = prepared["config"]
            configurable = {k: v for k, v in config["configurable"].items() if k != "checkpoint_id"}
            prepared["config"] = {**config, "configurable": configurable}
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
                                await self._abandon(key, thread)
                            elif not thread.closed and not (
                                thread.committed_checkpoint or thread.paused_checkpoint
                            ):
                                saved = await self.saver.aget_tuple(
                                    {"configurable": {"thread_id": key}}
                                )
                                if saved:
                                    checkpoint = saved.config["configurable"]["checkpoint_id"]
                                    if thread.pending:
                                        thread.paused_checkpoint = checkpoint
                                    else:
                                        thread.committed_checkpoint = checkpoint
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
                    task = asyncio.create_task(self._cleanup_idle())
                    resources.push_async_callback(self._stop_task, task)
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

    @staticmethod
    async def _stop_task(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _save(self, thread_id, thread):
        if self.storage is not None:
            await self.storage.save(thread_id, thread.snapshot())

    async def _abandon(self, thread_id, thread):
        thread.active = None
        thread.touched = time()
        if thread.options.get("transient"):
            await self._remove_thread(thread_id)
            return
        if thread.pending is None:
            thread.result = {"state": "failed", "messageId": thread.turn}
            if not thread.committed_checkpoint:
                await self.saver.adelete_thread(thread_id)
        await self._save(thread_id, thread)

    async def _cancel_turn(self, thread_id, thread):
        thread.pending = thread.paused_run = thread.pending_tool = thread.paused_checkpoint = None
        thread.receipt = None
        thread.partial_text = ""
        thread.evidence = []
        await self._abandon(thread_id, thread)

    async def _close_thread(self, thread_id, thread):
        thread.active = thread.pending = thread.paused_run = None
        thread.pending_tool = None
        thread.closed = True
        thread.committed_checkpoint = thread.paused_checkpoint = None
        thread.result = thread.receipt = thread.finished_receipt = None
        thread.partial_text = ""
        thread.evidence = []
        thread.touched = time()
        await self._save(thread_id, thread)
        await self.saver.adelete_thread(thread_id)

    async def _remove_thread(self, thread_id):
        await self.saver.adelete_thread(thread_id)
        if self.storage is not None:
            await self.storage.delete(thread_id)
        self.threads.pop(thread_id, None)

    async def _cleanup_idle(self):
        while True:
            await asyncio.sleep(60)
            async with self.lock:
                if self.storage and self.storage.connection.closed:
                    continue
                try:
                    await self._prune(time())
                except (PostgresError, PoolTimeout, OSError) as error:
                    logger.warning("board_agent cleanup_error=%s", type(error).__name__)

    async def _prune(self, now):
        expired = [
            key
            for key, thread in self.threads.items()
            if thread.active is None and now - thread.touched >= THREAD_TTL
        ]
        for key in expired[:8]:
            await self._remove_thread(key)

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
        if thread and not body.resume and body.run_id in thread.runs:
            raise HTTPException(409, "duplicate_agent_run")
        if body.resume:
            if not thread or thread.pending != body.resume[0].interrupt_id:
                raise HTTPException(409, "agent_interrupt_not_pending")
            if now >= thread.expires:
                await self._cancel_turn(body.thread_id, thread)
                raise HTTPException(410, "agent_interrupt_expired")
            _validate_result(body.resume[0], thread.pending_tool)
            receipt = body.resume[0].model_dump(mode="json", by_alias=True)
            if thread.receipt is not None and thread.receipt != receipt:
                raise HTTPException(409, "agent_receipt_conflict")
            if thread.resumes >= MAX_RESUMES_PER_TURN and thread.receipt is None:
                raise HTTPException(409, "agent_resume_limit")
        elif thread and thread.pending:
            if now < thread.expires:
                raise HTTPException(409, "agent_interrupt_pending")
            await self._cancel_turn(body.thread_id, thread)
        retry = bool(
            thread
            and body.messages
            and thread.result
            and thread.result["state"] == "failed"
            and thread.turn == body.messages[0].id
        )
        if (
            thread
            and not body.resume
            and not retry
            and len(thread.messages) >= MAX_TURNS_PER_THREAD
        ):
            raise HTTPException(409, "agent_thread_limit")
        if thread and body.messages and not retry and body.messages[0].id in thread.messages:
            raise HTTPException(409, "duplicate_agent_message")
        if sum(t.active is not None for t in self.threads.values()) >= MAX_RUNS:
            raise HTTPException(503, "agent_busy")
        if thread is None:
            if len(self.threads) >= MAX_THREADS:
                raise HTTPException(503, "agent_thread_capacity")
            thread = self.threads[body.thread_id] = Thread(owner, now)
        previous = thread.snapshot()
        thread.active = body.run_id
        thread.stop = asyncio.Event()
        thread.cancelled = False
        if not body.resume:
            thread.runs.add(body.run_id)
        if body.messages:
            thread.turn = body.messages[0].id
            thread.messages.add(thread.turn)
            thread.client_tool_names = [tool.name for tool in body.tools or ()]
            thread.options = (
                body.options.model_dump(exclude_none=True, exclude={"rule_files"})
                if body.options
                else {}
            )
            thread.resumes = 0
            thread.result = None
            thread.receipt = None
            thread.finished_receipt = None
            thread.partial_text = ""
            thread.evidence = []
            thread.evidence_error = None
        elif thread.receipt is None:
            thread.resumes += 1
            thread.receipt = receipt
            if (receipt.get("payload") or {}).get("code") == "ruleset_unavailable":
                thread.evidence_error = "ruleset_unavailable"
            if (receipt.get("payload") or {}).get("code") == "rule_evidence":
                data = receipt["payload"].get("data") or {}
                matches = data.get("matches")
                thread.evidence = (
                    [
                        citation
                        for match in matches
                        if isinstance(match, dict)
                        if isinstance(match.get("citations"), list)
                        for citation in match["citations"]
                        if isinstance(citation, dict)
                    ][:24]
                    if isinstance(matches, list)
                    else []
                )
        try:
            await self._save(body.thread_id, thread)
        except (PostgresError, PoolTimeout, OSError):
            self.threads[body.thread_id] = Thread.restore(previous)
            raise
        return thread

    def _response(self, body, thread):
        client_tools = thread.client_tool_names
        if body.resume and thread.pending_tool is None and not client_tools:
            client_tools = [OPEN_RANKINGS]  # Pending checkpoint from before multi-tool support.
        # The client advertises names; only server-owned definitions reach the model.
        body = body.model_copy(
            update={
                "tools": None,
                "messages": body.history + body.messages
                if (body.messages and not thread.committed_checkpoint)
                else body.messages,
            }
        )
        checkpoint = thread.paused_checkpoint if body.resume else thread.committed_checkpoint
        configurable = {"client_tool_names": client_tools, "run_options": thread.options}
        if checkpoint:
            configurable["checkpoint_id"] = checkpoint
        adapter = BoardGraphAdapter(
            name="board-game-agent",
            graph=self.graph,
            config={
                "recursion_limit": RUN_STEP_LIMIT,
                "configurable": configurable,
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
            text = []
            first_text_ms = None
            first_event_ms = None
            stop_token = run_stop.set(model_stop)
            files_token = run_files.set(
                tuple(file.model_dump() for file in body.options.rule_files) if body.options else ()
            )
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
                                if first_event_ms is None:
                                    first_event_ms = round((monotonic() - started) * 1000)
                                sent_start |= isinstance(event, RunStartedEvent)
                                if event.type == "TEXT_MESSAGE_CONTENT":
                                    if first_text_ms is None:
                                        first_text_ms = round((monotonic() - started) * 1000)
                                    if len(text) >= 16384:
                                        raise ValueError("agent_response_budget")
                                    text.append(event.delta)
                                yield encoder.encode(event)
                if isinstance(terminal, RunFinishedEvent):
                    outcome = terminal.outcome
                    if (
                        outcome is None
                        or outcome.type != "interrupt"
                        or len(outcome.interrupts) == 1
                    ):
                        paused_outcome = outcome is not None and outcome.type == "interrupt"
                        if paused_outcome and thread.resumes >= MAX_RESUMES_PER_TURN:
                            raise ValueError("agent_resume_limit")
                        state = await self.saver.aget_tuple(
                            {"configurable": {"thread_id": body.thread_id}}
                        )
                        checkpoint_id = state.config["configurable"]["checkpoint_id"]
                        async with self.lock:
                            if thread.stop.is_set():
                                return
                            previous = thread.snapshot()
                            thread.pending = None
                            thread.paused_run = None
                            thread.pending_tool = None
                            thread.paused_checkpoint = None
                            thread.finished_receipt = thread.receipt
                            thread.receipt = None
                            thread.partial_text += "".join(text)
                            if len(thread.partial_text) > 65536:
                                thread.partial_text = thread.partial_text[:65536]
                            if paused_outcome:
                                paused = outcome.interrupts[0]
                                thread.pending = paused.id
                                raw = (paused.metadata or {}).get("langgraph", {}).get("raw", {})
                                thread.pending_tool = raw.get("tool", OPEN_RANKINGS)
                                thread.paused_run = body.run_id
                                thread.expires = time() + INTERRUPT_TTL
                                thread.paused_checkpoint = checkpoint_id
                                thread.result = {
                                    "state": "waiting",
                                    "messageId": thread.turn,
                                    "interrupt": paused.model_dump(mode="json", by_alias=True),
                                }
                            else:
                                thread.committed_checkpoint = checkpoint_id
                                thread.result = {
                                    "state": "completed",
                                    "messageId": thread.turn,
                                    "text": thread.partial_text,
                                    "citations": thread.evidence,
                                    "evidenceError": thread.evidence_error,
                                }
                            thread.touched = time()
                            thread.active = None
                            try:
                                await self._save(body.thread_id, thread)
                            except (PostgresError, PoolTimeout, OSError):
                                restored = Thread.restore(previous)
                                thread.__dict__.update(restored.__dict__)
                                raise
                            committed = True
                        if thread.options.get("transient"):
                            async with self.lock:
                                await self._remove_thread(body.thread_id)
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
                run_files.reset(files_token)
                logger.info(
                    "board_agent run_id=%s thread_id=%s status=%s elapsed_ms=%d"
                    " first_event_ms=%s first_text_ms=%s",
                    body.run_id,
                    body.thread_id,
                    status,
                    (monotonic() - started) * 1000,
                    first_event_ms,
                    first_text_ms,
                )
            if not sent_start:
                yield encoder.encode(RunStartedEvent(thread_id=body.thread_id, run_id=body.run_id))
            yield encoder.encode(RunErrorEvent(code=status, message="Agent run failed."))

        async def release():
            if not committed:
                async with self.lock:
                    try:
                        if (
                            thread.cancelled
                            or (terminal and isinstance(terminal, RunFinishedEvent))
                            and thread.resumes >= MAX_RESUMES_PER_TURN
                        ):
                            await self._cancel_turn(body.thread_id, thread)
                        else:
                            await self._abandon(body.thread_id, thread)
                    except (PostgresError, PoolTimeout, OSError) as error:
                        logger.error("board_agent checkpoint_error=%s", type(error).__name__)

        return AgentStream(events(), thread.stop, model_stop, release)

    def _replay(self, body, result):
        encoder = EventEncoder()
        message_id = f"{body.run_id}-answer"

        async def events():
            yield encoder.encode(RunStartedEvent(thread_id=body.thread_id, run_id=body.run_id))
            if result["text"]:
                yield encoder.encode(TextMessageStartEvent(message_id=message_id, role="assistant"))
                yield encoder.encode(
                    TextMessageContentEvent(message_id=message_id, delta=result["text"])
                )
                yield encoder.encode(TextMessageEndEvent(message_id=message_id))
            yield encoder.encode(RunFinishedEvent(thread_id=body.thread_id, run_id=body.run_id))

        return StreamingResponse(
            events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
        )

    def install(self, router, path):
        @router.get(path + "/health")
        async def health(actor: Actor):
            if self.graph is None:
                raise HTTPException(503, "agent_unavailable")
            if self.storage and self.storage.connection.closed:
                try:
                    async with self.lock:
                        await self.storage.reconnect()
                except (ValueError, PostgresError, PoolTimeout, OSError):
                    raise HTTPException(503, "agent_unavailable") from None
            return {"status": "ready", "checkpoint": "postgres" if self.storage else "memory"}

        @router.post(path)
        async def run(body: AgentInput, actor: AgentActor):
            if self.graph is None:
                raise HTTPException(503, "agent_unavailable")
            _validate(body)
            async with self.lock:
                if self.graph is None:
                    raise HTTPException(503, "agent_unavailable")
                if self.storage and self.storage.connection.closed:
                    try:
                        await self.storage.reconnect()
                    except (ValueError, PostgresError, PoolTimeout, OSError):
                        raise HTTPException(503, "agent_unavailable") from None
                existing = self.threads.get(body.thread_id)
                if (
                    existing
                    and existing.owner == actor.user_id
                    and not existing.active
                    and body.resume
                    and existing.finished_receipt is not None
                    and existing.finished_receipt
                    == body.resume[0].model_dump(mode="json", by_alias=True)
                    and existing.result
                    and existing.result["state"] == "completed"
                ):
                    return self._replay(body, existing.result)
                try:
                    thread = await self._admit(body, actor.user_id)
                except (PostgresError, PoolTimeout, OSError) as error:
                    logger.error("board_agent checkpoint_error=%s", type(error).__name__)
                    raise HTTPException(503, "agent_unavailable") from None
            return self._response(body, thread)

        @router.get(path + "/{thread_id}")
        async def status(thread_id: str, actor: AgentActor, response: Response):
            response.headers["Cache-Control"] = "no-store"
            async with self.lock:
                thread = self.threads.get(thread_id)
                if thread is None or thread.owner != actor.user_id:
                    raise HTTPException(404, "agent_thread_not_found")
                if thread.closed:
                    raise HTTPException(410, "agent_thread_closed")
                if thread.active:
                    return {"state": "running", "messageId": thread.turn}
                if thread.pending and time() >= thread.expires:
                    try:
                        await self._cancel_turn(thread_id, thread)
                    except (PostgresError, PoolTimeout, OSError):
                        raise HTTPException(503, "agent_unavailable") from None
                    return {"state": "expired", "messageId": thread.turn}
                if thread.receipt is not None:
                    return {**(thread.result or {}), "receipt": thread.receipt}
                return thread.result or {"state": "idle"}

        @router.post(path + "/{thread_id}/runs/{run_id}/cancel", status_code=204)
        async def cancel(thread_id: str, run_id: str, actor: AgentActor):
            async with self.lock:
                thread = self.threads.get(thread_id)
                if thread is None or thread.owner != actor.user_id:
                    raise HTTPException(404, "agent_thread_not_found")
                if thread.active == run_id:
                    thread.cancelled = True
                    thread.stop.set()
                elif thread.active is None and thread.paused_run == run_id:
                    try:
                        await self._cancel_turn(thread_id, thread)
                    except (PostgresError, PoolTimeout, OSError) as error:
                        logger.error("board_agent checkpoint_error=%s", type(error).__name__)
                        raise HTTPException(503, "agent_unavailable") from None
                else:
                    raise HTTPException(409, "agent_run_not_active")
            return Response(status_code=204)

        @router.post(path + "/{thread_id}/renew", status_code=204)
        async def renew(thread_id: str, actor: AgentActor):
            async with self.lock:
                thread = self.threads.get(thread_id)
                if thread is None or thread.owner != actor.user_id:
                    raise HTTPException(404, "agent_thread_not_found")
                if (
                    thread.closed
                    or thread.active
                    or thread.pending
                    or len(thread.messages) < MAX_TURNS_PER_THREAD
                ):
                    raise HTTPException(409, "agent_thread_not_renewable")
                try:
                    await self._remove_thread(thread_id)
                except (PostgresError, PoolTimeout, OSError):
                    raise HTTPException(503, "agent_unavailable") from None
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
                    logger.error("board_agent checkpoint_error=%s", type(error).__name__)
                    raise HTTPException(503, "agent_unavailable") from None
            return Response(status_code=204)
