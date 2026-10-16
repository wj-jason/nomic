"""Player agents. Every agent answers the three player actions with a validated model."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel

from nomic.engine.state import GameState
from nomic.llm.openrouter import CallRecord


@dataclass
class Reply:
    action: BaseModel
    calls: list[CallRecord] = field(default_factory=list)


class Agent(Protocol):
    async def act(self, kind: str, state: GameState, player: str) -> Reply: ...
