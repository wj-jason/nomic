"""Turn a game's event log into an ordered list of spoken lines.

Only the people at the table speak, each in its own voice: players in the first person
(proposals, comments, rebuttals, votes) and the judge delivering its rulings. There is no
narrator; turns become chapters instead. Private thinking and post-game interviews are opt-in.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any

from pydantic import BaseModel

from nomic.engine.events import Event
from nomic.voice.voices import ASIDE_GAIN_DB, ASIDE_SPEED, JUDGE, cast

WORDS_PER_MINUTE = 155


class VoiceOptions(BaseModel):
    thinking: bool = False  # each player's private thinking, as a quieter aside (opt-in)
    votes: bool = True  # "I vote yes. <reason>"
    rulings: bool = True  # the judge's ruling at the end of each turn
    rule_text: bool = False  # read the full proposed rule text, not just the pitch
    interviews: bool = False  # post-game interviews (opt-in)


@dataclass
class Line:
    key: str  # stable id: "<event seq>:<part>"
    event_seq: int  # the feed item this line belongs to
    chapter: str  # "Turn 3" or "Interviews"
    speaker: str  # player name or "judge"
    kind: str  # proposal|comment|rebuttal|vote|ruling|aside|interview
    text: str
    voice: str = ""
    speed: float = 1.0
    gain_db: float = 0.0

    @property
    def cache_key(self) -> str:
        raw = f"{self.voice}|{self.speed}|{self.gain_db}|{self.text}"
        return hashlib.sha1(raw.encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Escaped so the dashes are unambiguous in the source (ruff RUF001).
_SPOKEN_SYMBOLS = {
    "\u2192": " to ",  # → arrow
    "&": " and ",
    "\u2014": ", ",  # em dash
    "\u2013": " to ",  # en dash, as in ranges "3-5"
}


def speakable(text: str) -> str:
    """Normalise text for speech: numbers, symbols and Markdown."""
    t = text or ""
    t = re.sub(r"(?i)\b(proposals?|rules?)\s+#(\d+)", r"\1 \2", t)  # "Proposal #301"
    t = re.sub(r"#(\d+)", r"proposal \1", t)  # a bare "#301"
    t = re.sub(r"(?<![\w)])\+(\d+)", r"plus \1", t)
    t = re.sub(r"(?<![\w)])-(\d+)", r"minus \1", t)
    t = re.sub(r"(\d+)\s*%", r"\1 percent", t)
    for symbol, spoken in _SPOKEN_SYMBOLS.items():
        t = t.replace(symbol, spoken)
    t = re.sub(r"[*_`~|<>\[\]{}]", "", t)
    return " ".join(t.split())


def _proposal_speech(d: dict[str, Any], rule_text: bool) -> str:
    target = d.get("target_rule")
    opening = {
        "enact": "I propose a new rule.",
        "amend": f"I propose amending rule {target}.",
        "repeal": f"I propose repealing rule {target}.",
        "transmute": f"I propose transmuting rule {target}.",
    }.get(d["op"], "I have a proposal.")
    text = f"{opening} {d.get('summary', '')}"
    if rule_text and d.get("text"):
        text += f" It would read: {d['text']}"
    return text


def build_script(
    seat_order: list[str],
    events: list[Event],
    opts: VoiceOptions,
    overrides: dict[str, str] | None = None,
) -> tuple[list[Line], dict[str, str]]:
    """Lines for every *completed* turn (aborted or in-progress turns are skipped), then the
    interviews if requested. ``overrides`` are per-game voice choices. Returns
    (lines, casting)."""
    voices = cast(seat_order, overrides)
    lines: list[Line] = []
    turn_lines: list[Line] = []
    chapter = ""

    def say(seq: int, part: str, speaker: str, kind: str, text: str) -> None:
        text = speakable(text)
        if text and speaker in voices:
            turn_lines.append(
                Line(f"{seq}:{part}", seq, chapter, speaker, kind, text, voices[speaker])
            )

    def aside(seq: int, speaker: str, thinking: str | None) -> None:
        if opts.thinking and thinking:
            say(seq, "aside", speaker, "aside", thinking)
            if turn_lines and turn_lines[-1].kind == "aside":
                turn_lines[-1].speed, turn_lines[-1].gain_db = ASIDE_SPEED, ASIDE_GAIN_DB

    for ev in events:
        d, seq = ev.data, ev.seq
        t = ev.type
        if t == "turn_started":
            turn_lines = []
            chapter = f"Turn {d['turn']}"
        elif t == "proposal":
            say(seq, "main", d["player"], "proposal", _proposal_speech(d, opts.rule_text))
            aside(seq, d["player"], d.get("thinking"))
        elif t == "message" and d.get("kind") in ("discussion", "rebuttal"):
            kind = "rebuttal" if d["kind"] == "rebuttal" else "comment"
            say(seq, "main", d["player"], kind, d["text"])
            aside(seq, d["player"], d.get("thinking"))
        elif t == "vote" and opts.votes and not d.get("auto"):
            say(seq, "main", d["player"], "vote", f"I vote {d['vote']}. {d['reason']}")
            aside(seq, d["player"], d.get("thinking"))
        elif t == "ruling" and opts.rulings:
            verdict = "passes" if d["passed"] else ("fails" if d["legal"] else "is illegal")
            say(seq, "main", JUDGE, "ruling", f"The proposal {verdict}. {d['rationale']}")
            aside(seq, JUDGE, d.get("thinking"))
        elif t == "turn_ended":
            lines += turn_lines
            turn_lines = []
        elif t in ("turn_aborted", "game_over"):
            turn_lines = []
        elif t == "reflection" and opts.interviews:
            turn_lines = []
            chapter = "Interviews"
            say(
                seq,
                "main",
                d["player"],
                "interview",
                f"Looking back. {d['overview']} My key decisions: {d['key_decisions']} "
                f"What I would change: {d['would_change']} On the others: {d['about_others']}",
            )
            lines += turn_lines
            turn_lines = []
    return lines, voices


def estimate(lines: list[Line]) -> dict[str, Any]:
    words = sum(len(line.text.split()) for line in lines)
    minutes = words / WORDS_PER_MINUTE + len(lines) * 0.35 / 60
    return {"lines": len(lines), "words": words, "minutes": round(minutes, 1)}
