"""Game configuration (loaded from YAML in configs/ or posted by the frontend)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high"]
JudgeMode = Literal["llm", "human", "llm_with_human_override", "human_on_appeal"]
RulesetName = Literal["lite", "suber"]


class SeatConfig(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    # OpenRouter model id, "mock/<anything>" for an offline scripted player, None for a human.
    model: str | None = None

    @property
    def human(self) -> bool:
        return self.model is None


class JudgeConfig(BaseModel):
    mode: JudgeMode = "llm"
    # Model that drafts rulings. In "human" mode it is optional and only pre-fills the form.
    model: str | None = "deepseek/deepseek-v4-pro"
    # llm_with_human_override: seconds to wait for a human verdict before the LLM draft
    # stands. None waits indefinitely.
    review_seconds: float | None = None
    # The judge does the bookkeeping (thresholds, stacked point rules), so unlike players it
    # gets hidden reasoning by default, and room to answer after it.
    reasoning_effort: ReasoningEffort = "low"
    max_output_tokens: int = Field(default=1500, ge=256)


class GameConfig(BaseModel):
    ruleset: RulesetName = "lite"
    win_points: int | None = Field(default=None, ge=1)  # None: the ruleset's default
    seats: list[SeatConfig] = Field(min_length=2, max_length=12)
    judge: JudgeConfig = JudgeConfig()
    budget_usd: float = Field(default=5.0, ge=0)
    max_turns: int = Field(default=150, ge=1)
    discussion: bool = True
    # Native (hidden) reasoning. Off by default: every action carries a short visible
    # "thinking" field instead, which is faster and cheaper.
    reasoning_effort: ReasoningEffort = "none"
    # Cap on the visible answer (reasoning headroom is added per effort). Bounds the cost of a
    # reply that runs away, and what OpenRouter reserves from your balance per request.
    max_output_tokens: int = Field(default=800, ge=256)
    reflection: bool = True  # post-game interview from each model seat when the game ends
    history_turns: int = Field(default=3, ge=0)  # turns shown in full; older ones are digested
    seed: int | None = None

    @model_validator(mode="after")
    def _unique_names(self) -> GameConfig:
        names = [s.name for s in self.seats]
        if len(set(names)) != len(names):
            raise ValueError("seat names must be unique")
        if any(n.lower() in ("judge", "gm", "system", "human") for n in names):
            raise ValueError("seat names judge/gm/system/human are reserved")
        return self

    @property
    def models(self) -> set[str]:
        out = {s.model for s in self.seats if s.model}
        if self.judge.model:
            out.add(self.judge.model)
        return out


def load_config(path: str | Path) -> GameConfig:
    return GameConfig.model_validate(yaml.safe_load(Path(path).read_text()))
