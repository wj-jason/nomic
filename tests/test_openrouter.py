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


def test_gives_up_and_reports_paid_calls() -> None:
    client = OpenRouterClient(
        api_key="k",
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_reply("?"))),
    )
    with pytest.raises(LLMError) as err:
        asyncio.run(client.structured("m/x", [], VoteAction, purpose="vote", seat="A", repairs=1))
    assert len(err.value.records) == 2
