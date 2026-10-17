from __future__ import annotations

import asyncio
import json

from nomic.agents.prompts import reflection_messages
from nomic.manager import GameManager
from nomic.store import Store
from nomic.transcript import render_transcript
from tests.conftest import fake_openrouter, mock_config

INTERVIEW = {
    "overview": "Went fine.",
    "key_decisions": "Proposed things.",
    "would_change": "More risk.",
    "about_others": "Gamma played best.",
}


def test_players_are_interviewed_when_the_game_ends() -> None:
    async def play() -> None:
        manager = GameManager(Store(), fake_openrouter()[0])
        cfg = mock_config(
            seats=[
                {"name": "Alpha", "model": "mock/fast"},
                {"name": "Beta", "model": "mock/fast"},
                {"name": "Me", "model": None},
            ],
            max_turns=1,
        )
        r = manager.create(cfg, start=False)
        r.intervene("end_game", {"winner": "Alpha"})
        r.start()
        await asyncio.wait_for(r.task, 10)  # type: ignore[arg-type]
        # model seats only; the human seat is never asked
        assert set(r.state.reflections) == {"Alpha", "Beta"}
        assert r.events[-1].type == "reflection"
        md = render_transcript(r.id, r.state, r.events)
        assert "### Alpha looks back" in md and "**Would change:**" in md
        # asking again does nothing: everyone has answered
        assert await r.reflect() == 0
        await manager.shutdown()

    asyncio.run(play())


def test_no_interviews_when_disabled_or_on_reload(tmp_path: object) -> None:
    db = f"{tmp_path}/g.db"

    async def play() -> str:
        manager = GameManager(Store(db), fake_openrouter()[0])
        r = manager.create(mock_config(reflection=False), start=False)
        r.intervene("end_game", {"winner": None})
        r.start()
        await asyncio.wait_for(r.task, 10)  # type: ignore[arg-type]
        assert r.state.reflections == {}
        await manager.shutdown()
        return r.id

    gid = asyncio.run(play())

    async def reload() -> None:
        manager = GameManager(Store(db), fake_openrouter()[0])
        manager.load_all()
        r = manager.runners[gid]
        await asyncio.wait_for(r.task, 10)  # type: ignore[arg-type]
        assert r.state.reflections == {}  # loading a finished game never spends money
        r.start_reflection()  # but you can ask for them explicitly
        await asyncio.wait_for(r.reflect_task, 10)  # type: ignore[arg-type]
        assert set(r.state.reflections) == {"Alpha", "Beta", "Gamma"}
        await manager.shutdown()

    asyncio.run(reload())


def test_interview_prompt_has_own_thinking_but_not_others() -> None:
    async def play() -> None:
        def override(body: dict) -> dict | None:  # type: ignore[type-arg]
            fmt = body.get("response_format", {}).get("json_schema", {}).get("name")
            if fmt == "ReflectionAction":
                return {
                    "choices": [{"message": {"content": json.dumps(INTERVIEW)}}],
                    "usage": {"cost": 0.001},
                }
            return None

        client, seen = fake_openrouter(override=override)
        manager = GameManager(Store(), client)
        cfg = mock_config(
            seats=[{"name": n, "model": "test/model"} for n in ("Alpha", "Beta", "Gamma")],
            judge={"mode": "llm", "model": "mock/judge"},
            max_turns=1,
        )
        r = manager.create(cfg)
        await asyncio.wait_for(r.task, 10)  # type: ignore[arg-type]
        assert r.state.reflections["Beta"]["about_others"] == "Gamma played best."

        msgs = json.dumps(reflection_messages(r.state, "Beta"))
        # Beta's own notes (commenting + voting) are included; the record is public only
        assert "SECRET-discuss plan." in msgs and "SECRET-vote plan." in msgs
        assert "SECRET-propose plan." not in msgs  # Alpha's proposal thinking
        assert "Be nice." in msgs  # the public proposal text
        await manager.shutdown()

    asyncio.run(play())
