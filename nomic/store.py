"""SQLite persistence: games, their event logs, and every LLM call (for debugging and cost)."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from nomic.engine.events import Event
from nomic.llm.openrouter import CallRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS games (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    config TEXT NOT NULL,
    parent_id TEXT,
    fork_seq INTEGER
);
CREATE TABLE IF NOT EXISTS events (
    game_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    actor TEXT NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (game_id, seq)
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id TEXT NOT NULL,
    ts REAL NOT NULL,
    seat TEXT NOT NULL,
    purpose TEXT NOT NULL,
    model TEXT NOT NULL,
    request TEXT NOT NULL,
    response TEXT NOT NULL,
    prompt_tokens INTEGER,
    completion_tokens INTEGER,
    cached_tokens INTEGER,
    reasoning_tokens INTEGER,
    cost REAL,
    latency_ms INTEGER,
    error TEXT,
    generation_id TEXT,
    extra TEXT
);
CREATE INDEX IF NOT EXISTS llm_calls_game ON llm_calls (game_id);
CREATE TABLE IF NOT EXISTS game_settings (
    game_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (game_id, key)
);
"""


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # games
    def create_game(
        self,
        game_id: str,
        config: dict[str, Any],
        parent_id: str | None = None,
        fork_seq: int | None = None,
    ) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO games (id, created_at, config, parent_id, fork_seq) VALUES (?,?,?,?,?)",
                (game_id, time.time(), json.dumps(config), parent_id, fork_seq),
            )

    def list_games(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM games ORDER BY created_at DESC").fetchall()
        return [{**dict(r), "config": json.loads(r["config"])} for r in rows]

    def delete_game(self, game_id: str) -> None:
        with self.db:
            for table, col in (
                ("events", "game_id"),
                ("llm_calls", "game_id"),
                ("game_settings", "game_id"),
                ("games", "id"),
            ):
                self.db.execute(f"DELETE FROM {table} WHERE {col} = ?", (game_id,))

    # per-game settings that aren't part of the game itself (e.g. voiceover casting)
    def get_setting(self, game_id: str, key: str, default: Any = None) -> Any:
        row = self.db.execute(
            "SELECT value FROM game_settings WHERE game_id = ? AND key = ?", (game_id, key)
        ).fetchone()
        return json.loads(row["value"]) if row else default

    def set_setting(self, game_id: str, key: str, value: Any) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO game_settings (game_id, key, value) VALUES (?,?,?) "
                "ON CONFLICT (game_id, key) DO UPDATE SET value = excluded.value",
                (game_id, key, json.dumps(value)),
            )

    # events
    def append_event(self, game_id: str, ev: Event) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO events (game_id, seq, ts, type, actor, data) VALUES (?,?,?,?,?,?)",
                (game_id, ev.seq, ev.ts, ev.type, ev.actor, json.dumps(ev.data)),
            )

    def events(self, game_id: str) -> list[Event]:
        rows = self.db.execute(
            "SELECT * FROM events WHERE game_id = ? ORDER BY seq", (game_id,)
        ).fetchall()
        return [
            Event(
                seq=r["seq"],
                ts=r["ts"],
                type=r["type"],
                actor=r["actor"],
                data=json.loads(r["data"]),
            )
            for r in rows
        ]

    # llm calls
    def record_call(self, game_id: str, rec: CallRecord) -> int:
        d = asdict(rec)
        with self.db:
            cur = self.db.execute(
                """INSERT INTO llm_calls (game_id, ts, seat, purpose, model, request, response,
                   prompt_tokens, completion_tokens, cached_tokens, reasoning_tokens, cost,
                   latency_ms, error, generation_id, extra)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    game_id,
                    time.time(),
                    d["seat"],
                    d["purpose"],
                    d["model"],
                    json.dumps(d["request"]),
                    d["response_text"],
                    d["prompt_tokens"],
                    d["completion_tokens"],
                    d["cached_tokens"],
                    d["reasoning_tokens"],
                    d["cost"],
                    d["latency_ms"],
                    d["error"],
                    d["generation_id"],
                    json.dumps(d["extra"]),
                ),
            )
        return int(cur.lastrowid or 0)

    def get_call(self, call_id: int) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM llm_calls WHERE id = ?", (call_id,)).fetchone()
        if r is None:
            return None
        out = dict(r)
        out["request"] = json.loads(out["request"])
        out["extra"] = json.loads(out["extra"] or "{}")
        return out

    def call_stats(self, game_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT seat, model, COUNT(*) AS calls, SUM(prompt_tokens) AS prompt_tokens,
                      SUM(completion_tokens) AS completion_tokens,
                      SUM(cached_tokens) AS cached_tokens, SUM(cost) AS cost,
                      AVG(latency_ms) AS avg_latency_ms
               FROM llm_calls WHERE game_id = ? GROUP BY seat, model""",
            (game_id,),
        ).fetchall()
        return [dict(r) for r in rows]
