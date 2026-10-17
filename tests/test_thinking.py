from __future__ import annotations

import asyncio
import json

from nomic.agents.prompts import judge_messages, player_messages
from nomic.engine.actions import (
    ACTION_MODELS,
    THINKING_MAX_CHARS,
    VoteAction,
    clip_thinking,
    strict_schema,
)
from nomic.manager import GameManager
from nomic.store import Store
from nomic.transcript import render_transcript
from tests.conftest import fake_openrouter, mock_config


def test_clip_keeps_three_sentences_and_caps_length() -> None:
    text = "One. Two!  Three?\nFour. Five."
    assert clip_thinking(text) == "One. Two! Three?"
    assert len(clip_thinking("x" * 2000)) == THINKING_MAX_CHARS
    vote = VoteAction.model_validate(
        {"thinking": "A. B. C. D.", "vote": "no", "reason": "r", "dispute": None}
    )
    assert vote.thinking == "A. B. C."


def test_thinking_is_first_and_required_in_every_schema() -> None:
    for kind, model in ACTION_MODELS.items():
        if kind == "reflect":  # the post-game interview is all public commentary
            continue
        s = strict_schema(model)
        assert next(iter(s["properties"])) == "thinking"
        assert "thinking" in s["required"]
    # humans may omit it
    assert VoteAction.model_validate({"vote": "yes", "reason": "r"}).thinking == ""


def test_thinking_is_stored_but_never_shown_to_other_models() -> None:
    async def play() -> None:
        client, seen = fake_openrouter()
        manager = GameManager(Store(), client)
        cfg = mock_config(
            seats=[{"name": n, "model": "test/model"} for n in ("Alpha", "Beta", "Gamma")],
            judge={"mode": "llm", "model": "test/judge"},
            max_turns=2,
        )
        r = manager.create(cfg)
        await asyncio.wait_for(r.task, 10)  # type: ignore[arg-type]

        by_type = {e.type: e.data for e in r.events}
        for t in ("proposal", "vote", "ruling"):
            assert by_type[t]["thinking"].startswith("SECRET-")
        discussion = [e.data for e in r.events if e.type == "message"]
        assert discussion and all(d["thinking"] == "SECRET-discuss plan." for d in discussion)

        # Every prompt sent after turn 1 included turn 1's history, yet no thinking leaked.
        # (The post-game interview is the one prompt that carries thinking: the player's own.)
        moves = [
            b for b in seen if b["response_format"]["json_schema"]["name"] != "ReflectionAction"
        ]
        assert len(moves) > 8
        for body in moves:
            assert "SECRET-" not in json.dumps(body["messages"])
        # And the prompt builders themselves never render it.
        st = r.state
        assert st.finished_turns[0].proposal is not None
        rendered = player_messages(st, "Beta", "propose", cfg)
        assert "SECRET-" not in json.dumps(rendered)
        # ...but the exported transcript does include it
        md = render_transcript(r.id, st, r.events)
        for secret in ("propose", "discuss", "vote", "ruling"):
            assert f"💭 *SECRET-{secret} plan.*" in md
        assert "replies:" in md and "*(automatic)*" in md
        await manager.shutdown()

    asyncio.run(play())


def test_judge_prompt_omits_player_thinking() -> None:
    async def play() -> None:
        client, _ = fake_openrouter()
        manager = GameManager(Store(), client)
        cfg = mock_config(judge={"mode": "llm", "model": "mock/judge"}, max_turns=1)
        r = manager.create(cfg, start=False)
        r.start()
        await asyncio.wait_for(r.task, 10)  # type: ignore[arg-type]
        turn = r.state.finished_turns[0]
        assert turn.proposal is not None and turn.proposal["thinking"]
        # rebuild the judge's view of that turn
        r.state.turns[-1].ended = False
        msgs = judge_messages(r.state, cfg)
        r.state.turns[-1].ended = True
        assert turn.proposal["thinking"] not in json.dumps(msgs)
        for v in turn.votes.values():
            if v["thinking"]:  # the proposer's automatic vote has none
                assert v["thinking"] not in json.dumps(msgs)
        await manager.shutdown()

    asyncio.run(play())
