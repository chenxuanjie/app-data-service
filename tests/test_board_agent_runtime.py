import asyncio
import json

import httpx
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy.exc import SQLAlchemyError

from app_data_service.modules.board_game_agent import agent_cli, agent_runtime


def model_stream(output=None, *, error=None, tool="get_favorite_rankings", call_id="call_1"):
    if error:
        events = [{"type": "error", "code": "test_error", "message": error}]
    elif isinstance(output, dict):
        item = {
            "type": "function_call",
            "name": tool,
            "id": "fc_1",
            "call_id": call_id,
            "arguments": "",
        }
        arguments = json.dumps(output)
        events = [
            {"type": "response.output_item.added", "output_index": 0, "item": item},
            *[
                {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc_1",
                    "output_index": 0,
                    "delta": part,
                }
                for part in (arguments[:4], arguments[4:])
            ],
        ]
    else:
        events = [
            {
                "type": "response.output_text.delta",
                "item_id": "msg_1",
                "output_index": 0,
                "content_index": 0,
                "delta": part,
            }
            for part in output
        ]
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        text="".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n",
    )


def test_responses_graph_streams_tools_and_checkpoints(monkeypatch, capsys):
    monkeypatch.setenv("APP_DATA_BOARD_LOOMEX_API_KEY", "fixture")
    monkeypatch.setenv("APP_DATA_BOARD_LOOMEX_BASE_URL", "https://provider.invalid/v1")
    requests = []
    reads = []

    def rankings(engine, limit, locale):
        reads.append((limit, locale))
        if len(reads) == 2:
            raise SQLAlchemyError("private database diagnostics")
        return {"catalog_revision": 1, "items": [{"title": "Cabo", "rank": 1, "favorite_count": 7}]}

    def provider(request):
        assert request.url.path == "/v1/responses"
        body = json.loads(request.content)
        assert body["store"] is False and body["stream"] is True
        assert "previous_response_id" not in body
        assert [t["name"] for t in body["tools"]] == ["get_favorite_rankings"]
        requests.append(body)
        return next(responses)

    responses = iter(
        [
            model_stream({"limit": 1, "locale": "en"}),
            model_stream(["Cabo ", "is first."]),
            model_stream(error="private provider diagnostics"),
            model_stream(["You asked about Cabo."]),
            model_stream({"limit": 3, "locale": "en"}),
            model_stream(["Rankings are unavailable."]),
        ]
    )
    monkeypatch.setattr(agent_runtime, "read_favorite_rankings", rankings)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            agent = agent_runtime.build_agent(
                None, agent_runtime.loomex_model(client), checkpointer=InMemorySaver()
            )
            start = {"configurable": {"thread_id": "fixture"}}
            checkpoint, ok = await agent_cli.stream_turn(agent, "Who is first?", start)
            assert ok and checkpoint["configurable"]["checkpoint_id"]
            failed, ok = await agent_cli.stream_turn(agent, "failed prompt", checkpoint)
            assert not ok and failed == checkpoint
            checkpoint, ok = await agent_cli.stream_turn(agent, "What did I ask?", failed)
            assert ok
            _, ok = await agent_cli.stream_turn(agent, "Check again.", checkpoint)
            assert ok

    asyncio.run(scenario())
    assert reads == [(1, "en"), (3, "en")]
    first_tool_result = next(
        item for item in requests[1]["input"] if item["type"] == "function_call_output"
    )
    assert first_tool_result["call_id"] == "call_1"
    assert json.loads(first_tool_result["output"])["items"][0]["favorite_count"] == 7
    followup = json.dumps(requests[3]["input"])
    assert "Who is first?" in followup and "Cabo is first." in followup
    assert "failed prompt" not in followup
    assert "rankings_unavailable" in json.dumps(requests[-1]["input"])
    output = capsys.readouterr().out
    assert "Cabo is first." in output and "You asked about Cabo." in output
    assert "model_request_failed" in output and "Rankings are unavailable." in output
    assert "private" not in output


def test_run_deadline_returns_previous_checkpoint(monkeypatch, capsys):
    class SlowAgent:
        async def astream(self, *args, **kwargs):
            await asyncio.sleep(60)
            yield None

    monkeypatch.setattr(agent_cli, "RUN_TIMEOUT_SECONDS", 0.01)
    start = {"configurable": {"thread_id": "fixture"}}
    assert asyncio.run(agent_cli.stream_turn(SlowAgent(), "Wait", start)) == (start, False)
    assert "run_timeout" in capsys.readouterr().out
