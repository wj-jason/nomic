from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from nomic.engine.actions import RulingAction, VoteAction, strict_schema
from nomic.llm.openrouter import LLMError, OpenRouterClient, _prepare_messages, extract_json


def _reply(content: str, cost: float = 0.001) -> dict[str, Any]:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": cost},
    }


def test_strict_schema_is_closed_and_inlined() -> None:
    s = strict_schema(RulingAction)
    assert "$defs" not in json.dumps(s) and "default" not in json.dumps(s)
    assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])
    item = s["properties"]["rule_changes"]["items"]
    assert item["additionalProperties"] is False and "new_number" in item["required"]


def test_extract_json_tolerates_fences_and_chatter() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": {"b": 2}} hope that helps') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_cache_control_only_for_supported_models() -> None:
    msgs = [{"role": "user", "content": "rules", "cache": True}]
    anth = _prepare_messages("anthropic/claude-sonnet-5.5", msgs)
    assert anth[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    other = _prepare_messages("deepseek/deepseek-v4-pro", msgs)
    assert other[0] == {"role": "user", "content": "rules"}


def test_structured_repairs_invalid_reply() -> None:
    replies = iter(
        [_reply("I vote yes!"), _reply('{"vote": "yes", "reason": "ok", "dispute": null}')]
    )
    bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json=next(replies))

    client = OpenRouterClient(api_key="k", transport=httpx.MockTransport(handler))
    action, calls = asyncio.run(
        client.structured(
            "m/x", [{"role": "user", "content": "v"}], VoteAction, purpose="vote", seat="A"
        )
    )
    assert action.vote == "yes" and len(calls) == 2 and calls[0].error
    assert "not valid" in bodies[1]["messages"][-1]["content"]
    assert sum(c.cost for c in calls) == pytest.approx(0.002)


def test_retries_then_drops_response_format() -> None:
    statuses = iter([429, 400, 200])
    bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        code = next(statuses)
        if code != 200:
            return httpx.Response(code, json={"error": {"message": "nope"}})
        return httpx.Response(200, json=_reply('{"vote": "no", "reason": "r", "dispute": null}'))

    client = OpenRouterClient(api_key="k", transport=httpx.MockTransport(handler))
    action, _ = asyncio.run(client.structured("m/x", [], VoteAction, purpose="vote", seat="A"))
    assert action.vote == "no"
    assert "response_format" in bodies[1] and "response_format" not in bodies[2]
    assert "m/x" in client._no_schema


def test_falls_back_to_minimal_reasoning_when_mandatory() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        bodies.append(body)
        if body["reasoning"]["effort"] == "none":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "Reasoning is mandatory for this endpoint and "
                        "cannot be disabled.",
                        "code": 400,
                    }
                },
            )
        return httpx.Response(200, json=_reply('{"vote": "no", "reason": "r", "dispute": null}'))

    client = OpenRouterClient(api_key="k", transport=httpx.MockTransport(handler))

    async def two_calls() -> None:
        for _ in range(2):
            await client.structured("g/x", [], VoteAction, purpose="vote", seat="A", effort="none")

    asyncio.run(two_calls())
    # first call: rejected, retried with minimal (schema kept); second call goes straight to minimal
    assert [b["reasoning"]["effort"] for b in bodies] == ["none", "minimal", "minimal"]
    assert all("response_format" in b for b in bodies)
    assert "g/x" in client._needs_reasoning and "g/x" not in client._no_schema


def test_output_cap_includes_reasoning_allowance() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json=_reply('{"vote": "no", "reason": "r", "dispute": null}'))

    client = OpenRouterClient(api_key="k", transport=httpx.MockTransport(handler))
    for effort in ("none", "low"):
        asyncio.run(
            client.structured("m/x", [], VoteAction, purpose="vote", seat="A", effort=effort)
        )
    assert [b["max_tokens"] for b in bodies] == [800, 800 + 2048]


def test_judge_has_its_own_reasoning_and_cap() -> None:
    from nomic.manager import GameManager
    from nomic.store import Store
    from tests.conftest import fake_openrouter, mock_config

    async def play() -> list[dict[str, Any]]:
        client, seen = fake_openrouter()
        manager = GameManager(Store(), client)
        cfg = mock_config(
            seats=[{"name": n, "model": "test/model"} for n in ("Alpha", "Beta")],
            judge={"mode": "llm", "model": "test/judge"},
            max_turns=1,
            reflection=False,
        )
        r = manager.create(cfg)
        await asyncio.wait_for(r.task, 10)  # type: ignore[arg-type]
        await manager.shutdown()
        return seen

    seen = asyncio.run(play())
    kind = {b["response_format"]["json_schema"]["name"]: b for b in seen}
    judge, player = kind["RulingAction"], kind["VoteAction"]
    assert judge["reasoning"]["effort"] == "low" and judge["max_tokens"] == 1500 + 2048
    assert player["reasoning"]["effort"] == "none" and player["max_tokens"] == 800


def test_truncated_reply_is_retried_without_echoing_it() -> None:
    long_reply = '{"vote": "yes", "reason": "' + "hmm " * 500
    replies = iter(
        [
            {
                "choices": [{"message": {"content": long_reply}, "finish_reason": "length"}],
                "usage": {"completion_tokens": 800, "cost": 0.003},
            },
            _reply('{"vote": "yes", "reason": "short", "dispute": null}'),
        ]
    )
    bodies: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json=next(replies))

    client = OpenRouterClient(api_key="k", transport=httpx.MockTransport(handler))
    action, calls = asyncio.run(
        client.structured(
            "m/x", [{"role": "user", "content": "v"}], VoteAction, purpose="vote", seat="A"
        )
    )
    assert action.reason == "short" and calls[0].error and calls[0].error.startswith("truncated")
    retry = bodies[1]["messages"]
    assert "cut off" in retry[-1]["content"]
    assert all(long_reply not in m["content"] for m in retry)


def test_gives_up_and_reports_paid_calls() -> None:
    client = OpenRouterClient(
        api_key="k",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_reply("?"))),
    )
    with pytest.raises(LLMError) as err:
        asyncio.run(client.structured("m/x", [], VoteAction, purpose="vote", seat="A", repairs=1))
    assert len(err.value.records) == 2
