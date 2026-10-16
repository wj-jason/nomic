"""Prompt rendering. Message order is stable prefix first (system, rules, shared game state)
and the per-player task last, so providers can cache everything but the final message."""

from __future__ import annotations

from typing import Any

from nomic.config import GameConfig
from nomic.engine.state import GameState, TurnRecord

PLAYER_SYSTEM = """\
You are a player in a game of Nomic against other players (other AI models, and possibly \
humans). Nomic is a game in which changing the rules is a move: on your turn you propose a \
change to the rules, everyone votes, and points are awarded as the current rules say. The \
rules can change anything, including how proposals pass, how points are earned and how many \
are needed to win.

How this table works:
- The current rules are authoritative. An impartial Judge (not a player) interprets them each \
turn and decides legality, vote outcomes and points.
- The rules are numbered. Immutable rules cannot be amended or repealed unless first \
transmuted to mutable.
- A proposal is one rule-change: "enact" a new rule (it gets the proposal's number), \
"repeal" a rule, "amend" a rule (give its complete new text) or "transmute" a rule \
(flip it between immutable and mutable).
- Messages marked [GM] come from the human game master who runs this table.

Play to win under the current rules. Think about what other players will vote for, build \
coalitions, and watch for proposals that hand others an advantage. Keep messages short. \
Always reply with only a JSON object in the requested format."""

JUDGE_SYSTEM = """\
You are the Judge in a game of Nomic played by AI models. You are impartial and not a player. \
Each turn you receive the current rules, the facts of the turn (the proposal, every vote with \
its reason and any disputes, and the die roll the engine made for the active player) and you \
rule on the outcome strictly according to the rules currently in effect.

Decide:
1. legal: was the proposal a legal move under the current rules (e.g. a single rule-change, \
not amending an immutable rule, within any limits on the number of rules)?
2. passed: was it adopted, applying the current voting rules exactly (thresholds, unanimity \
phases, special cases)? An illegal proposal never passes.
3. rule_changes: the changes that take effect now, only if legal and passed. Normally this is \
the proposal itself. For "enact" use the proposal number as the new rule number unless the \
rules say otherwise. If the rules renumber an amended or transmuted rule, set new_number.
4. score_deltas: EVERY point change this turn (die roll if the rules call for one, penalties \
for defeated proposals, bonuses for voters, and any effects of rules just adopted that apply \
now). Use exact player names. Leave out zero changes.
5. winner: the name of a player who has now won under the rules, after applying the deltas; \
otherwise null.
6. rationale: a brief explanation citing rule numbers. Address every dispute raised.

Where the rules are unclear, inconsistent or silent, follow the spirit of the game and game \
custom (your earlier rulings). Reply with only a JSON object in the requested format."""

TASKS = {
    "propose": """\
It is YOUR turn, {name}. Propose exactly one rule-change (proposal #{number}).
Reply with JSON: {{"op": "enact"|"repeal"|"amend"|"transmute", "target_rule": <rule number or \
null for enact>, "text": <complete new rule text for enact/amend, else null>, \
"summary": <one or two sentences pitching it>}}""",
    "discuss": """\
You are {name}. Proposal #{number} by {proposer} is on the table (see above). Before the vote, \
say something to the table: argue for or against, suggest a deal, or warn others.
Reply with JSON: {{"message": <under 120 words>}}""",
    "vote": """\
You are {name}. Vote on proposal #{number} by {proposer} (see above).
Reply with JSON: {{"vote": "yes"|"no", "reason": <one sentence>, "dispute": <why you believe \
this proposal or turn is illegal, or null>}}""",
}


def render_rules(state: GameState) -> str:
    lines = ["CURRENT RULES ([I] = immutable, [M] = mutable):"]
    for n, r in sorted(state.rules.items()):
        lines.append(f"{n} [{'M' if r.mutable else 'I'}] {r.text}")
    return "\n".join(lines)


def render_proposal(p: dict[str, Any]) -> str:
    target = f" rule {p['target_rule']}" if p.get("target_rule") is not None else ""
    out = f"#{p['number']} by {p['player']}: {p['op'].upper()}{target}"
    if p.get("text"):
        out += f'\n  Text: "{p["text"]}"'
    out += f"\n  Pitch: {p.get('summary', '')}"
    return out


def digest_turn(t: TurnRecord) -> str:
    """One line per old turn, deterministic and free (no summarizer model needed)."""
    if t.proposal is None:
        return f"T{t.turn} {t.player}: no proposal"
    p = t.proposal
    target = f" {p['target_rule']}" if p.get("target_rule") is not None else ""
    yes = sum(1 for v in t.votes.values() if v["vote"] == "yes")
    outcome = "?"
    if t.ruling:
        outcome = (
            "passed" if t.ruling["passed"] else ("ILLEGAL" if not t.ruling["legal"] else "failed")
        )
    pts = ", ".join(f"{c['player']} {c['delta']:+d}" for c in t.score_changes)
    return (
        f"T{t.turn} {t.player} #{p['number']} {p['op']}{target} ({p.get('summary', '')[:80]}) "
        f"-> {outcome} {yes}-{len(t.votes) - yes}; {pts}"
    )


def render_turn(t: TurnRecord, *, include_votes: bool = True) -> str:
    lines = [f"--- Turn {t.turn} (round {t.round}), active player {t.player} ---"]
    if t.proposal:
        lines.append("Proposal " + render_proposal(t.proposal))
    for m in t.messages:
        lines.append(f"{m['player']} says: {m['text']}")
    if include_votes and t.votes:
        for name, v in t.votes.items():
            dispute = f" DISPUTE: {v['dispute']}" if v.get("dispute") else ""
            lines.append(f"{name} votes {v['vote'].upper()}: {v['reason']}{dispute}")
    if t.dice is not None:
        lines.append(f"Die roll: {t.dice}")
    if t.ruling:
        r = t.ruling
        verdict = "PASSED" if r["passed"] else ("ILLEGAL" if not r["legal"] else "FAILED")
        lines.append(f"Judge ({r.get('by', 'llm')}): {verdict}. {r['rationale']}")
    for c in t.score_changes:
        lines.append(f"Points: {c['player']} {c['delta']:+d} ({c['reason']})")
    return "\n".join(lines)


def render_state(state: GameState, cfg: GameConfig) -> str:
    done = state.finished_turns
    recent = done[-cfg.history_turns :] if cfg.history_turns else []
    older = done[: len(done) - len(recent)]
    lines = [
        f"GAME STATE: {state.completed_turns} turns completed. Seating order: "
        f"{', '.join(state.seat_order)}.",
        "Scores: " + ", ".join(f"{n} {state.players[n].score}" for n in state.seat_order),
    ]
    if older:
        lines.append("\nEarlier turns (digest):")
        lines += [digest_turn(t) for t in older]
    if recent:
        lines.append("\nRecent turns:")
        lines += [render_turn(t) for t in recent]
    if state.gm_log:
        lines.append("\nGame master notes:")
        lines += [f"[GM] (after turn {g['turn']}) {g['text']}" for g in state.gm_log[-10:]]
    cur = state.current_turn
    if cur is not None:
        lines.append("\nCURRENT TURN:")
        lines.append(render_turn(cur, include_votes=False))
    return "\n".join(lines)


def player_messages(
    state: GameState, player: str, kind: str, cfg: GameConfig
) -> list[dict[str, Any]]:
    cur = state.current_turn
    prop = cur.proposal if cur else None
    task = TASKS[kind].format(
        name=player,
        number=prop["number"] if prop else state.next_number,
        proposer=prop["player"] if prop else player,
    )
    if kind == "vote" and cur is not None and cur.player == player:
        task += "\n(This is your own proposal.)"
    return [
        {"role": "system", "content": PLAYER_SYSTEM},
        {"role": "user", "content": render_rules(state), "cache": True},
        {"role": "user", "content": render_state(state, cfg), "cache": True},
        {"role": "user", "content": f"Your score: {state.players[player].score}.\n{task}"},
    ]


def judge_messages(
    state: GameState, cfg: GameConfig, feedback: list[str] | None = None
) -> list[dict[str, Any]]:
    cur = state.current_turn
    assert cur is not None and cur.proposal is not None
    rulings = [t for t in state.finished_turns if t.ruling][-5:]
    precedent = "\n".join(
        f"T{t.turn}: {'passed' if t.ruling and t.ruling['passed'] else 'not passed'} - "
        f"{(t.ruling or {}).get('rationale', '')[:300]}"
        for t in rulings
    )
    facts = [
        render_turn(cur),
        f"\nVotes: {sum(1 for v in cur.votes.values() if v['vote'] == 'yes')} yes, "
        f"{sum(1 for v in cur.votes.values() if v['vote'] == 'no')} no, of "
        f"{len(state.players)} players.",
        f"Turn {cur.turn}, round {cur.round}; {state.completed_turns} turns completed before it.",
        f"The engine rolled a six-sided die for {cur.player}: {cur.dice}.",
        "Scores before this turn's changes: "
        + ", ".join(f"{n} {state.players[n].score}" for n in state.seat_order),
    ]
    if precedent:
        facts.append("\nYour recent rulings (precedent):\n" + precedent)
    if feedback:
        facts.append(
            "\nYour previous ruling could not be applied by the engine:\n- "
            + "\n- ".join(feedback)
            + "\nFix these problems."
        )
    facts.append("\nRule on this turn now. Reply with the JSON ruling.")
    return [
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user", "content": render_rules(state), "cache": True},
        {"role": "user", "content": render_state(state, cfg)},
        {"role": "user", "content": "\n".join(facts)},
    ]
