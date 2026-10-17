from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from nomic.config import GameConfig
from nomic.llm.openrouter import OpenRouterClient


def mock_config(**overrides: Any) -> GameConfig:
    base: dict[str, Any] = {
        "ruleset": "lite",
        "seats": [{"name": n, "model": "mock/fast"} for n in ("Alpha", "Beta", "Gamma")],
        "judge": {"mode": "llm", "model": "mock/judge"},
        "budget_usd": 0,
        "seed": 7,
    }
    base.update(overrides)
    return GameConfig.model_validate(base)


CANNED: dict[str, dict[str, Any]] = {
    "ReflectionAction": {
        "overview": "Fine.",
        "key_decisions": "Voted yes.",
        "would_change": "Nothing.",
        "about_others": "All fine.",
    },
    "ProposalAction": {
        "thinking": "SECRET-propose plan.",
        "op": "enact",
        "target_rule": None,
        "text": "Be nice.",
        "summary": "Nice.",
    },
    "DiscussAction": {"thinking": "SECRET-discuss plan.", "message": "Sounds good."},
    "VoteAction": {
        "thinking": "SECRET-vote plan.",
        "vote": "yes",
        "reason": "Fine.",
        "dispute": None,
    },
    "RulingAction": {
        "thinking": "SECRET-ruling plan.",
        "legal": True,
        "passed": True,
        "rule_changes": [],
        "score_deltas": [{"player": "Alpha", "delta": 3, "reason": "die"}],
        "winner": None,
        "rationale": "Adopted under 203.",
    },
}


def fake_openrouter(
    cost: float = 0.01, override: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None
) -> tuple[OpenRouterClient, list[dict[str, Any]]]:
    """An OpenRouterClient whose HTTP layer returns canned structured replies."""
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if override is not None:
            forced = override(body)
            if forced is not None:
                return httpx.Response(200, json=forced)
        name = body.get("response_format", {}).get("json_schema", {}).get("name", "VoteAction")
        content = dict(CANNED[name])
        if name == "RulingAction":
            # credit the active player named in the final message
            last = body["messages"][-1]["content"]
            for p in ("Alpha", "Beta", "Gamma"):
                if f"die for {p}" in last:
                    content["score_deltas"] = [{"player": p, "delta": 3, "reason": "die"}]
        return httpx.Response(
            200,
            json={
                "id": "gen-1",
                "choices": [{"message": {"content": json.dumps(content)}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "cost": cost,
                    "prompt_tokens_details": {"cached_tokens": 400},
                },
            },
        )

    client = OpenRouterClient(api_key="test", transport=httpx.MockTransport(handler), max_retries=2)
    return client, seen


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry backoff should not slow tests down."""
    import nomic.llm.openrouter as orr

    async def instant(_: int) -> None:
        return None

    monkeypatch.setattr(orr, "_backoff", instant)
