"""Who sounds like whom. Kokoro voice ids start with a/b (American/British) + f/m."""

from __future__ import annotations

from typing import Any

# Every English voice in hexgrad/Kokoro-82M (checked against the repo's voices/ folder).
ALL_VOICES = [
    "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore", "af_nicole",
    "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael", "am_onyx",
    "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
]  # fmt: skip

# Default casting: seats take these in order, alternating accent and gender so neighbours
# sound different.
SEAT_VOICES = [
    "af_heart",
    "bm_george",
    "am_michael",
    "bf_emma",
    "af_bella",
    "bm_lewis",
    "am_fenrir",
    "bf_isabella",
    "af_nicole",
    "am_puck",
    "bf_alice",
    "am_eric",
]

JUDGE = "judge"  # the judge speaks too (its rulings); seat names can't be "judge"
JUDGE_VOICE = "bm_daniel"  # default: not in SEAT_VOICES, so it never matches a player's default

ASIDE_SPEED = 0.92  # thinking is read a little slower...
ASIDE_GAIN_DB = -8.0  # ...and quieter, like an inner voice

PREVIEW_TEXT = "Hello, everyone. I propose a new rule, and I think you will all want to vote yes."


def describe(voice: str) -> dict[str, Any]:
    return {
        "id": voice,
        "name": voice[3:].title(),
        "accent": "American" if voice[0] == "a" else "British",
        "gender": "female" if voice[1] == "f" else "male",
    }


def cast(seat_order: list[str], overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Voice per seat (default by seat position) plus the judge, replaced by any valid
    per-game choice."""
    voices = {name: SEAT_VOICES[i % len(SEAT_VOICES)] for i, name in enumerate(seat_order)}
    voices[JUDGE] = JUDGE_VOICE
    for name, voice in (overrides or {}).items():
        if name in voices and voice in ALL_VOICES:
            voices[name] = voice
    return voices
