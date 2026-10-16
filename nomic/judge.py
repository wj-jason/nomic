"""The Judge: an LLM (or scripted) drafter, optionally reviewed or replaced by a human.

Modes:
- llm                      the model's ruling stands
- human                    a human rules every turn (the model's draft, if any, pre-fills the form)
- llm_with_human_override  the model rules, a human approves/edits it (optionally with a timeout)
- human_on_appeal          the model rules, but any player dispute sends the turn to the human
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from nomic.agents.prompts import judge_messages
from nomic.config import GameConfig
from nomic.engine.actions import RulingAction, ScoreDelta
from nomic.engine.state import GameState
from nomic.engine.validate import default_changes, ruling_errors
from nomic.human import HumanInbox
from nomic.llm.openrouter import CallRecord, OpenRouterClient


@dataclass
class JudgeResult:
    ruling: RulingAction
    by: str  # llm | mock | human
    calls: list[CallRecord] = field(default_factory=list)


class LLMJudge:
    def __init__(self, client: OpenRouterClient, model: str, cfg: GameConfig) -> None:
        self.client, self.model, self.cfg = client, model, cfg

    async def draft(
        self, state: GameState, feedback: list[str] | None
    ) -> tuple[RulingAction, list[CallRecord]]:
        return await self.client.structured(
            self.model,
            judge_messages(state, self.cfg, feedback),
            RulingAction,
            purpose="ruling",
            seat="judge",
            effort=self.cfg.reasoning_effort,
            max_tokens=self.cfg.max_output_tokens,
        )


class MockJudge:
    """Applies the Initial Set mechanically: majority (or unanimity) votes, the die roll,
    defeat penalties and Suber's 10-point bonus for dissenters. Ignores amended rule texts."""

    async def draft(
        self, state: GameState, feedback: list[str] | None
    ) -> tuple[RulingAction, list[CallRecord]]:
        cur = state.current_turn
        assert cur is not None and cur.proposal is not None
        prop = cur.proposal
        suber = state.config.get("ruleset") == "suber"
        yes = sum(1 for v in cur.votes.values() if v["vote"] == "yes")
        k = len(state.players)
        unanimity_phase = suber and cur.round <= 2
        unanimous_needed = unanimity_phase or prop["op"] == "transmute"
        changes = default_changes(prop, prop["number"])
        probe = RulingAction(
            legal=True,
            passed=True,
            rule_changes=changes,
            score_deltas=[],
            winner=None,
            rationale="",
        )
        legal = not ruling_errors(state, probe)
        passed = legal and (yes == k if unanimous_needed else 2 * yes > k)
        deltas = [ScoreDelta(player=cur.player, delta=cur.dice or 0, reason="die roll")]
        if not passed:
            deltas.append(
                ScoreDelta(player=cur.player, delta=-(10 if suber else 5), reason="defeated")
            )
        elif suber and not unanimity_phase:
            for name, v in cur.votes.items():
                if v["vote"] == "no":
                    deltas.append(ScoreDelta(player=name, delta=10, reason="voted against winner"))
        target = state.win_points
        for r in state.rules.values():
            m = re.search(r"first player to reach (\d+)", r.text)
            if m:
                target = int(m.group(1))
        totals = {n: p.score for n, p in state.players.items()}
        for d in deltas:
            totals[d.player] += d.delta
        winner = next((n for n in state.seat_order if totals[n] >= target), None)
        return (
            RulingAction(
                legal=legal,
                passed=passed,
                rule_changes=changes if passed else [],
                score_deltas=deltas,
                winner=winner,
                rationale=f"{yes}/{k} in favour; "
                + ("unanimity required. " if unanimous_needed else "majority required. ")
                + ("Adopted." if passed else "Not adopted."),
            ),
            [],
        )


class Judge:
    def __init__(self, cfg: GameConfig, client: OpenRouterClient, inbox: HumanInbox | None) -> None:
        self.cfg = cfg
        self.inbox = inbox
        self.set_model(cfg.judge.model, client)

    def set_model(self, model: str | None, client: OpenRouterClient) -> None:
        self.drafter: LLMJudge | MockJudge | None
        if model is None:
            self.drafter = None
        elif model.startswith("mock/"):
            self.drafter = MockJudge()
        else:
            self.drafter = LLMJudge(client, model, self.cfg)
        self.kind = "mock" if isinstance(self.drafter, MockJudge) else "llm"

    async def rule(self, state: GameState, feedback: list[str] | None = None) -> JudgeResult:
        mode = self.cfg.judge.mode
        draft: RulingAction | None = None
        calls: list[CallRecord] = []
        if self.drafter is not None:
            draft, calls = await self.drafter.draft(state, feedback)
        cur = state.current_turn
        assert cur is not None
        disputed = any(v.get("dispute") for v in cur.votes.values())
        wants_human = mode in ("human", "llm_with_human_override") or (
            mode == "human_on_appeal" and disputed
        )
        if wants_human and self.inbox is not None:
            timeout = self.cfg.judge.review_seconds if mode == "llm_with_human_override" else None
            answer = await self.inbox.request(
                "ruling",
                "judge",
                {
                    "draft": draft.model_dump() if draft else None,
                    "feedback": feedback,
                    "proposal": cur.proposal,
                    "votes": cur.votes,
                    "dice": cur.dice,
                    "active": cur.player,
                },
                timeout=timeout if draft is not None else None,
            )
            if answer is not None:
                return JudgeResult(RulingAction.model_validate(answer), "human", calls)
        if draft is None:
            raise RuntimeError("no judge model configured and no human judge available")
        return JudgeResult(draft, self.kind, calls)
