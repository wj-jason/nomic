"""Owns every game runner in the process: create, reload from disk, fork."""

from __future__ import annotations

import uuid

from nomic.config import GameConfig
from nomic.llm.openrouter import OpenRouterClient
from nomic.llm.pricing import Pricing, get_pricing
from nomic.runner import GameRunner
from nomic.store import Store


class GameManager:
    def __init__(self, store: Store, client: OpenRouterClient | None = None) -> None:
        self.store = store
        self.client = client or OpenRouterClient()
        self.pricing: Pricing | None = None
        self.runners: dict[str, GameRunner] = {}

    async def refresh_pricing(self) -> Pricing:
        self.pricing = await get_pricing()
        for r in self.runners.values():
            r.pricing = self.pricing
        return self.pricing

    def _check(self, cfg: GameConfig) -> None:
        real = [m for m in cfg.models if not m.startswith("mock/")]
        if real and not self.client.api_key:
            raise ValueError("OPENROUTER_API_KEY is not set; only mock/ models can be used")
        if cfg.judge.model is None and cfg.judge.mode != "human":
            raise ValueError("a judge model is required unless the judge mode is 'human'")

    def create(self, cfg: GameConfig, *, start: bool = True) -> GameRunner:
        self._check(cfg)
        gid = uuid.uuid4().hex[:8]
        self.store.create_game(gid, cfg.model_dump())
        runner = GameRunner(gid, cfg, self.store, self.client, self.pricing)
        runner.create()
        self.runners[gid] = runner
        if start:
            runner.start()
        return runner

    def load_all(self) -> None:
        """Rebuild runners for games on disk. They come back paused."""
        for g in self.store.list_games():
            if g["id"] in self.runners:
                continue
            events = self.store.events(g["id"])
            if not events:
                continue
            runner = GameRunner(
                g["id"],
                GameConfig.model_validate(g["config"]),
                self.store,
                self.client,
                self.pricing,
                events=events,
            )
            runner.recover()
            self.runners[g["id"]] = runner
            runner.start()

    def fork(self, game_id: str, seq: int) -> GameRunner:
        """Start a new game from the first ``seq`` events of an existing one (paused)."""
        parent = self.runners[game_id]
        # Drop game_over so forking the end of a finished game reopens it.
        kept = [e for e in parent.events if e.seq <= seq and e.type != "game_over"]
        if not kept:
            raise ValueError("nothing to fork")
        prefix = [e.model_copy(update={"seq": i}) for i, e in enumerate(kept, start=1)]
        gid = uuid.uuid4().hex[:8]
        self.store.create_game(gid, parent.cfg.model_dump(), parent_id=game_id, fork_seq=seq)
        for ev in prefix:
            self.store.append_event(gid, ev)
        runner = GameRunner(gid, parent.cfg, self.store, self.client, self.pricing, events=prefix)
        runner.recover()
        if runner.state.status == "running":
            runner.pause("forked")
        self.runners[gid] = runner
        runner.start()
        return runner

    def delete(self, game_id: str) -> None:
        runner = self.runners.pop(game_id, None)
        if runner is not None and runner.task is not None:
            runner.inbox.cancel_all()
            runner.task.cancel()
        self.store.delete_game(game_id)

    async def shutdown(self) -> None:
        for r in self.runners.values():
            await r.stop()
        await self.client.aclose()
