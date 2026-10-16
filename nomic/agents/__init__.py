from __future__ import annotations

from nomic.agents.base import Agent, Reply
from nomic.agents.human_agent import HumanAgent
from nomic.agents.llm_agent import LLMAgent
from nomic.agents.mock_agent import MockAgent
from nomic.config import GameConfig
from nomic.human import HumanInbox
from nomic.llm.openrouter import OpenRouterClient

__all__ = ["Agent", "HumanAgent", "LLMAgent", "MockAgent", "Reply", "make_agent"]


def make_agent(
    model: str | None, cfg: GameConfig, client: OpenRouterClient, inbox: HumanInbox
) -> Agent:
    if model is None:
        return HumanAgent(inbox)
    if model.startswith("mock/"):
        return MockAgent(seed=cfg.seed or 0, delay=1.0 if model == "mock/slow" else 0.0)
    return LLMAgent(client, model, cfg)
