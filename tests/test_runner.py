from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nomic.manager import GameManager
from nomic.store import Store
from tests.conftest import fake_openrouter, mock_config


async def _wait(cond: Any, timeout: float = 10.0) -> None:
    async def poll() -> None:
        while not cond():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout)


@pytest.mark.parametrize("ruleset", ["lite", "suber"])
def test_mock_game_reaches_a_winner_deterministically(ruleset: str) -> None:
    async def play() -> tuple[str | None, int, list[str]]:
        manager = GameManager(Store(), fake_openrouter()[0])
        r = manager.create(mock_config(ruleset=ruleset))
        await asyncio.wait_for(r.task, 30)  # type: ignore[arg-type]
        st = r.state
        await manager.shutdown()
        return st.winner, st.completed_turns, [e.type for e in r.events]

    first, second = asyncio.run(play()), asyncio.run(play())
    winner, turns, types = first
    assert first == second
    assert winner is not None and types[-1] == "game_over"
    assert "rule_changed" in types and turns < 150


def test_llm_game_records_costs_and_pauses_at_budget() -> None:
    async def play() -> None:
        client, seen = fake_openrouter(cost=0.01)
        manager = GameManager(Store(), client)
        cfg = mock_config(
            seats=[{"name": n, "model": "test/model"} for n in ("Alpha", "Beta", "Gamma")],
            judge={"mode": "llm", "model": "test/judge"},
            budget_usd=0.115,
        )
        r = manager.create(cfg)
        await _wait(lambda: r.state.status == "paused")
        st = r.state
        assert "budget" in st.status_reason
        # turn 1 = 1 propose + 3 discuss + 3 votes + 1 ruling = $0.08; turn 2 then pauses
        # between phases (propose + discussion) once spend reaches $0.12
        assert st.completed_turns == 1 and st.total_cost == pytest.approx(0.12)
        assert st.phase == "discussion"
        assert st.players["Alpha"].score == 3 and 301 in st.rules
        assert st.judge_cost == pytest.approx(0.01)
        # our cache markers never reach a non-Anthropic provider
        assert all("cache" not in m for body in seen for m in body["messages"])
        calls = manager.store.call_stats(r.id)
        assert sum(c["calls"] for c in calls) == 12
        # raising the budget lets it continue
        r.intervene("set_budget", {"budget_usd": 1.0})
        r.resume()
        await _wait(lambda: r.state.completed_turns >= 2)
        await manager.shutdown()

    asyncio.run(play())


def test_judge_feedback_loop_then_sanitize() -> None:
    async def play() -> None:
        bad = {
            "legal": True,
            "passed": True,
            "rule_changes": [
                {"op": "amend", "number": 101, "text": "x", "mutable": None, "new_number": None}
            ],
            "score_deltas": [],
            "winner": None,
            "rationale": "bad",
        }

        def override(body: dict[str, Any]) -> dict[str, Any] | None:
            if body.get("response_format", {}).get("json_schema", {}).get("name") == "RulingAction":
                import json

                return {
                    "choices": [{"message": {"content": json.dumps(bad)}}],
                    "usage": {"cost": 0.0},
                }
            return None

        client, seen = fake_openrouter(override=override)
        manager = GameManager(Store(), client)
        cfg = mock_config(judge={"mode": "llm", "model": "test/judge"}, max_turns=1)
        r = manager.create(cfg)
        await asyncio.wait_for(r.task, 10)  # type: ignore[arg-type]
        ruling = r.state.finished_turns[0].ruling
        assert ruling is not None and "engine dropped" in ruling["rationale"]
        assert r.state.rules[101].text != "x"
        judge_bodies = [b for b in seen if "response_format" in b]
        assert (
            len(judge_bodies) == 3
            and "could not be applied" in judge_bodies[-1]["messages"][-1]["content"]
        )
        await manager.shutdown()

    asyncio.run(play())


def test_human_seat_and_human_judge() -> None:
    async def play() -> None:
        manager = GameManager(Store(), fake_openrouter()[0])
        cfg = mock_config(
            seats=[{"name": "Me", "model": None}, {"name": "Bot", "model": "mock/fast"}],
            judge={"mode": "human", "model": "mock/judge"},
            discussion=False,
        )
        r = manager.create(cfg)
        await _wait(lambda: r.inbox.list())
        req = r.inbox.list()[0]
        assert req["kind"] == "propose" and req["seat"] == "Me"
        r.inbox.respond(
            req["id"], {"op": "enact", "target_rule": None, "text": "Hats.", "summary": "hats"}
        )
        await _wait(lambda: any(p["kind"] == "vote" for p in r.inbox.list()))
        vote = r.inbox.list()[0]
        r.inbox.respond(vote["id"], {"vote": "yes", "reason": "mine", "dispute": None})
        await _wait(lambda: any(p["kind"] == "ruling" for p in r.inbox.list()))
        ruling_req = r.inbox.list()[0]
        draft = ruling_req["context"]["draft"]
        assert draft is not None and draft["passed"]
        draft["score_deltas"] = [{"player": "Me", "delta": 40, "reason": "judge's whim"}]
        draft["winner"] = "Me"
        r.inbox.respond(ruling_req["id"], draft)
        await asyncio.wait_for(r.task, 5)  # type: ignore[arg-type]
        assert r.state.winner == "Me" and r.state.finished_turns[0].ruling["by"] == "human"  # type: ignore[index]
        await manager.shutdown()

    asyncio.run(play())


def test_interventions_fork_and_reload(tmp_path: Any) -> None:
    db = tmp_path / "g.db"

    async def play() -> tuple[str, str]:
        manager = GameManager(Store(db), fake_openrouter()[0])
        r = manager.create(mock_config(), start=False)
        r.intervene("edit_rule", {"number": 206, "text": "The winner is the first to 10 points."})
        r.intervene("set_score", {"player": "Beta", "score": 7})
        r.intervene("gm_message", {"text": "Play nice."})
        r.intervene("swap_model", {"player": "Gamma", "model": None})
        with pytest.raises(ValueError):
            r.intervene("set_score", {"player": "Nobody", "score": 1})
        st = r.state
        assert st.rules[206].text.endswith("10 points.") and st.players["Beta"].score == 7
        assert st.players["Gamma"].human and st.gm_log[-1]["text"] == "Play nice."
        r.intervene("swap_model", {"player": "Gamma", "model": "mock/fast"})
        r.start()
        await asyncio.wait_for(r.task, 10)  # type: ignore[arg-type]
        assert r.state.status == "over"
        fork = manager.fork(r.id, seq=8)
        assert fork.state.status == "paused" and fork.state.players["Beta"].score == 7
        await manager.shutdown()
        return r.id, fork.id

    gid, fid = asyncio.run(play())

    async def reload() -> None:
        manager = GameManager(Store(db), fake_openrouter()[0])
        manager.load_all()
        assert manager.runners[gid].state.status == "over"
        f = manager.runners[fid]
        assert f.state.status == "paused" and f.state.rules[206].text.endswith("10 points.")
        f.resume()
        await asyncio.wait_for(f.task, 10)  # type: ignore[arg-type]
        assert f.state.winner is not None
        await manager.shutdown()

    asyncio.run(reload())


def test_turn_cap_ends_game() -> None:
    async def play() -> None:
        manager = GameManager(Store(), fake_openrouter()[0])
        r = manager.create(mock_config(max_turns=2))
        await asyncio.wait_for(r.task, 10)  # type: ignore[arg-type]
        assert r.state.completed_turns == 2 and "turn cap" in r.state.end_reason
        await manager.shutdown()

    asyncio.run(play())
