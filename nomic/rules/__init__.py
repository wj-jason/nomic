"""Initial rulesets, stored as YAML next to this module."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

RULESET_DIR = Path(__file__).parent
RULESETS = ("lite", "suber")


def ruleset_info(name: str) -> dict[str, Any]:
    if name not in RULESETS:
        raise ValueError(f"unknown ruleset {name!r}")
    data: dict[str, Any] = yaml.safe_load((RULESET_DIR / f"{name}.yaml").read_text())
    return data


def load_ruleset(name: str, win_points: int | None = None) -> tuple[list[dict[str, Any]], int]:
    """Return the initial rules (number, text, mutable) and the effective win target."""
    data = ruleset_info(name)
    points = win_points or int(data["win_points"])
    rules = [
        {
            "number": int(r["number"]),
            "mutable": bool(r["mutable"]),
            "text": " ".join(str(r["text"]).split()).replace("{win_points}", str(points)),
        }
        for r in data["rules"]
    ]
    return rules, points
