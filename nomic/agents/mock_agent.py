"""Offline scripted player (model ids "mock/..."), for tests and for trying the UI for free."""

from __future__ import annotations

import asyncio
import random
import zlib

from nomic.agents.base import Reply
from nomic.engine.actions import DiscussAction, ProposalAction, ReflectionAction, VoteAction
from nomic.engine.state import GameState

_FLAVOURS = [
    "Players should address each other politely in all discussion.",
    "A player who proposes a rule mentioning the moon gains 1 point when it is adopted.",
    "Proposals should be summarised in one sentence.",
    "The Judge is encouraged to explain rulings briefly.",
    "Players may describe their proposals as 'bold' without penalty.",
]


class MockAgent:
    def __init__(self, seed: int = 0, yes_rate: float = 0.75, delay: float = 0.0) -> None:
        self.seed = seed
        self.yes_rate = yes_rate
        self.delay = delay  # "mock/slow" thinks for a while, so you can watch a game in the UI

    def _rng(self, state: GameState, player: str, kind: str) -> random.Random:
        key = f"{self.seed}:{player}:{kind}:{state.seq}".encode()
        return random.Random(zlib.crc32(key))

    async def act(self, kind: str, state: GameState, player: str) -> Reply:
        rng = self._rng(state, player, kind)
        if self.delay:
            await asyncio.sleep(self.delay * (0.5 + rng.random()))
        if kind == "propose":
            mutable = [n for n, r in state.rules.items() if r.mutable]
            enacted = [n for n in mutable if n >= 301]
            if enacted and rng.random() < 0.3:
                n = rng.choice(enacted)
                return Reply(
                    ProposalAction(
                        thinking=f"Rule {n} is mine to tidy. Low risk, likely to pass.",
                        op="amend",
                        target_rule=n,
                        text=rng.choice(_FLAVOURS),
                        summary=f"Tidy up rule {n}.",
                    )
                )
            return Reply(
                ProposalAction(
                    thinking="A harmless rule should pass and avoid the defeat penalty.",
                    op="enact",
                    text=rng.choice(_FLAVOURS),
                    summary="A harmless flavour rule.",
                )
            )
        if kind == "discuss":
            return Reply(
                DiscussAction(
                    thinking="Nothing here changes the balance of power.",
                    message=rng.choice(
                        ["Seems fine to me.", "I'm not convinced.", "Let's keep the game moving."]
                    ),
                )
            )
        if kind == "reflect":
            return Reply(
                ReflectionAction(
                    overview=f"I finished on {state.players[player].score} points.",
                    key_decisions="I proposed harmless flavour rules so they would pass.",
                    would_change="Propose rules that actually earn me points.",
                    about_others="Everyone played it safe; nobody took real risks.",
                )
            )
        if kind == "rebut":
            return Reply(
                DiscussAction(
                    thinking="Reassure the doubters; it costs me nothing.",
                    message="It's harmless, I promise. Vote yes and we all move on.",
                )
            )
        own = state.current_turn is not None and state.current_turn.player == player
        yes = own or rng.random() < self.yes_rate
        return Reply(
            VoteAction(
                thinking="My own proposal, so yes." if own else "Rolling the dice on this one.",
                vote="yes" if yes else "no",
                reason="Scripted vote.",
                dispute=None,
            )
        )
