from __future__ import annotations

from nomic.agents.base import Reply
from nomic.agents.prompts import player_messages
from nomic.config import GameConfig
from nomic.engine.actions import ACTION_MODELS
from nomic.engine.state import GameState
from nomic.llm.openrouter import OpenRouterClient


class LLMAgent:
    def __init__(self, client: OpenRouterClient, model: str, cfg: GameConfig) -> None:
        self.client = client
        self.model = model
        self.cfg = cfg

    async def act(self, kind: str, state: GameState, player: str) -> Reply:
        action, calls = await self.client.structured(
            self.model,
            player_messages(state, player, kind, self.cfg),
            ACTION_MODELS[kind],
            purpose=kind,
            seat=player,
            effort=self.cfg.reasoning_effort,
            max_tokens=self.cfg.max_output_tokens,
        )
        return Reply(action, calls)
