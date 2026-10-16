from __future__ import annotations

import pytest

from nomic.engine.actions import RuleChange, RulingAction, ScoreDelta
from nomic.engine.events import Event
from nomic.engine.state import GameState, RuleChangeError, apply_rule_change
from nomic.engine.validate import default_changes, ruling_errors, sanitize_ruling
from nomic.rules import load_ruleset


def make_state(ruleset: str = "lite") -> GameState:
    rules, win = load_ruleset(ruleset)
    ev = Event(
        seq=1,
        type="game_created",
        actor="system",
        data={
            "config": {"ruleset": ruleset, "budget_usd": 0.5},
            "rules": rules,
            "players": [{"name": "A", "model": "x"}, {"name": "B", "model": None}],
            "win_points": win,
        },
    )
    return GameState.from_events([ev])


def test_rulesets_load_with_win_points() -> None:
    lite, win = load_ruleset("lite")
    assert win == 40 and len(lite) == 14
    assert any("40 points" in r["text"] for r in lite)
    suber, win = load_ruleset("suber", win_points=60)
    assert win == 60 and len(suber) == 29
    assert sum(not r["mutable"] for r in suber) == 16
    assert any("60 (positive) points" in r["text"] for r in suber)


def test_rule_change_ops() -> None:
    st = make_state()
    rules = st.clone_rules()
    apply_rule_change(rules, {"op": "enact", "number": 301, "text": "New."})
    assert rules[301].mutable and rules[301].text == "New."
    apply_rule_change(rules, {"op": "amend", "number": 301, "text": "Newer."})
    assert rules[301].text == "Newer." and len(rules[301].history) == 2
    apply_rule_change(rules, {"op": "transmute", "number": 301})
    assert not rules[301].mutable
    with pytest.raises(RuleChangeError):
        apply_rule_change(rules, {"op": "amend", "number": 301, "text": "x"})
    apply_rule_change(rules, {"op": "transmute", "number": 101, "mutable": True})
    apply_rule_change(rules, {"op": "repeal", "number": 101})
    assert 101 not in rules
    apply_rule_change(rules, {"op": "amend", "number": 205, "text": "Lose 3.", "new_number": 302})
    assert 205 not in rules and rules[302].text == "Lose 3."
    with pytest.raises(RuleChangeError):
        apply_rule_change(rules, {"op": "enact", "number": 302, "text": "dup"})
    # human edits bypass immutability
    apply_rule_change(rules, {"op": "set", "number": 102, "text": "Edited."}, force=True)
    assert rules[102].text == "Edited."


def test_fold_scores_costs_and_turns() -> None:
    st = make_state()
    evs = [
        ("turn_started", {"turn": 1, "round": 1, "player": "A"}),
        (
            "proposal",
            {
                "number": 301,
                "player": "A",
                "op": "enact",
                "target_rule": None,
                "text": "Hi.",
                "summary": "s",
            },
        ),
        ("vote", {"player": "A", "vote": "yes", "reason": "r"}),
        ("dice_roll", {"player": "A", "value": 4}),
        ("llm_usage", {"seat": "A", "cost": 0.5}),
        ("llm_usage", {"seat": "judge", "cost": 0.25}),
        ("rule_changed", {"op": "enact", "number": 301, "text": "Hi.", "source": "proposal 301"}),
        ("score_changed", {"player": "A", "delta": 4, "reason": "die"}),
        ("turn_ended", {"turn": 1}),
    ]
    for i, (t, d) in enumerate(evs, start=2):
        st.apply(Event(seq=i, type=t, actor="x", data=d))
    assert st.players["A"].score == 4 and st.next_number == 302 and 301 in st.rules
    assert st.total_cost == 0.75 and st.judge_cost == 0.25 and st.over_budget
    assert st.next_player() == "B" and st.current_turn is None
    assert st.finished_turns[0].rule_changes[0]["number"] == 301


def test_aborted_turn_is_replayed_by_same_player() -> None:
    st = make_state()
    st.apply(
        Event(seq=2, type="turn_started", actor="s", data={"turn": 1, "round": 1, "player": "A"})
    )
    st.apply(Event(seq=3, type="turn_aborted", actor="s", data={"turn": 1, "reason": "x"}))
    assert st.current_turn is None and st.next_player() == "A" and st.completed_turns == 0


def test_ruling_validation_and_sanitize() -> None:
    st = make_state()
    bad = RulingAction(
        legal=True,
        passed=True,
        rule_changes=[RuleChange(op="amend", number=101, text="no")],
        score_deltas=[ScoreDelta(player="Zed", delta=1, reason="?")],
        winner="Zed",
        rationale="",
    )
    errors = ruling_errors(st, bad)
    assert len(errors) == 3
    fixed, dropped = sanitize_ruling(st, bad)
    assert not ruling_errors(st, fixed) and len(dropped) == 3
    not_passed = bad.model_copy(
        update={
            "passed": False,
            "rule_changes": [RuleChange(op="enact", number=301, text="x")],
            "score_deltas": [],
            "winner": None,
        }
    )
    assert ruling_errors(st, not_passed) == [
        "rule_changes must be empty unless the proposal is legal and passed"
    ]


def test_default_changes() -> None:
    enact = default_changes({"op": "enact", "target_rule": None, "text": "T", "summary": ""}, 305)
    assert enact[0].number == 305 and enact[0].mutable
    amend = default_changes({"op": "amend", "target_rule": 203, "text": "T", "summary": ""}, 305)
    assert amend[0].number == 203
