"""Runs one game: the async turn loop, pause/step control, budget guard and human interventions.

Everything that changes the game goes through ``emit`` so it lands in the event log, the
SQLite store and every websocket subscriber.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from typing import Any

from pydantic import BaseModel

from nomic.agents import Agent, make_agent
from nomic.config import GameConfig
from nomic.engine.actions import RulingAction
from nomic.engine.events import Event
from nomic.engine.state import GameState
from nomic.engine.validate import default_changes, ruling_errors, sanitize_ruling
from nomic.human import HumanInbox
from nomic.judge import Judge, JudgeResult
from nomic.llm.openrouter import CallRecord, LLMError, OpenRouterClient
from nomic.llm.pricing import Pricing
from nomic.rules import load_ruleset
from nomic.store import Store

log = logging.getLogger(__name__)


class _GameOver(Exception):
    pass


class GameRunner:
    def __init__(
        self,
        game_id: str,
        cfg: GameConfig,
        store: Store,
        client: OpenRouterClient,
        pricing: Pricing | None = None,
        events: list[Event] | None = None,
    ) -> None:
        self.id = game_id
        self.cfg = cfg
        self.store = store
        self.client = client
        self.pricing = pricing
        self.events: list[Event] = []
        self.state = GameState()
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.inbox = HumanInbox(on_change=self._broadcast_pending)
        self.judge = Judge(cfg, client, self.inbox)
        self.agents: dict[str, Agent] = {}
        self.task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._step = False
        if events:
            for ev in events:
                self.state.apply(ev)
                self.events.append(ev)
            self.rng = random.Random(f"{cfg.seed}:{len(events)}")
        else:
            self.rng = random.Random(cfg.seed)
        self._build_agents()

    # ------------------------------------------------------------------ plumbing
    def _build_agents(self) -> None:
        for name, p in self.state.players.items():
            self.agents[name] = make_agent(p.model, self.cfg, self.client, self.inbox)

    def emit(self, type_: str, actor: str, data: dict[str, Any]) -> Event:
        ev = Event(seq=len(self.events) + 1, type=type_, actor=actor, data=data)
        self.state.apply(ev)  # raises before anything is persisted if the event is invalid
        self.events.append(ev)
        self.store.append_event(self.id, ev)
        self._broadcast({"type": "event", "event": ev.model_dump(), "state": self.snapshot()})
        return ev

    def snapshot(self) -> dict[str, Any]:
        return {"id": self.id, **self.state.to_dict(), "pending": self.inbox.list()}

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self.subscribers.discard(q)

    def _broadcast(self, msg: dict[str, Any]) -> None:
        for q in self.subscribers:
            q.put_nowait(msg)

    def _broadcast_pending(self) -> None:
        self._broadcast({"type": "pending", "pending": self.inbox.list()})

    def _record_calls(self, calls: list[CallRecord]) -> int | None:
        call_id = None
        for rec in calls:
            if not rec.cost and self.pricing is not None:
                rec.cost = self.pricing.call_cost(
                    rec.model, rec.prompt_tokens, rec.completion_tokens, rec.cached_tokens
                )
            call_id = self.store.record_call(self.id, rec)
            self.emit(
                "llm_usage",
                "system",
                {
                    "call_id": call_id,
                    "seat": rec.seat,
                    "model": rec.model,
                    "purpose": rec.purpose,
                    "cost": rec.cost,
                    "prompt_tokens": rec.prompt_tokens,
                    "completion_tokens": rec.completion_tokens,
                    "cached_tokens": rec.cached_tokens,
                },
            )
        return call_id

    # ------------------------------------------------------------------ lifecycle
    def create(self) -> None:
        """Emit the opening event for a brand-new game."""
        rules, win_points = load_ruleset(self.cfg.ruleset, self.cfg.win_points)
        self.emit(
            "game_created",
            "system",
            {
                "config": self.cfg.model_dump(),
                "rules": rules,
                "players": [{"name": s.name, "model": s.model} for s in self.cfg.seats],
                "win_points": win_points,
            },
        )
        self._build_agents()

    def recover(self) -> None:
        """After loading from disk: abandon any half-played turn and start paused."""
        if self.state.status == "over":
            return
        if self.state.current_turn is not None:
            self.emit(
                "turn_aborted",
                "system",
                {"turn": self.state.current_turn.turn, "reason": "server restarted"},
            )
        if self.state.status == "running":
            self.emit("status", "system", {"status": "paused", "reason": "server restarted"})

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run(), name=f"game-{self.id}")

    async def stop(self) -> None:
        self.inbox.cancel_all()
        if self.task is not None:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task

    async def run(self) -> None:
        while self.state.status != "over":
            try:
                await self._gate()
                await self._play_turn()
            except _GameOver:
                break
            except asyncio.CancelledError:
                if self.state.status == "over":
                    break
                raise
            except Exception as e:
                log.exception("game %s: turn failed", self.id)
                if self.state.status == "over":
                    break
                if self.state.current_turn is not None:
                    self.emit(
                        "turn_aborted",
                        "system",
                        {"turn": self.state.current_turn.turn, "reason": str(e)[:500]},
                    )
                self.pause(f"error: {str(e)[:300]}")
                continue
            if self._step:
                self._step = False
                self.pause("stepped one turn")
        self.inbox.cancel_all()

    async def _gate(self) -> None:
        """Block while paused; pause automatically when the budget is spent."""
        while True:
            if self.state.status == "over":
                raise _GameOver
            if self.state.status == "running" and self.state.over_budget:
                self.pause(f"budget of ${self.state.budget_usd:.2f} reached")
            if self.state.status == "running":
                return
            self._wake.clear()
            await self._wake.wait()

    # ------------------------------------------------------------------ controls
    def pause(self, reason: str = "paused by human") -> None:
        if self.state.status == "running":
            self.emit("status", "system", {"status": "paused", "reason": reason})

    def resume(self) -> None:
        if self.state.status == "paused":
            self.emit("status", "system", {"status": "running", "reason": ""})
        self._wake.set()

    def step(self) -> None:
        self._step = True
        self.resume()

    def intervene(self, kind: str, data: dict[str, Any]) -> None:
        st = self.state
        if kind == "edit_rule":
            number = int(data["number"])
            if not data.get("text") and number not in st.rules:
                raise ValueError("text is required for a new rule")
            self.emit(
                "rule_changed",
                "human",
                {
                    "op": "set",
                    "number": number,
                    "text": data.get("text") or None,
                    "mutable": data.get("mutable"),
                    "new_number": None,
                    "source": "human",
                },
            )
        elif kind == "repeal_rule":
            number = int(data["number"])
            if number not in st.rules:
                raise ValueError(f"rule {number} does not exist")
            self.emit(
                "rule_changed",
                "human",
                {
                    "op": "repeal",
                    "number": number,
                    "text": None,
                    "mutable": None,
                    "new_number": None,
                    "source": "human",
                },
            )
        elif kind == "set_score":
            player = self._player(data["player"])
            delta = int(data["score"]) - st.players[player].score
            self.emit(
                "score_changed",
                "human",
                {"player": player, "delta": delta, "reason": "set by human", "source": "human"},
            )
        elif kind == "swap_model":
            player = self._player(data["player"])
            model = data.get("model") or None
            self.emit("model_swapped", "human", {"player": player, "model": model})
            self.agents[player] = make_agent(model, self.cfg, self.client, self.inbox)
        elif kind == "gm_message":
            text = str(data.get("text", "")).strip()
            if not text:
                raise ValueError("empty message")
            self.emit(
                "message", "human", {"player": "GM", "text": text, "kind": "gm", "call_id": None}
            )
        elif kind == "set_budget":
            self.emit("budget_changed", "human", {"budget_usd": float(data["budget_usd"])})
        elif kind == "end_game":
            winner = data.get("winner") or None
            if winner is not None:
                self._player(winner)
            self.emit("game_over", "human", {"winner": winner, "reason": "ended by human"})
            self.inbox.cancel_all()
            self._wake.set()
        else:
            raise ValueError(f"unknown intervention {kind!r}")

    def _player(self, name: str) -> str:
        if name not in self.state.players:
            raise ValueError(f"unknown player {name!r}")
        return name

    # ------------------------------------------------------------------ the turn
    async def _act(self, player: str, kind: str) -> tuple[BaseModel, int | None]:
        try:
            reply = await self.agents[player].act(kind, self.state, player)
        except LLMError as e:
            self._record_calls(e.records)
            raise
        return reply.action, self._record_calls(reply.calls)

    async def _play_turn(self) -> None:
        st, cfg = self.state, self.cfg
        if st.completed_turns >= cfg.max_turns:
            top = max(p.score for p in st.players.values())
            leaders = [n for n in st.seat_order if st.players[n].score == top]
            self.emit(
                "game_over",
                "system",
                {
                    "winner": leaders[0] if len(leaders) == 1 else None,
                    "reason": f"turn cap of {cfg.max_turns} reached",
                },
            )
            raise _GameOver
        player = st.next_player()
        turn = st.completed_turns + 1
        self.emit(
            "turn_started", "system", {"turn": turn, "round": st.round_of(turn), "player": player}
        )

        action, cid = await self._act(player, "propose")
        number = st.next_number
        self.emit(
            "proposal",
            player,
            {"number": number, "player": player, **action.model_dump(), "call_id": cid},
        )

        if cfg.discussion:
            await self._gate()
            said = await asyncio.gather(*(self._act(n, "discuss") for n in st.seat_order))
            for n, (a, cid) in zip(st.seat_order, said, strict=True):
                self.emit(
                    "message",
                    n,
                    {
                        "player": n,
                        "text": a.model_dump()["message"],
                        "kind": "discussion",
                        "call_id": cid,
                    },
                )

        await self._gate()
        votes = await asyncio.gather(*(self._act(n, "vote") for n in st.seat_order))
        for n, (a, cid) in zip(st.seat_order, votes, strict=True):
            self.emit("vote", n, {"player": n, **a.model_dump(), "call_id": cid})

        self.emit("dice_roll", "system", {"player": player, "value": self.rng.randint(1, 6)})
        await self._gate()
        result, cid = await self._judge()
        ruling = result.ruling

        changes = ruling.rule_changes
        cur = st.current_turn
        assert cur is not None and cur.proposal is not None
        if ruling.legal and ruling.passed and not changes:
            implied = default_changes(cur.proposal, number)
            probe = ruling.model_copy(update={"rule_changes": implied})
            if not ruling_errors(st, probe):
                changes = implied
        self.emit(
            "ruling",
            "judge",
            {
                "number": number,
                **ruling.model_dump(),
                "rule_changes": [c.model_dump() for c in changes],
                "by": result.by,
                "call_id": cid,
            },
        )
        if ruling.legal and ruling.passed:
            for ch in changes:
                self.emit(
                    "rule_changed", "judge", {**ch.model_dump(), "source": f"proposal {number}"}
                )
        for d in ruling.score_deltas:
            if d.delta:
                self.emit("score_changed", "judge", {**d.model_dump(), "source": "judge"})
        self.emit("turn_ended", "system", {"turn": turn})
        if ruling.winner:
            self.emit("game_over", "judge", {"winner": ruling.winner, "reason": "judge ruling"})

    async def _judge(self) -> tuple[JudgeResult, int | None]:
        feedback: list[str] | None = None
        cid: int | None = None
        result: JudgeResult | None = None
        for _ in range(3):
            try:
                result = await self.judge.rule(self.state, feedback)
            except LLMError as e:
                self._record_calls(e.records)
                raise
            cid = self._record_calls(result.calls) or cid
            errors = ruling_errors(self.state, result.ruling)
            if not errors:
                return result, cid
            log.warning("game %s: judge ruling rejected: %s", self.id, errors)
            feedback = errors
        assert result is not None
        fixed, dropped = sanitize_ruling(self.state, result.ruling)
        note = f" [engine dropped: {'; '.join(dropped)}]" if dropped else ""
        fixed = RulingAction.model_validate(
            {**fixed.model_dump(), "rationale": fixed.rationale + note}
        )
        return JudgeResult(fixed, result.by, []), cid
