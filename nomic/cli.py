"""Command line: `nomic serve`, `nomic run CONFIG`, `nomic estimate CONFIG`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

from nomic.config import GameConfig, load_config

DEFAULT_DB = "data/nomic.db"


def load_dotenv(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def format_event(ev: dict[str, Any]) -> str | None:
    t, d = ev["type"], ev["data"]
    if t == "turn_started":
        return f"\n=== Turn {d['turn']} (round {d['round']}): {d['player']} ==="
    if t == "proposal":
        target = f" {d['target_rule']}" if d.get("target_rule") is not None else ""
        text = f'\n    "{d["text"]}"' if d.get("text") else ""
        return f"  #{d['number']} {d['op'].upper()}{target}: {d['summary']}{text}"
    if t == "message" and d.get("kind") == "discussion":
        return f"  {d['player']}: {d['text']}"
    if t == "message":
        return f"  [GM] {d['text']}"
    if t == "vote":
        dispute = f"  DISPUTE: {d['dispute']}" if d.get("dispute") else ""
        return f"  vote {d['player']}: {d['vote']} ({d['reason']}){dispute}"
    if t == "dice_roll":
        return f"  die: {d['value']}"
    if t == "ruling":
        verdict = "PASSED" if d["passed"] else ("ILLEGAL" if not d["legal"] else "FAILED")
        return f"  judge ({d['by']}): {verdict}. {d['rationale']}"
    if t == "rule_changed":
        return f"  rule {d['op']} {d['number']}"
    if t == "score_changed":
        return f"  points {d['player']} {d['delta']:+d} ({d['reason']})"
    if t == "status":
        return f"  [{d['status']}] {d.get('reason', '')}"
    if t == "game_over":
        return f"\n*** Game over: winner {d.get('winner')} ({d.get('reason')}) ***"
    if t == "turn_aborted":
        return f"  [turn aborted: {d.get('reason')}]"
    return None


async def _run(cfg: GameConfig, db: str, quiet_costs: bool) -> int:
    from nomic.manager import GameManager
    from nomic.store import Store

    if any(s.human for s in cfg.seats) or cfg.judge.mode != "llm":
        print("`nomic run` is headless: use model seats and judge mode 'llm' (or `nomic serve`).")
        return 2
    manager = GameManager(Store(db))
    await manager.refresh_pricing()
    runner = manager.create(cfg, start=False)
    q = runner.subscribe()
    print(
        f"game {runner.id}: {', '.join(f'{s.name}={s.model}' for s in cfg.seats)}; "
        f"judge={cfg.judge.model}"
    )
    runner.start()
    try:
        while True:
            msg = await q.get()
            if msg["type"] != "event":
                continue
            ev = msg["event"]
            line = format_event(ev)
            if line:
                print(line, flush=True)
            if ev["type"] == "llm_usage" and not quiet_costs:
                print(
                    f"    ${ev['data']['cost']:.4f} {ev['data']['seat']}/{ev['data']['purpose']}"
                    f" ({ev['data']['prompt_tokens']} in, {ev['data']['cached_tokens']} cached,"
                    f" {ev['data']['completion_tokens']} out)"
                )
            st = runner.state
            if st.status == "over" or (st.status == "paused" and ev["type"] == "status"):
                break
    finally:
        st = runner.state
        print(f"\nstatus: {st.status} {st.status_reason}".rstrip())
        print("scores: " + ", ".join(f"{n} {st.players[n].score}" for n in st.seat_order))
        print(
            f"cost: ${st.total_cost:.4f} (judge ${st.judge_cost:.4f}); "
            + ", ".join(f"{n} ${st.players[n].cost:.4f}" for n in st.seat_order)
        )
        await manager.shutdown()
    return 0


async def _estimate(cfg: GameConfig) -> None:
    from nomic.llm.pricing import estimate_game_cost, get_pricing

    est = estimate_game_cost(cfg, await get_pricing())
    print(f"~{est['rounds']} rounds, {est['turns']} turns to {est['win_points']} points")
    for name, cost in est["seats"].items():
        print(f"  {name:20s} ${cost:.2f}")
    print(f"  {'judge':20s} ${est['judge']:.2f}")
    print(f"total ~${est['total']:.2f} (range ${est['low']:.2f}-${est['high']:.2f})")
    if est["unknown_models"]:
        print("unknown models (not priced): " + ", ".join(est["unknown_models"]))


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="nomic")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the web UI and API")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--db", default=DEFAULT_DB)
    r = sub.add_parser("run", help="play one game headless in the terminal")
    r.add_argument("config")
    r.add_argument("--db", default=DEFAULT_DB)
    r.add_argument("--quiet-costs", action="store_true")
    e = sub.add_parser("estimate", help="estimate what a config costs per game")
    e.add_argument("config")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if args.cmd == "serve":
        import uvicorn

        from nomic.server.app import create_app

        uvicorn.run(create_app(args.db), host=args.host, port=args.port)
        return 0
    cfg = load_config(args.config)
    if args.cmd == "run":
        return asyncio.run(_run(cfg, args.db, args.quiet_costs))
    asyncio.run(_estimate(cfg))
    return 0


if __name__ == "__main__":
    sys.exit(main())
