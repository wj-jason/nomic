"""A readable Markdown transcript of a game, including every model's private thinking."""

from __future__ import annotations

import time
from typing import Any

from nomic.engine.events import Event
from nomic.engine.state import GameState


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in text.splitlines())


def _thinking(d: dict[str, Any]) -> str:
    return f"\n  - 💭 *{d['thinking']}*" if d.get("thinking") else ""


def _event_lines(ev: Event) -> list[str]:
    d = ev.data
    t = ev.type
    if t == "turn_started":
        return ["", f"## Turn {d['turn']} · round {d['round']} · {d['player']}", ""]
    if t == "proposal":
        target = f" rule {d['target_rule']}" if d.get("target_rule") is not None else ""
        out = [f"**Proposal #{d['number']}** by {d['player']}: {d['op'].upper()}{target}"]
        if d.get("text"):
            out.append(_quote(d["text"]))
        out.append(f"- Pitch: {d.get('summary', '')}{_thinking(d)}")
        out.append("")
        return out
    if t == "message":
        if d.get("kind") == "gm":
            return [f"- **[GM]** {d['text']}"]
        label = "replies" if d.get("kind") == "rebuttal" else "says"
        return [f"- **{d['player']}** {label}: {d['text']}{_thinking(d)}"]
    if t == "vote":
        auto = " *(automatic)*" if d.get("auto") else ""
        dispute = f" **Dispute:** {d['dispute']}" if d.get("dispute") else ""
        return [
            f"- Vote **{d['player']}**: {d['vote'].upper()}{auto} — {d['reason']}{dispute}"
            f"{_thinking(d)}"
        ]
    if t == "dice_roll":
        return [f"- 🎲 {d['player']} rolled {d['value']}"]
    if t == "ruling":
        verdict = "PASSED" if d["passed"] else ("FAILED" if d["legal"] else "ILLEGAL")
        return [f"- ⚖ **Judge ({d.get('by', 'llm')}): {verdict}.** {d['rationale']}{_thinking(d)}"]
    if t == "rule_changed":
        who = "human edit: " if d.get("source") == "human" else ""
        renum = f" → {d['new_number']}" if d.get("new_number") else ""
        return [f"- Rule change ({who}{d['op']} {d['number']}{renum})"]
    if t == "score_changed":
        who = " (human edit)" if d.get("source") == "human" else ""
        return [f"- Points: {d['player']} {d['delta']:+d} ({d['reason']}){who}"]
    if t == "status":
        reason = f": {d['reason']}" if d.get("reason") else ""
        return [f"- *[{d['status']}{reason}]*"]
    if t == "turn_aborted":
        return [f"- *[turn aborted: {d.get('reason', '')}]*"]
    if t == "model_swapped":
        return [f"- *[human: {d['player']} is now {d.get('model') or 'human'}]*"]
    if t == "budget_changed":
        return [f"- *[human: budget set to ${d['budget_usd']:.2f}]*"]
    if t == "game_over":
        winner = d.get("winner") or "no winner"
        return ["", f"## Game over: {winner} ({d.get('reason', '')})"]
    if t == "reflection":
        return [
            "",
            f"### {d['player']} looks back",
            "",
            f"- **How it went:** {d['overview']}",
            f"- **Key decisions:** {d['key_decisions']}",
            f"- **Would change:** {d['would_change']}",
            f"- **On the others:** {d['about_others']}",
        ]
    return []  # game_created, turn_ended, llm_usage


def render_transcript(
    game_id: str, state: GameState, events: list[Event], audio: str | None = None
) -> str:
    cfg = state.config
    created = time.strftime("%Y-%m-%d %H:%M", time.localtime(events[0].ts)) if events else ""
    lines = [
        f"# Nomic game {game_id}",
        "",
        f"- Started: {created}",
        f"- Ruleset: {cfg.get('ruleset')} · first to {state.win_points} points",
        "- Players: "
        + ", ".join(f"{n} ({state.players[n].model or 'human'})" for n in state.seat_order),
        f"- Judge: {cfg.get('judge', {}).get('mode')} · {cfg.get('judge', {}).get('model')}",
        f"- Status: {state.status}"
        + (f" · winner **{state.winner}**" if state.winner else "")
        + f" · {state.completed_turns} turns",
        "- Final scores: " + ", ".join(f"{n} {state.players[n].score}" for n in state.seat_order),
        f"- Cost: ${state.total_cost:.4f} (judge ${state.judge_cost:.4f}; "
        + ", ".join(f"{n} ${state.players[n].cost:.4f}" for n in state.seat_order)
        + ")",
        *([f"- Voiceover: {audio}"] if audio else []),
        "",
        "💭 lines are each model's private thinking, which no other player or the judge saw.",
    ]
    for ev in events:
        lines += _event_lines(ev)
    lines += ["", "## Rules at the end", ""]
    for n, r in sorted(state.rules.items()):
        lines.append(f"- **{n}** [{'M' if r.mutable else 'I'}] {r.text}")
    return "\n".join(lines) + "\n"
