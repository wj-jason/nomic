from __future__ import annotations

from nomic.agents.base import Reply
from nomic.engine.actions import ACTION_MODELS
from nomic.engine.state import GameState
from nomic.human import HumanInbox


class HumanAgent:
    """A seat played from the web UI: each action waits until the human submits it."""

    def __init__(self, inbox: HumanInbox) -> None:
        self.inbox = inbox

    async def act(self, kind: str, state: GameState, player: str) -> Reply:
        cur = state.current_turn
        context = {
            "proposal": cur.proposal if cur else None,
            "next_number": state.next_number,
            "score": state.players[player].score,
        }
        while True:
            answer = await self.inbox.request(kind, player, context)
            if answer is None:
                continue
            return Reply(ACTION_MODELS[kind].model_validate(answer))
