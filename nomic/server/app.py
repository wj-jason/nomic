"""FastAPI app: REST controls, a websocket event stream per game, and the static UI."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
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

STATIC = Path(__file__).parent / "static"


class Control(BaseModel):
    action: str  # pause | resume | step


class Intervention(BaseModel):
    kind: str
    data: dict[str, Any] = {}


class Fork(BaseModel):
    seq: int


def create_app(db_path: str = "data/nomic.db", client: OpenRouterClient | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        manager = GameManager(Store(db_path), client)
        app.state.manager = manager
        await manager.refresh_pricing()
        manager.load_all()
        yield
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
