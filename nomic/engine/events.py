"""The append-only event log. Game state is always ``fold(events)``.

Event types and their ``data``:

- game_created   {config, rules: [{number, text, mutable}], players: [{name, model}], win_points}
- turn_started   {turn, round, player}
- proposal       {number, player, op, target_rule, text, summary, call_id}
- message        {player, text, kind: "discussion" | "gm", call_id}
- vote           {player, vote: "yes" | "no", reason, dispute, call_id}
- dice_roll      {player, value}
- ruling         {number, legal, passed, rule_changes, score_deltas, winner, rationale, by, call_id}
- rule_changed   {op: enact|repeal|amend|transmute|set, number, text, mutable, new_number, source}
- score_changed  {player, delta, reason, source}
- turn_ended     {turn}
- turn_aborted   {turn, reason}
- game_over      {winner, reason}
- status         {status: "running" | "paused", reason}
- model_swapped  {player, model}
- budget_changed {budget_usd}
- llm_usage      {call_id, seat, model, purpose, cost, prompt_tokens, completion_tokens, cached_tokens}
- reflection     {player, overview, key_decisions, would_change, about_others, call_id}
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

EVENT_TYPES = frozenset(
    {
        "game_created",
        "turn_started",
        "proposal",
        "message",
        "vote",
        "dice_roll",
        "ruling",
        "rule_changed",
        "score_changed",
        "turn_ended",
        "turn_aborted",
        "game_over",
        "status",
        "model_swapped",
        "budget_changed",
        "llm_usage",
        "reflection",
    }
)


class Event(BaseModel):
    seq: int
    type: str
    actor: str  # a player name, "judge", "human" or "system"
    data: dict[str, Any] = Field(default_factory=dict)
    ts: float = Field(default_factory=time.time)
