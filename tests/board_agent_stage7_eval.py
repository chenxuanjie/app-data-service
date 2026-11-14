"""Run the stage 7 AG-UI evaluation with real Loomex and simulated app receipts."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from fastapi import FastAPI
from http_helpers import serving
from test_board_agent_http import PATH, body, events, resume

from app_data_service.dependencies import RequestScope, application_router, request_application
from app_data_service.modules.board_game_agent import agent_http, agent_runtime

FIXTURE = Path(__file__).with_name("fixtures") / "board_agent_stage7.json"


async def evaluate(cases):
    requests = 0
    lookup_names = {"search_games", "get_game_details"}
    catalog = [
        {"game_id": "cabo", "title": "卡波", "players": [2, 3, 4]},
        {"game_id": "azul", "title": "花砖物语", "players": [2, 3, 4]},
        {"game_id": "exploding-kittens", "title": "炸弹猫", "players": [2, 3, 4, 5]},
    ]
    budget = sum(
        len(case["steps"])
        + 1
        + ("server_tool" in case)
        + 2 * any(step.get("target_aliases") for step in case["steps"])
        for case in cases
    )

    async def check_model(request):
        nonlocal requests
        payload = json.loads(request.content)
        if requests >= budget or payload["model"] != "gpt-6.1-sol":
            raise ValueError("unexpected model or request budget exceeded")
        if payload["reasoning"]["effort"] != "low" or payload["store"] is not False:
            raise ValueError("unexpected reasoning or storage configuration")
        requests += 1

    app = FastAPI()
    service = agent_http.AgentService(None)
    router = application_router("board_game_agent", lifespan=service.lifespan)
    service.install(router, PATH)
    app.include_router(router)
    app.dependency_overrides[request_application] = lambda: "board_game_agent"
    app.dependency_overrides[agent_http._agent_identity] = lambda: RequestScope(
        "stage7-local-fixture", "board_game_agent", "local"
    )
    agent_runtime.read_favorite_rankings = lambda *_: {
        "items": [{"title": "卡波", "slug": "cabo", "rank": 1, "favorite_count": 7}]
    }

    results = {}
    async with (
        httpx.AsyncClient(
            timeout=httpx.Timeout(45, connect=8),
            trust_env=False,
            event_hooks={"request": [check_model]},
        ) as upstream,
        app.router.lifespan_context(app),
        serving(app) as url,
        httpx.AsyncClient(base_url=url, timeout=100, trust_env=False) as client,
    ):
        service.graph = agent_runtime.build_agent(
            service.engine,
            agent_runtime.loomex_model(upstream),
            checkpointer=service.saver,
            client_tools=True,
        )
        for case in cases:
            thread = uuid4().hex
            first_call = requests
            seen = []
            payload = body(uuid4().hex, thread=thread)
            payload["messages"][0]["content"] = case["prompt"]
            payload["tools"] = [
                {"name": name, "description": name, "parameters": {}}
                for name in sorted(agent_runtime.CLIENT_TOOL_NAMES)
            ]
            try:
                step_index = 0
                optional_lookups = 0
                for _ in range(len(case["steps"]) + 3):
                    step = case["steps"][step_index] if step_index < len(case["steps"]) else None
                    response = await client.post(PATH, json=payload)
                    stream = events(response)
                    if not stream or stream[-1]["type"] != "RUN_FINISHED":
                        raise AssertionError(
                            (response.status_code, stream[-1].get("code") if stream else None)
                        )
                    terminal = stream[-1]
                    outcome = terminal.get("outcome") or {}
                    seen.extend(
                        event["toolCallName"]
                        for event in stream
                        if event["type"] == "TOOL_CALL_START"
                    )
                    if step is None:
                        assert outcome.get("type") != "interrupt", "unexpected tool"
                        reply = "".join(
                            event["delta"]
                            for event in stream
                            if event["type"] == "TEXT_MESSAGE_CONTENT"
                        )
                        assert reply.strip(), "missing final reply"
                        for word in case.get("reject_reply", []):
                            assert word not in reply, f"false success claim: {word}"
                        for word in case.get("reply", []):
                            assert word in reply, f"missing reply evidence: {word}"
                        if case.get("reply_any"):
                            assert any(word in reply for word in case["reply_any"]), (
                                f"missing expected outcome: {reply}"
                            )
                        break
                    assert outcome.get("type") == "interrupt", "tool not called"
                    (pending,) = outcome["interrupts"]
                    raw = pending["metadata"]["langgraph"]["raw"]
                    actual = raw.get("tool", "open_rankings")

                    assert any(
                        event["type"] == "TOOL_CALL_START"
                        and event["toolCallId"] == pending["toolCallId"]
                        for event in stream
                    ), "uncorrelated interrupt"
                    args = raw.get("arguments", {})
                    if actual not in step["tools"]:
                        assert (
                            actual in lookup_names
                            and step.get("target_aliases")
                            and optional_lookups < 2
                        ), f"unexpected tool {actual}"
                        optional_lookups += 1
                        game = next(
                            item for item in catalog if item["game_id"] == step["target_aliases"][0]
                        )
                        requested = args.get("query" if actual == "search_games" else "name")
                        requested = requested or args.get("game_id")
                        matching = requested in step["target_aliases"]
                        receipt = {
                            "status": "succeeded",
                            "code": "data_read",
                            "data": (
                                {"games": [game], "total": 1}
                                if matching
                                else {"games": [], "total": 0}
                            )
                            if actual == "search_games"
                            else {"game": game if matching else None},
                        }
                    else:
                        for field, value in step.get("args", {}).items():
                            assert args.get(field) == value, (
                                f"unexpected {field}: {args.get(field)!r}"
                            )
                        targets = step.get(
                            "target_aliases", [step["target"]] if "target" in step else []
                        )
                        if targets:
                            assert any(args.get(key) in targets for key in ("name", "game_id")), (
                                f"unexpected game target: {args.get('name')!r}, "
                                f"{args.get('game_id')!r}"
                            )
                        if actual == "navigate_to_page" and "args" not in step:
                            assert args.get("page") == "rankings", "unexpected navigation page"
                        receipt = (
                            {"status": "opened"} if actual == "open_rankings" else step["receipt"]
                        )
                        step_index += 1
                    payload = resume(uuid4().hex, pending["id"], thread=thread)
                    payload["resume"][0]["payload"] = receipt
                else:
                    raise AssertionError("missing terminal answer")
                if "server_tool" in case:
                    assert case["server_tool"] in seen, "server tool not called"
                if case.get("no_tools"):
                    assert not seen, "unexpected tool"
                result = "PASS"
            except (AssertionError, KeyError, TypeError, ValueError, httpx.HTTPError) as error:
                result = f"FAIL {error}"
            results[case["id"]] = result
            print(
                f"{case['id']}: {result} ({requests - first_call} calls, {','.join(seen)})",
                flush=True,
            )
            await client.delete(f"{PATH}/{thread}")
    print(f"MODEL_CALLS={requests} LIMIT={budget}", flush=True)
    return all(result == "PASS" for result in results.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Call the configured Loomex account")
    parser.add_argument("--case", action="append", help="Run only these case IDs")
    args = parser.parse_args()
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if args.case:
        selected = set(args.case)
        cases = [case for case in cases if case["id"] in selected]
        if {case["id"] for case in cases} != selected:
            parser.error("Unknown case ID")
    if not args.live:
        print("Cases:", ", ".join(case["id"] for case in cases))
        return 0
    upstream = urlsplit(agent_runtime.loomex_base_url())
    if upstream.hostname not in {"loomex.lol", "www.loomex.lol"}:
        parser.error("Use the configured Loomex HTTPS origin")
    if not os.getenv("APP_DATA_BOARD_LOOMEX_API_KEY"):
        parser.error("Set APP_DATA_BOARD_LOOMEX_API_KEY privately")
    os.environ.pop("APP_DATA_BOARD_AGENT_CHECKPOINT_DSN", None)
    return 0 if asyncio.run(evaluate(cases)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
