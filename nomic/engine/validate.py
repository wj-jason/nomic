"""Structural checks on judge rulings before the engine applies them.

The judge decides *what* the rules mean; this only rejects rulings the engine cannot apply
(unknown players, amending a rule that doesn't exist, editing an immutable rule, ...).
"""

from __future__ import annotations

from nomic.engine.actions import ProposalAction, RuleChange, RulingAction
from nomic.engine.state import GameState, RuleChangeError, apply_rule_change


def ruling_errors(state: GameState, ruling: RulingAction) -> list[str]:
    errors: list[str] = []
    for d in ruling.score_deltas:
        if d.player not in state.players:
            errors.append(
                f"score_deltas: unknown player {d.player!r} (players: {list(state.players)})"
            )
    if ruling.winner is not None and ruling.winner not in state.players:
        errors.append(f"winner: unknown player {ruling.winner!r}")
    if ruling.rule_changes and not (ruling.legal and ruling.passed):
        errors.append("rule_changes must be empty unless the proposal is legal and passed")
    rules = state.clone_rules()
    for i, ch in enumerate(ruling.rule_changes):
        try:
            apply_rule_change(rules, ch.model_dump())
        except RuleChangeError as e:
            errors.append(f"rule_changes[{i}]: {e}")
    return errors


def sanitize_ruling(state: GameState, ruling: RulingAction) -> tuple[RulingAction, list[str]]:
    """Drop the parts of a ruling the engine cannot apply. Used when the judge keeps failing."""
    dropped: list[str] = []
    deltas = [d for d in ruling.score_deltas if d.player in state.players]
    if len(deltas) != len(ruling.score_deltas):
        dropped.append("score deltas for unknown players")
    winner = ruling.winner if ruling.winner in state.players else None
    if winner != ruling.winner:
        dropped.append(f"unknown winner {ruling.winner!r}")
    changes: list[RuleChange] = []
    if ruling.legal and ruling.passed:
        rules = state.clone_rules()
        for ch in ruling.rule_changes:
            try:
                apply_rule_change(rules, ch.model_dump())
                changes.append(ch)
            except RuleChangeError as e:
                dropped.append(f"rule change ({e})")
    fixed = ruling.model_copy(
        update={"score_deltas": deltas, "winner": winner, "rule_changes": changes}
    )
    return fixed, dropped


def default_changes(proposal: dict[str, object], number: int) -> list[RuleChange]:
    """The rule change a passed proposal implies when the judge doesn't spell it out."""
    p = ProposalAction.model_validate(proposal)
    if p.op == "enact":
        return [RuleChange(op="enact", number=number, text=p.text, mutable=True)]
    if p.target_rule is None:
        return []
    return [RuleChange(op=p.op, number=p.target_rule, text=p.text)]
