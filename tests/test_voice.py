from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from nomic.engine.events import Event
from nomic.manager import GameManager
from nomic.server.app import create_app
from nomic.store import Store
from nomic.voice.render import VoiceRenderer
from nomic.voice.script import VoiceOptions, build_script, estimate, speakable
from nomic.voice.tts import FakeSynth
from nomic.voice.voices import ALL_VOICES, JUDGE_VOICE, SEAT_VOICES, cast
from tests.conftest import fake_openrouter, mock_config


def finished_game() -> tuple[list[str], list[Event]]:
    """A short mock game, played to the end, with interviews."""

    async def play() -> tuple[list[str], list[Event]]:
        manager = GameManager(Store(), fake_openrouter()[0])
        r = manager.create(mock_config(max_turns=3))
        await asyncio.wait_for(r.task, 10)  # type: ignore[arg-type]
        await manager.shutdown()
        return r.state.seat_order, r.events

    return asyncio.run(play())


def test_speakable() -> None:
    assert speakable("Vote for #301, +3 points & 50% more → win") == (
        "Vote for proposal 301, plus 3 points and 50 percent more to win"
    )
    assert speakable("**bold** `code` rule 7-3") == "bold code rule 7-3"
    assert speakable("I support Proposal #301 and rule #206") == (
        "I support Proposal 301 and rule 206"
    )


def test_casting_is_distinct_per_seat() -> None:
    seats = [f"p{i}" for i in range(8)]
    voices = cast(seats)
    assert voices == cast(seats)  # deterministic
    assert set(voices) == set(seats) | {"judge"}  # the table: players + judge, no narrator
    assert len(set(voices.values())) == 9 and set(voices.values()) <= set(ALL_VOICES)
    assert voices["judge"] == JUDGE_VOICE and JUDGE_VOICE not in SEAT_VOICES


def test_cast_overrides_apply_and_ignore_bad_values() -> None:
    seats = ["a", "b", "c"]
    voices = cast(seats, {"b": "bm_daniel", "c": "xx_nope", "ghost": "af_sky", "judge": "af_kore"})
    assert voices == {
        "a": SEAT_VOICES[0],
        "b": "bm_daniel",
        "c": SEAT_VOICES[2],
        "judge": "af_kore",
    }
    assert len(ALL_VOICES) == 28 and set(SEAT_VOICES) <= set(ALL_VOICES)


def test_default_script_is_only_the_table_talking() -> None:
    seats, events = finished_game()
    lines, voices = build_script(seats, events, VoiceOptions())
    kinds = [ln.kind for ln in lines]
    assert set(kinds) == {"proposal", "comment", "rebuttal", "vote", "ruling"}  # no asides
    assert {ln.speaker for ln in lines} == set(seats) | {"judge"}
    rulings = [ln for ln in lines if ln.kind == "ruling"]
    assert rulings and all(ln.speaker == "judge" and ln.voice == voices["judge"] for ln in rulings)
    assert all(ln.text.startswith("The proposal ") for ln in rulings)
    # each turn ends with the judge's ruling
    assert kinds.index("ruling") > kinds.index("vote")
    no_rulings = build_script(seats, events, VoiceOptions(rulings=False))[0]
    assert "ruling" not in {ln.kind for ln in no_rulings}
    assert lines[0].chapter == "Turn 1" and lines[-1].chapter.startswith("Turn")
    # first person, no name tags or narration
    assert lines[0].text.startswith("I propose")
    assert all(ln.text.startswith("I vote ") for ln in lines if ln.kind == "vote")
    assert not any(ln.text.startswith(f"{ln.speaker}.") for ln in lines)
    # the proposer's automatic yes is never voiced
    auto = {e.seq for e in events if e.type == "vote" and e.data.get("auto")}
    assert not any(ln.event_seq in auto for ln in lines)


def test_thoughts_and_interviews_are_opt_in() -> None:
    seats, events = finished_game()
    base = build_script(seats, events, VoiceOptions())[0]
    full = build_script(seats, events, VoiceOptions(thinking=True, interviews=True))[0]
    kinds = [ln.kind for ln in full]
    assert kinds[:2] == ["proposal", "aside"]  # the pitch, then the proposer's thinking
    aside = full[1]
    assert aside.voice == full[0].voice and aside.gain_db < 0 and aside.speed < 1
    assert full[-1].kind == "interview" and full[-1].chapter == "Interviews"
    # adding thoughts keeps every spoken line identical, so its cached audio is reused
    assert {ln.cache_key for ln in base} <= {ln.cache_key for ln in full}

    quiet = build_script(seats, events, VoiceOptions(votes=False))[0]
    assert "vote" not in {ln.kind for ln in quiet}
    assert estimate(quiet)["minutes"] < estimate(base)["minutes"] < estimate(full)["minutes"]


def test_aborted_and_unfinished_turns_are_not_voiced() -> None:
    seats, events = finished_game()
    n = len(events)
    extra = [
        Event(
            seq=n + 1,
            type="turn_started",
            actor="s",
            data={"turn": 9, "round": 3, "player": "Alpha"},
        ),
        Event(
            seq=n + 2,
            type="proposal",
            actor="Alpha",
            data={
                "number": 399,
                "player": "Alpha",
                "op": "enact",
                "target_rule": None,
                "text": "x",
                "summary": "never voiced",
                "thinking": "",
            },
        ),
        Event(seq=n + 3, type="turn_aborted", actor="s", data={"turn": 9, "reason": "x"}),
    ]
    lines, _ = build_script(seats, events + extra, VoiceOptions())
    assert not any("never voiced" in ln.text for ln in lines)


def test_render_caches_lines_and_times_add_up(tmp_path: Path) -> None:
    from mutagen.id3 import ID3
    from mutagen.mp3 import MP3

    seats, events = finished_game()
    synth = FakeSynth()
    vr = VoiceRenderer(tmp_path, lambda: synth)
    opts = VoiceOptions(interviews=True)
    lines, voices = build_script(seats, events, opts)
    progress: list[dict[str, Any]] = []
    m = asyncio.run(vr.render("g1", lines, voices, opts, progress.append))
    assert m is not None and progress[-1]["state"] == "done"
    assert len(synth.calls) == len({ln.cache_key for ln in lines})  # repeats synthesised once
    last = m["lines"][-1]
    assert abs(last["start"] + last["duration"] - m["duration"]) < 1e-6
    starts = [ln["start"] for ln in m["lines"]]
    assert starts == sorted(starts)
    d = tmp_path / "g1"
    mp3 = MP3(d / "game.mp3")
    assert abs(mp3.info.length - m["duration"]) < 0.3
    chaps = [f for f in ID3(d / "game.mp3").values() if f.FrameID == "CHAP"]
    assert len(chaps) == len(m["chapters"]) and m["chapters"][-1]["title"] == "Interviews"
    vtt = (d / "game.vtt").read_text()
    assert vtt.startswith("WEBVTT") and vtt.count(" --> ") == len(lines)
    # the cache holds only compact MP3 segments: about the size of game.mp3 itself
    cache = sum(f.stat().st_size for f in (d / "lines").iterdir())
    assert not list((d / "lines").glob("*.flac"))
    assert cache <= (d / "game.mp3").stat().st_size

    # a second render reuses every cached line
    synth.calls.clear()
    asyncio.run(vr.render("g1", lines, voices, opts))
    assert synth.calls == []
    # adding thoughts only synthesises the asides
    more, _ = build_script(seats, events, VoiceOptions(interviews=True, thinking=True))
    changed = {ln.cache_key for ln in more} - {ln.cache_key for ln in lines}
    asyncio.run(vr.render("g1", more, voices, VoiceOptions(interviews=True, thinking=True)))
    assert len(synth.calls) == len(changed) > 0
    assert all(c[2] < 1 for c in synth.calls)  # every new line is a slower aside

    # dropping thoughts again prunes their files: the cache holds only what's in use
    asyncio.run(vr.render("g1", lines, voices, opts))
    assert {f.stem for f in (d / "lines").iterdir()} == {ln.cache_key for ln in lines}


def test_cancel_stops_after_current_line_and_keeps_cache(tmp_path: Path) -> None:
    seats, events = finished_game()
    lines, voices = build_script(seats, events, VoiceOptions())
    vr = VoiceRenderer(tmp_path, FakeSynth)
    states: list[str] = []

    def progress(s: dict[str, Any]) -> None:
        states.append(s["state"])
        if s["state"] == "running" and s["done_lines"] == 3:
            assert vr.cancel("g1")

    assert asyncio.run(vr.render("g1", lines, voices, VoiceOptions(), progress)) is None
    assert states[-1] == "cancelled" and not vr.busy("g1") and not vr.cancel("g1")
    assert len(list((tmp_path / "g1" / "lines").glob("*.mp3"))) == 3
    assert not (tmp_path / "g1" / "manifest.json").exists()

    # rendering again resumes: only the remaining lines are synthesised
    synth = FakeSynth()
    vr2 = VoiceRenderer(tmp_path, lambda: synth)
    assert asyncio.run(vr2.render("g1", lines, voices, VoiceOptions())) is not None
    assert len(synth.calls) == len({ln.cache_key for ln in lines}) - 3


def test_voice_api(tmp_path: Path) -> None:
    app = create_app(
        str(tmp_path / "t.db"),
        fake_openrouter()[0],
        synth_factory=FakeSynth,
        audio_dir=tmp_path / "audio",
    )
    with TestClient(app) as c:
        g = c.post("/api/games", json=mock_config(max_turns=2).model_dump()).json()
        gid = g["id"]
        end = time.time() + 10
        while c.get(f"/api/games/{gid}").json()["state"]["status"] != "over" and time.time() < end:
            time.sleep(0.02)
        est = c.get(f"/api/games/{gid}/voice/estimate").json()
        with_thoughts = c.get(f"/api/games/{gid}/voice/estimate?thinking=true").json()
        assert est["available"] and 0 < est["lines"] < with_thoughts["lines"]
        assert est["voices"]["judge"] == JUDGE_VOICE
        judge = c.put(f"/api/games/{gid}/voice/cast", json={"judge": "af_kore"}).json()
        assert judge["cast"]["judge"] == "af_kore"

        # per-game casting: saved, validated, used by estimates and renders
        assert len(c.get("/api/voices").json()["voices"]) == 28
        assert c.put(f"/api/games/{gid}/voice/cast", json={"Alpha": "xx"}).status_code == 400
        assert c.put(f"/api/games/{gid}/voice/cast", json={"Nobody": "af_sky"}).status_code == 400
        r = c.put(f"/api/games/{gid}/voice/cast", json={"Alpha": "bm_daniel"}).json()
        assert r["cast"]["Alpha"] == "bm_daniel"
        assert r["custom"] == {"Alpha": "bm_daniel", "judge": "af_kore"}
        assert c.get(f"/api/games/{gid}/voice/cast").json()["cast"]["Alpha"] == "bm_daniel"
        assert c.get(f"/api/games/{gid}/voice/estimate").json()["voices"]["Alpha"] == "bm_daniel"
        # previews: a short cached sample per voice
        p = c.get("/api/voices/af_sky/preview")
        assert p.status_code == 200 and p.headers["content-type"] == "audio/mpeg"
        assert (tmp_path / "audio" / "_previews" / "af_sky.mp3").exists()
        assert c.get("/api/voices/nope/preview").status_code == 404
        assert c.get(f"/api/games/{gid}/voice/files/game.mp3").status_code == 404
        c.post(f"/api/games/{gid}/voice", json={"thinking": True})
        while time.time() < end:
            v = c.get(f"/api/games/{gid}/voice").json()
            if v["status"]["state"] in ("done", "error"):
                break
            time.sleep(0.02)
        assert v["status"]["state"] == "done" and v["manifest"]["lines"]
        assert v["manifest"]["voices"]["Alpha"] == "bm_daniel"
        assert all(
            ln["voice"] == "bm_daniel" for ln in v["manifest"]["lines"] if ln["speaker"] == "Alpha"
        )
        mp3 = c.get(f"/api/games/{gid}/voice/files/game.mp3")
        assert mp3.status_code == 200 and mp3.headers["content-type"] == "audio/mpeg"
        assert c.get(f"/api/games/{gid}/voice/files/game.vtt").text.startswith("WEBVTT")
        assert c.get(f"/api/games/{gid}/voice/files/..%2Ft.db").status_code == 404
        assert "Voiceover: nomic-" in c.get(f"/api/games/{gid}/transcript").text
        assert c.delete(f"/api/games/{gid}/voice").status_code == 409  # nothing running

    no_engine = create_app(
        str(tmp_path / "u.db"),
        fake_openrouter()[0],
        synth_factory=None,
        audio_dir=tmp_path / "audio2",
    )
    with TestClient(no_engine) as c:
        gid = c.post("/api/games", json=mock_config(max_turns=1).model_dump()).json()["id"]
        r = c.post(f"/api/games/{gid}/voice", json={})
        assert r.status_code == 501 and "pip install" in r.json()["detail"]
