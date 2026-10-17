"""FastAPI app: REST controls, a websocket event stream per game, and the static UI."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from nomic.config import GameConfig
from nomic.engine.actions import ACTION_MODELS
from nomic.llm.openrouter import OpenRouterClient
from nomic.llm.pricing import estimate_game_cost, get_pricing
from nomic.manager import GameManager
from nomic.rules import RULESETS, ruleset_info
from nomic.runner import GameRunner
from nomic.store import Store
from nomic.transcript import render_transcript
from nomic.voice.render import FILES as VOICE_FILES
from nomic.voice.render import VoiceRenderer
from nomic.voice.script import VoiceOptions, build_script
from nomic.voice.script import estimate as voice_length
from nomic.voice.tts import INSTALL_HINT, REALTIME_FACTOR, Synth, render_device
from nomic.voice.voices import ALL_VOICES, JUDGE, PREVIEW_TEXT, cast, describe

STATIC = Path(__file__).parent / "static"


class Control(BaseModel):
    action: str  # pause | resume | step


class Intervention(BaseModel):
    kind: str
    data: dict[str, Any] = {}


class Fork(BaseModel):
    seq: int


def _default_synth() -> Callable[[], Synth] | None:
    from nomic.voice.tts import KokoroSynth, kokoro_available

    return KokoroSynth if kokoro_available() else None


def create_app(
    db_path: str = "data/nomic.db",
    client: OpenRouterClient | None = None,
    synth_factory: Callable[[], Synth] | None | str = "auto",
    audio_dir: str | Path | None = None,
) -> FastAPI:
    if audio_dir is None:
        audio_dir = Path(db_path).parent / "audio" if db_path != ":memory:" else "data/audio"
    voice = VoiceRenderer(
        Path(audio_dir),
        _default_synth() if synth_factory == "auto" else synth_factory,  # type: ignore[arg-type]
    )
    voice_tasks: set[asyncio.Task[Any]] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        manager = GameManager(Store(db_path), client)
        app.state.manager = manager
        await manager.refresh_pricing()
        manager.load_all()
        yield
        for t in voice_tasks:
            t.cancel()
        await manager.shutdown()

    app = FastAPI(title="Nomic Arena", lifespan=lifespan)

    def mgr() -> GameManager:
        m: GameManager = app.state.manager
        return m

    def runner(game_id: str) -> GameRunner:
        r = mgr().runners.get(game_id)
        if r is None:
            raise HTTPException(404, f"no game {game_id}")
        return r

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/models")
    async def models() -> list[dict[str, Any]]:
        pricing = await get_pricing()
        return [
            {
                "id": p.id,
                "name": p.name,
                "prompt": p.prompt * 1e6,
                "completion": p.completion * 1e6,
                "context": p.context_length,
            }
            for p in sorted(pricing.models.values(), key=lambda p: p.id)
        ]

    @app.get("/api/rulesets")
    async def rulesets() -> list[dict[str, Any]]:
        out = []
        for name in RULESETS:
            info = ruleset_info(name)
            out.append(
                {
                    "id": name,
                    "name": info["name"],
                    "win_points": info["win_points"],
                    "rules": len(info["rules"]),
                }
            )
        return out

    @app.post("/api/estimate")
    async def estimate(cfg: GameConfig) -> dict[str, Any]:
        return estimate_game_cost(cfg, await get_pricing())

    @app.get("/api/games")
    async def list_games() -> list[dict[str, Any]]:
        out = []
        for g in mgr().store.list_games():
            r = mgr().runners.get(g["id"])
            if r is None:
                continue
            st = r.state
            out.append(
                {
                    "id": g["id"],
                    "created_at": g["created_at"],
                    "parent_id": g["parent_id"],
                    "ruleset": st.config.get("ruleset"),
                    "status": st.status,
                    "winner": st.winner,
                    "turns": st.completed_turns,
                    "cost": st.total_cost,
                    "players": [
                        {"name": n, "model": st.players[n].model, "score": st.players[n].score}
                        for n in st.seat_order
                    ],
                    "pending": len(r.inbox.pending),
                }
            )
        return out

    @app.post("/api/games")
    async def create_game(cfg: GameConfig) -> dict[str, Any]:
        try:
            r = mgr().create(cfg)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return r.snapshot()

    @app.get("/api/games/{game_id}")
    async def get_game(game_id: str) -> dict[str, Any]:
        r = runner(game_id)
        return {"state": r.snapshot(), "events": [e.model_dump() for e in r.events]}

    @app.delete("/api/games/{game_id}")
    async def delete_game(game_id: str) -> dict[str, str]:
        runner(game_id)
        mgr().delete(game_id)
        return {"ok": "deleted"}

    @app.post("/api/games/{game_id}/reflect")
    async def reflect(game_id: str) -> dict[str, str]:
        r = runner(game_id)
        try:
            r.start_reflection()
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": "interviews started"}

    @app.get("/api/games/{game_id}/transcript")
    async def transcript(game_id: str) -> PlainTextResponse:
        r = runner(game_id)
        m = voice.manifest(game_id)
        audio = f"nomic-{game_id}.mp3 ({m['duration'] / 60:.0f} min)" if m else None
        return PlainTextResponse(
            render_transcript(game_id, r.state, r.events, audio=audio),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="nomic-{game_id}.md"'},
        )

    # ------------------------------------------------------------------ voiceover
    def voice_overrides(game_id: str) -> dict[str, str]:
        saved: dict[str, str] = mgr().store.get_setting(game_id, "voices", {})
        return saved

    @app.get("/api/voices")
    async def voices_list() -> dict[str, Any]:
        return {"available": voice.available, "voices": [describe(v) for v in ALL_VOICES]}

    @app.get("/api/voices/{voice_id}/preview")
    async def voice_preview(voice_id: str) -> FileResponse:
        if voice_id not in ALL_VOICES:
            raise HTTPException(404, "unknown voice")
        if not voice.available:
            raise HTTPException(501, INSTALL_HINT)
        return FileResponse(await voice.preview(voice_id, PREVIEW_TEXT), media_type="audio/mpeg")

    @app.get("/api/games/{game_id}/voice/cast")
    async def voice_cast(game_id: str) -> dict[str, Any]:
        r = runner(game_id)
        custom = voice_overrides(game_id)
        return {"cast": cast(r.state.seat_order, custom), "custom": custom}

    @app.put("/api/games/{game_id}/voice/cast")
    async def voice_set_cast(game_id: str, body: dict[str, str | None]) -> dict[str, Any]:
        """Set voices by player name; an empty value goes back to the default."""
        r = runner(game_id)
        custom = dict(voice_overrides(game_id))
        for player, v in body.items():
            if player not in r.state.players and player != JUDGE:
                raise HTTPException(400, f"unknown player {player!r}")
            if not v:
                custom.pop(player, None)
            elif v not in ALL_VOICES:
                raise HTTPException(400, f"unknown voice {v!r}")
            else:
                custom[player] = v
        mgr().store.set_setting(game_id, "voices", custom)
        return {"cast": cast(r.state.seat_order, custom), "custom": custom}

    @app.get("/api/games/{game_id}/voice/estimate")
    async def voice_estimate(
        game_id: str,
        thinking: bool = False,
        votes: bool = True,
        rulings: bool = True,
        rule_text: bool = False,
        interviews: bool = False,
    ) -> dict[str, Any]:
        r = runner(game_id)
        opts = VoiceOptions(
            thinking=thinking,
            votes=votes,
            rulings=rulings,
            rule_text=rule_text,
            interviews=interviews,
        )
        lines, voices = build_script(r.state.seat_order, r.events, opts, voice_overrides(game_id))
        device = render_device() if voice.available else "cpu"
        return {
            **voice_length(lines),
            "voices": voices,
            "available": voice.available,
            "device": device,
            "realtime_factor": REALTIME_FACTOR[device],
        }

    @app.post("/api/games/{game_id}/voice")
    async def voice_start(game_id: str, opts: VoiceOptions) -> dict[str, Any]:
        r = runner(game_id)
        if not voice.available:
            raise HTTPException(501, INSTALL_HINT)
        if voice.busy(game_id):
            return voice.status(game_id)
        lines, voices = build_script(r.state.seat_order, r.events, opts, voice_overrides(game_id))
        if not lines:
            raise HTTPException(400, "nothing to voice yet: no completed turns")
        task = asyncio.create_task(
            voice.render(
                game_id,
                lines,
                voices,
                opts,
                on_progress=lambda s: r.broadcast({"type": "voice", "status": s}),
            )
        )
        voice_tasks.add(task)
        task.add_done_callback(voice_tasks.discard)
        await asyncio.sleep(0)  # let it register as queued
        return voice.status(game_id)

    @app.delete("/api/games/{game_id}/voice")
    async def voice_cancel(game_id: str) -> dict[str, Any]:
        runner(game_id)
        if not voice.cancel(game_id):
            raise HTTPException(409, "no voiceover is rendering for this game")
        return voice.status(game_id)

    @app.get("/api/games/{game_id}/voice")
    async def voice_get(game_id: str) -> dict[str, Any]:
        runner(game_id)
        return {
            "available": voice.available,
            "status": voice.status(game_id),
            "manifest": voice.manifest(game_id),
        }

    @app.get("/api/games/{game_id}/voice/files/{name}")
    async def voice_file(game_id: str, name: str) -> FileResponse:
        runner(game_id)
        path = voice.game_dir(game_id) / name
        if name not in VOICE_FILES or not path.exists():
            raise HTTPException(404, "no such voiceover file")
        media = {
            "game.mp3": "audio/mpeg",
            "game.vtt": "text/vtt",
            "manifest.json": "application/json",
        }[name]
        return FileResponse(path, media_type=media)

    @app.get("/api/games/{game_id}/stats")
    async def stats(game_id: str) -> list[dict[str, Any]]:
        runner(game_id)
        return mgr().store.call_stats(game_id)

    @app.post("/api/games/{game_id}/control")
    async def control(game_id: str, body: Control) -> dict[str, Any]:
        r = runner(game_id)
        if body.action == "pause":
            r.pause()
        elif body.action == "resume":
            r.resume()
        elif body.action == "step":
            r.step()
        else:
            raise HTTPException(400, f"unknown action {body.action}")
        return r.snapshot()

    @app.post("/api/games/{game_id}/intervene")
    async def intervene(game_id: str, body: Intervention) -> dict[str, Any]:
        r = runner(game_id)
        try:
            r.intervene(body.kind, body.data)
        except (ValueError, KeyError, TypeError) as e:
            raise HTTPException(400, str(e)) from e
        return r.snapshot()

    @app.post("/api/games/{game_id}/pending/{req_id}")
    async def answer(game_id: str, req_id: str, body: dict[str, Any]) -> dict[str, str]:
        r = runner(game_id)
        req = r.inbox.pending.get(req_id)
        if req is None:
            raise HTTPException(404, "no such pending request (already answered?)")
        try:
            ACTION_MODELS[req.kind].model_validate(body)
        except ValidationError as e:
            raise HTTPException(400, str(e)) from e
        r.inbox.respond(req_id, body)
        return {"ok": "submitted"}

    @app.post("/api/games/{game_id}/fork")
    async def fork(game_id: str, body: Fork) -> dict[str, Any]:
        runner(game_id)
        try:
            r = mgr().fork(game_id, body.seq)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return r.snapshot()

    @app.get("/api/calls/{call_id}")
    async def call(call_id: int) -> dict[str, Any]:
        c = mgr().store.get_call(call_id)
        if c is None:
            raise HTTPException(404, "no such call")
        return c

    @app.websocket("/ws/games/{game_id}")
    async def ws(websocket: WebSocket, game_id: str) -> None:
        r = mgr().runners.get(game_id)
        if r is None:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        q = r.subscribe()
        try:
            await websocket.send_json(
                {
                    "type": "snapshot",
                    "state": r.snapshot(),
                    "events": [e.model_dump() for e in r.events],
                }
            )
            while True:
                await websocket.send_json(await q.get())
        except WebSocketDisconnect:
            pass
        finally:
            r.unsubscribe(q)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
