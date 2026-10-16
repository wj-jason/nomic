"""Game state, rebuilt by folding the event log."""

from __future__ import annotations

import copy
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from nomic.engine.events import Event


@dataclass
class Rule:
    number: int
    text: str
    mutable: bool
    # [{seq, op, text, mutable, source, number}] oldest first; the first entry is the initial text.
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Player:
    name: str
    model: str | None
    score: int = 0
    cost: float = 0.0

    @property
    def human(self) -> bool:
        return self.model is None


@dataclass
class TurnRecord:
    turn: int
    round: int
    player: str
    proposal: dict[str, Any] | None = None
    messages: list[dict[str, Any]] = field(default_factory=list)
    votes: dict[str, dict[str, Any]] = field(default_factory=dict)
    dice: int | None = None
    ruling: dict[str, Any] | None = None
    score_changes: list[dict[str, Any]] = field(default_factory=list)
    rule_changes: list[dict[str, Any]] = field(default_factory=list)
    ended: bool = False
    aborted: bool = False

    @property
    def open(self) -> bool:
        return not (self.ended or self.aborted)


class RuleChangeError(ValueError):
    pass


def apply_rule_change(
    rules: dict[int, Rule], change: dict[str, Any], *, seq: int = 0, force: bool = False
) -> None:
    """Apply one rule change in place. ``force`` (human edits) skips the mutability checks."""
    op = change["op"]
    number = int(change["number"])
    text = change.get("text")
    existing = rules.get(number)
    source = change.get("source", "")

    if op == "set":  # human-only: create or overwrite a rule outright
        mutable = change.get("mutable")
        if existing is None:
            if not text:
                raise RuleChangeError("set needs text for a new rule")
            rules[number] = Rule(number, text, True if mutable is None else bool(mutable))
        else:
            existing.text = text or existing.text
            if mutable is not None:
                existing.mutable = bool(mutable)
    elif op == "enact":
        if existing is not None:
            raise RuleChangeError(f"rule {number} already exists")
        if not text:
            raise RuleChangeError("enact needs text")
        mutable = change.get("mutable")
        rules[number] = Rule(number, text, True if mutable is None else bool(mutable))
    else:
        if existing is None:
            raise RuleChangeError(f"rule {number} does not exist")
        if op == "repeal":
            if not existing.mutable and not force:
                raise RuleChangeError(f"rule {number} is immutable and cannot be repealed")
            existing.history.append(
                {
                    "seq": seq,
                    "op": "repeal",
                    "text": None,
                    "mutable": existing.mutable,
                    "source": source,
                }
            )
            del rules[number]
            return
        if op == "amend":
            if not existing.mutable and not force:
                raise RuleChangeError(f"rule {number} is immutable and cannot be amended")
            if not text:
                raise RuleChangeError("amend needs text")
            existing.text = text
        elif op == "transmute":
            target = change.get("mutable")
            existing.mutable = (not existing.mutable) if target is None else bool(target)
        else:
            raise RuleChangeError(f"unknown op {op!r}")

    rule = rules[number]
    new_number = change.get("new_number")
    if new_number is not None and int(new_number) != number:
        if int(new_number) in rules:
            raise RuleChangeError(f"cannot renumber {number} to {new_number}: already taken")
        del rules[number]
        rule.number = int(new_number)
        rules[rule.number] = rule
    rule.history.append(
        {
            "seq": seq,
            "op": op,
            "text": rule.text,
            "mutable": rule.mutable,
            "source": source,
            "number": rule.number,
        }
    )


@dataclass
class GameState:
    config: dict[str, Any] = field(default_factory=dict)
    win_points: int = 0
    rules: dict[int, Rule] = field(default_factory=dict)
    players: dict[str, Player] = field(default_factory=dict)
    seat_order: list[str] = field(default_factory=list)
    turns: list[TurnRecord] = field(default_factory=list)
    gm_log: list[dict[str, Any]] = field(default_factory=list)  # GM messages & human edits
    status: str = "created"  # created | running | paused | over
    status_reason: str = ""
    winner: str | None = None
    end_reason: str = ""
    next_number: int = 301
    judge_cost: float = 0.0
    total_cost: float = 0.0
    budget_usd: float = 0.0
    seq: int = 0

    # ------------------------------------------------------------------ folding
    @classmethod
    def from_events(cls, events: Iterable[Event]) -> GameState:
        state = cls()
        for ev in events:
            state.apply(ev)
        return state

    def apply(self, ev: Event) -> None:
        self.seq = ev.seq
        d = ev.data
        handler = getattr(self, f"_on_{ev.type}", None)
        if handler is None:
            raise ValueError(f"unknown event type {ev.type!r}")
        handler(ev, d)

    def _on_game_created(self, ev: Event, d: dict[str, Any]) -> None:
        self.config = d["config"]
        self.win_points = int(d["win_points"])
        self.budget_usd = float(d["config"].get("budget_usd", 0.0))
        for r in d["rules"]:
            self.rules[r["number"]] = Rule(
                r["number"],
                r["text"],
                r["mutable"],
                history=[
                    {
                        "seq": ev.seq,
                        "op": "initial",
                        "text": r["text"],
                        "mutable": r["mutable"],
                        "source": "initial",
                        "number": r["number"],
                    }
                ],
            )
        for p in d["players"]:
            self.players[p["name"]] = Player(p["name"], p.get("model"))
            self.seat_order.append(p["name"])
        self.status = "running"

    def _on_turn_started(self, ev: Event, d: dict[str, Any]) -> None:
        self.turns.append(TurnRecord(turn=d["turn"], round=d["round"], player=d["player"]))

    def _on_proposal(self, ev: Event, d: dict[str, Any]) -> None:
        self._require_turn().proposal = d
        self.next_number = int(d["number"]) + 1

    def _on_message(self, ev: Event, d: dict[str, Any]) -> None:
        if d.get("kind") == "gm":
            self.gm_log.append({"seq": ev.seq, "turn": self.completed_turns, "text": d["text"]})
        else:
            self._require_turn().messages.append(d)

    def _on_vote(self, ev: Event, d: dict[str, Any]) -> None:
        self._require_turn().votes[d["player"]] = d

    def _on_dice_roll(self, ev: Event, d: dict[str, Any]) -> None:
        self._require_turn().dice = int(d["value"])

    def _on_ruling(self, ev: Event, d: dict[str, Any]) -> None:
        self._require_turn().ruling = d

    def _on_rule_changed(self, ev: Event, d: dict[str, Any]) -> None:
        apply_rule_change(self.rules, d, seq=ev.seq, force=d.get("source") == "human")
        turn = self.current_turn
        if turn is not None and d.get("source") != "human":
            turn.rule_changes.append(d)
        if d.get("source") == "human":
            self.gm_log.append(
                {
                    "seq": ev.seq,
                    "turn": self.completed_turns,
                    "text": f"[human edit] {d['op']} rule {d['number']}",
                }
            )

    def _on_score_changed(self, ev: Event, d: dict[str, Any]) -> None:
        self.players[d["player"]].score += int(d["delta"])
        turn = self.current_turn
        if turn is not None and d.get("source") != "human":
            turn.score_changes.append(d)
        if d.get("source") == "human":
            self.gm_log.append(
                {
                    "seq": ev.seq,
                    "turn": self.completed_turns,
                    "text": f"[human edit] {d['player']} {d['delta']:+d} points",
                }
            )

    def _on_turn_ended(self, ev: Event, d: dict[str, Any]) -> None:
        self._require_turn().ended = True

    def _on_turn_aborted(self, ev: Event, d: dict[str, Any]) -> None:
        self._require_turn().aborted = True

    def _on_game_over(self, ev: Event, d: dict[str, Any]) -> None:
        self.status = "over"
        self.winner = d.get("winner")
        self.end_reason = d.get("reason", "")
        if self.current_turn is not None:
            self.current_turn.ended = True

    def _on_status(self, ev: Event, d: dict[str, Any]) -> None:
        if self.status != "over":
            self.status = d["status"]
            self.status_reason = d.get("reason", "")

    def _on_model_swapped(self, ev: Event, d: dict[str, Any]) -> None:
        self.players[d["player"]].model = d.get("model") or None

    def _on_budget_changed(self, ev: Event, d: dict[str, Any]) -> None:
        self.budget_usd = float(d["budget_usd"])

    def _on_llm_usage(self, ev: Event, d: dict[str, Any]) -> None:
        cost = float(d.get("cost") or 0.0)
        self.total_cost += cost
        seat = d.get("seat")
        if seat in self.players:
            self.players[seat].cost += cost
        else:
            self.judge_cost += cost

    def _require_turn(self) -> TurnRecord:
        turn = self.current_turn
        if turn is None:
            raise ValueError("no turn in progress")
        return turn

    # ------------------------------------------------------------------ queries
    @property
    def current_turn(self) -> TurnRecord | None:
        if self.turns and self.turns[-1].open:
            return self.turns[-1]
        return None

    @property
    def completed_turns(self) -> int:
        return sum(1 for t in self.turns if t.ended)

    @property
    def finished_turns(self) -> list[TurnRecord]:
        return [t for t in self.turns if t.ended]

    def round_of(self, turn: int) -> int:
        return (turn - 1) // max(1, len(self.seat_order)) + 1

    def next_player(self) -> str:
        """Whose turn is next: the seat after the last completed turn (an aborted turn is
        replayed by the same player)."""
        done = self.finished_turns
        if not done:
            return self.seat_order[0]
        last = done[-1].player
        idx = self.seat_order.index(last) if last in self.seat_order else -1
        return self.seat_order[(idx + 1) % len(self.seat_order)]

    @property
    def phase(self) -> str:
        if self.status == "over":
            return "over"
        t = self.current_turn
        if t is None:
            return "between turns"
        if t.ruling is not None:
            return "applying"
        if t.dice is not None:
            return "judging"
        if t.votes:
            return "voting"
        if t.messages:
            return "discussion"
        if t.proposal is not None:
            return "deliberating"
        return "proposing"

    @property
    def over_budget(self) -> bool:
        return self.budget_usd > 0 and self.total_cost >= self.budget_usd

    def clone_rules(self) -> dict[int, Rule]:
        return copy.deepcopy(self.rules)

    def to_dict(self) -> dict[str, Any]:
        cur = self.current_turn
        return {
            "status": self.status,
            "status_reason": self.status_reason,
            "phase": self.phase,
            "winner": self.winner,
            "end_reason": self.end_reason,
            "win_points": self.win_points,
            "config": self.config,
            "seq": self.seq,
            "completed_turns": self.completed_turns,
            "round": self.round_of(self.completed_turns + 1),
            "next_player": self.next_player() if self.seat_order else None,
            "next_number": self.next_number,
            "rules": [asdict(r) for _, r in sorted(self.rules.items())],
            "players": [
                {**asdict(self.players[n]), "human": self.players[n].human} for n in self.seat_order
            ],
            "current_turn": asdict(cur) if cur else None,
            "gm_log": self.gm_log[-20:],
            "judge_cost": self.judge_cost,
            "total_cost": self.total_cost,
            "budget_usd": self.budget_usd,
        }
