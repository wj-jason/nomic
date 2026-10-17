"""Structured actions that players and judges return, plus strict JSON schemas for them."""

from __future__ import annotations

import copy
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Op = Literal["enact", "repeal", "amend", "transmute"]
ActionKind = Literal["propose", "discuss", "rebut", "vote"]

THINKING_SENTENCES = 3
THINKING_MAX_CHARS = 450


def clip_thinking(text: str) -> str:
    """Keep the first few sentences of a model's private reasoning."""
    sentences = re.split(r"(?<=[.!?])\s+", " ".join(text.split()))
    out = " ".join(sentences[:THINKING_SENTENCES])
    if len(out) > THINKING_MAX_CHARS:
        out = out[: THINKING_MAX_CHARS - 1].rstrip() + "…"
    return out


class _Thinks(BaseModel):
    """Every action starts with a short private rationale (declared first, so models write
    it before committing to an answer). It is stored and shown to the human, but never put
    in another player's or the judge's prompt."""

    thinking: str = Field(
        default="",
        description="Your private reasoning in at most 3 sentences. Never shown to other players.",
    )

    @field_validator("thinking")
    @classmethod
    def _clip(cls, v: str) -> str:
        return clip_thinking(v)


class ProposalAction(_Thinks):
    op: Op
    target_rule: int | None = Field(
        default=None, description="Rule to repeal, amend or transmute; null when enacting."
    )
    text: str | None = Field(
        default=None, description="Full new rule text for enact/amend; null otherwise."
    )
    summary: str = Field(description="One or two sentences pitching the proposal to voters.")


class DiscussAction(_Thinks):
    message: str = Field(description="What you say to the table (under 120 words).")


class VoteAction(_Thinks):
    vote: Literal["yes", "no"]
    reason: str = Field(description="One sentence explaining your vote.")
    dispute: str | None = Field(
        default=None,
        description="If you believe this proposal or move is illegal, say why; otherwise null.",
    )


class RuleChange(BaseModel):
    op: Op
    number: int = Field(description="Rule affected; for enact, the new rule's number.")
    text: str | None = Field(default=None, description="New rule text for enact/amend.")
    mutable: bool | None = Field(
        default=None, description="Status after the change (enact/transmute); null otherwise."
    )
    new_number: int | None = Field(
        default=None, description="If the rules renumber the affected rule, its new number."
    )


class ScoreDelta(BaseModel):
    player: str
    delta: int
    reason: str


class RulingAction(_Thinks):
    legal: bool = Field(description="Whether the proposal was a legal move.")
    passed: bool = Field(description="Whether the proposal was adopted under the current rules.")
    rule_changes: list[RuleChange] = Field(
        description="Changes that take effect now (empty unless legal and passed)."
    )
    score_deltas: list[ScoreDelta] = Field(
        description="Every point change this turn, including the die roll if the rules use it."
    )
    winner: str | None = Field(
        description="Name of the player who has won after these changes, else null."
    )
    rationale: str = Field(description="Short explanation citing rule numbers.")


class ReflectionAction(BaseModel):
    """A player's post-game interview. Public: shown in the feed and the transcript."""

    overview: str = Field(description="How the game went for you, in 2-4 sentences.")
    key_decisions: str = Field(
        description="Your most important decisions (proposals, votes, deals) and why you made "
        "them, in 2-5 sentences."
    )
    would_change: str = Field(description="What you would do differently, in 2-4 sentences.")
    about_others: str = Field(
        description="Your read on the other players' strategies and who played best, in 2-4 "
        "sentences."
    )


ACTION_MODELS: dict[str, type[BaseModel]] = {
    "propose": ProposalAction,
    "discuss": DiscussAction,
    "rebut": DiscussAction,  # the proposer answering the table's comments
    "vote": VoteAction,
    "ruling": RulingAction,
    "reflect": ReflectionAction,
}


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema -> OpenAI-style strict schema: refs inlined, every property required,
    no additional properties, no defaults or titles."""
    raw = model.model_json_schema()
    defs = raw.pop("$defs", {})

    def fix(node: Any) -> Any:
        if isinstance(node, list):
            return [fix(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return fix(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
        out = {k: fix(v) for k, v in node.items() if k not in ("default", "title")}
        if out.get("type") == "object" and "properties" in out:
            out["required"] = list(out["properties"])
            out["additionalProperties"] = False
        return out

    result: dict[str, Any] = fix(raw)
    return result
