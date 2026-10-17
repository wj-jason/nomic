"""Live OpenRouter prices and a rough per-game cost estimator."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any

import httpx

from nomic.config import GameConfig
from nomic.llm.openrouter import BASE_URL


@dataclass(frozen=True)
class ModelPrice:
    id: str
    name: str
    prompt: float  # USD per token
    completion: float
    cache_read: float | None
    context_length: int


class Pricing:
    def __init__(self, models: dict[str, ModelPrice]) -> None:
        self.models = models

    def get(self, model: str) -> ModelPrice | None:
        return self.models.get(model)

    def call_cost(self, model: str, prompt: int, completion: int, cached: int = 0) -> float:
        p = self.get(model)
        if p is None:
            return 0.0
        read = p.cache_read if p.cache_read is not None else p.prompt
        return (prompt - cached) * p.prompt + cached * read + completion * p.completion

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Pricing:
        models = {}
        for m in payload.get("data", []):
            pr = m.get("pricing") or {}
            try:
                prompt, completion = float(pr.get("prompt", 0)), float(pr.get("completion", 0))
            except (TypeError, ValueError):
                continue
            if prompt < 0 or completion < 0:  # "-1" marks router/variable-price entries
                continue
            cache = pr.get("input_cache_read")
            models[m["id"]] = ModelPrice(
                id=m["id"],
                name=m.get("name", m["id"]),
                prompt=prompt,
                completion=completion,
                cache_read=float(cache) if cache is not None and cache != "" else None,
                context_length=int(m.get("context_length") or 0),
            )
        return cls(models)


_cache: tuple[float, Pricing] | None = None


async def get_pricing(max_age: float = 3600.0) -> Pricing:
    """Fetch (and cache for an hour) the public model list. Returns an empty table offline."""
    global _cache
    if _cache and time.time() - _cache[0] < max_age:
        return _cache[1]
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.get(f"{BASE_URL}/models")
            resp.raise_for_status()
            pricing = Pricing.from_payload(resp.json())
    except httpx.HTTPError:
        return _cache[1] if _cache else Pricing({})
    _cache = (time.time(), pricing)
    return pricing


# ---------------------------------------------------------------- estimator
# Rough per-call token shapes; see the README. Deliberately simple: game length dominates.
POINTS_PER_TURN = 3.3  # die average minus failed-proposal penalties, plus rule-granted points
RULES_TOKENS = {"lite": 1100, "suber": 4200}
SYSTEM_TOKENS = 900
REASONING_TOKENS = {"none": 0, "minimal": 150, "low": 400, "medium": 1200, "high": 3000}
THINKING_OUT = 60  # the short visible "thinking" field on every action
VISIBLE_OUT = {
    k: v + THINKING_OUT
    for k, v in {"propose": 350, "discuss": 150, "vote": 120, "ruling": 500}.items()
}


def _price(pricing: Pricing, model: str | None) -> ModelPrice | None:
    if not model or model.startswith("mock/"):
        return None
    return pricing.get(model)


def estimate_game_cost(cfg: GameConfig, pricing: Pricing) -> dict[str, Any]:
    from nomic.rules import ruleset_info

    k = len(cfg.seats)
    win = cfg.win_points or int(ruleset_info(cfg.ruleset)["win_points"])
    rounds = max(1, math.ceil(win / POINTS_PER_TURN))
    turns = min(rounds * k, cfg.max_turns)
    rounds_eff = turns / k
    prefix = SYSTEM_TOKENS + RULES_TOKENS[cfg.ruleset]
    per_turn_detail = 200 + 200 * k if cfg.discussion else 200 + 90 * k
    state = 1500 + cfg.history_turns * per_turn_detail + 15 * turns // 2

    def call_cost(p: ModelPrice, extra_in: int, kind: str) -> float:
        effort = cfg.judge.reasoning_effort if kind == "ruling" else cfg.reasoning_effort
        read = p.cache_read if p.cache_read is not None else p.prompt
        return (
            prefix * read
            + (state + extra_in) * p.prompt
            + (VISIBLE_OUT[kind] + REASONING_TOKENS[effort]) * p.completion
        )

    seats: dict[str, float] = {}
    unknown: list[str] = []
    for s in cfg.seats:
        p = _price(pricing, s.model)
        if p is None:
            seats[s.name] = 0.0
            if s.model and not s.model.startswith("mock/"):
                unknown.append(s.model)
            continue
        # Each seat proposes once a round and votes on everyone else's proposals; with
        # discussion it comments on others' proposals and rebuts on its own (one message a turn).
        own_turns = rounds_eff
        cost = own_turns * call_cost(p, 0, "propose") + (turns - own_turns) * call_cost(
            p, 0, "vote"
        )
        if cfg.discussion:
            cost += turns * call_cost(p, 0, "discuss")
        if cfg.reflection:
            # one interview over the whole game record (~350 tokens per turn), ~700 out
            read = p.cache_read if p.cache_read is not None else p.prompt
            cost += prefix * read + turns * 350 * p.prompt + 700 * p.completion
        seats[s.name] = cost

    judge = 0.0
    jp = _price(pricing, cfg.judge.model)
    if jp is not None:
        judge = turns * call_cost(jp, 200 + 150 * k, "ruling")
    elif cfg.judge.model and not cfg.judge.model.startswith("mock/"):
        unknown.append(cfg.judge.model)

    total = sum(seats.values()) + judge
    return {
        "rounds": rounds,
        "turns": turns,
        "win_points": win,
        "seats": seats,
        "judge": judge,
        "total": total,
        "low": total * 0.5,
        "high": total * 2.0,
        "unknown_models": unknown,
    }
