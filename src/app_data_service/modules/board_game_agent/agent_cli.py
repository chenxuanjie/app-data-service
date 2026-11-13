"""Local text entry point for the board-game LangGraph runtime."""

import argparse
import asyncio
import logging
from contextlib import aclosing
from time import monotonic
from uuid import uuid4

import httpx
from langchain_core.messages import AIMessageChunk, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.errors import GraphRecursionError
from langsmith import tracing_context
from openai import OpenAIError
from sqlalchemy.engine import make_url

from app_data_service.cli import local_config
from app_data_service.config import make_engine

from .agent_runtime import RUN_STEP_LIMIT, RUN_TIMEOUT_SECONDS, build_agent, loomex_model

logger = logging.getLogger(__name__)


async def stream_turn(agent, prompt, checkpoint):
    run_id = uuid4()
    started = monotonic()
    config = {**checkpoint, "run_id": run_id, "recursion_limit": RUN_STEP_LIMIT}
    code = "run_failed"
    try:
        with tracing_context(enabled=False):
            state = await _run_with_deadline(agent, prompt, config)
        code = "ok"
        print(flush=True)
        return state.config, True
    except TimeoutError:
        code = "run_timeout"
    except GraphRecursionError:
        code = "step_limit"
    except (OpenAIError, ValueError):
        code = "model_request_failed"
    except asyncio.CancelledError:
        code = "run_cancelled"
        raise
    finally:
        logger.info(
            "board_agent run_id=%s thread_id=%s status=%s elapsed_ms=%d",
            run_id,
            checkpoint["configurable"]["thread_id"],
            code,
            (monotonic() - started) * 1000,
        )
    print(f"\nAgent run failed ({code}).", flush=True)
    return checkpoint, False


async def _run_with_deadline(agent, prompt, config):
    async with asyncio.timeout(RUN_TIMEOUT_SECONDS):
        stream = agent.astream(
            {"messages": [HumanMessage(content=prompt, id=config["run_id"].hex)]},
            config,
            stream_mode="messages",
        )
        async with aclosing(stream) as events:
            async for message, _ in events:
                if isinstance(message, AIMessageChunk) and message.text:
                    print(message.text, end="", flush=True)
        thread = {"configurable": {"thread_id": config["configurable"]["thread_id"]}}
        state = await agent.aget_state(thread)
        reply = state.values["messages"][-1]
        if not reply.text or reply.response_metadata.get("incomplete_details"):
            raise ValueError("model_response_incomplete")
        return state


async def run_console(engine, prompts, *, interactive):
    checkpoint = {"configurable": {"thread_id": uuid4().hex}}
    async with httpx.AsyncClient(
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(connect=8, read=45, write=15, pool=5),
    ) as client:
        saver = InMemorySaver()
        agent = build_agent(engine, loomex_model(client), checkpointer=saver)
        for prompt in prompts:
            if not 1 <= len(prompt.strip()) <= 4000:
                print("Enter a prompt of 1 to 4000 characters.", flush=True)
                if not interactive:
                    return 2
                continue
            checkpoint, succeeded = await stream_turn(agent, prompt, checkpoint)
            if not succeeded:
                if not interactive:
                    return 1
                if not checkpoint["configurable"].get("checkpoint_id"):
                    await saver.adelete_thread(checkpoint["configurable"]["thread_id"])
                    checkpoint = {"configurable": {"thread_id": uuid4().hex}}
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prompt", nargs="*", help="Successive turns in a single local thread")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        url = local_config()["urls"]["runtime"]
        if make_url(url).host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("local_postgresql_required")
        engine = make_engine(url)
    except (KeyError, OSError, ValueError) as error:
        parser.exit(2, f"Local PostgreSQL configuration unavailable ({type(error).__name__}).\n")

    try:
        prompts = args.prompt or iter(lambda: input("> "), "")
        return asyncio.run(run_console(engine, prompts, interactive=not args.prompt))
    except EOFError:
        return 0
    except KeyboardInterrupt:
        return 130
    except ValueError as error:
        parser.exit(2, f"Agent configuration unavailable ({type(error).__name__}).\n")
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
