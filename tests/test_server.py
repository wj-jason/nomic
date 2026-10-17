from __future__ import annotations

import time
from typing import Any

from fastapi.testclient import TestClient

import nomic.server.app as app_module
from nomic.llm.pricing import ModelPrice, Pricing
from nomic.server.app import create_app
from tests.conftest import fake_openrouter


def _offline_pricing(monkeypatch: Any) -> None:
    table = Pricing({"cheap/model": ModelPrice("cheap/model", "Cheap", 1e-6, 4e-6, 1e-7, 100000)})

    async def fake() -> Pricing:
        return table

    monkeypatch.setattr(app_module, "get_pricing", fake)
    import nomic.manager

    monkeypatch.setattr(nomic.manager, "get_pricing", fake)


def _wait(client: TestClient, gid: str, cond: Any, timeout: float = 10) -> dict[str, Any]:
    end = time.time() + timeout
    while time.time() < end:
        st = client.get(f"/api/games/{gid}").json()["state"]
        if cond(st):
            return st  # type: ignore[no-any-return]
        time.sleep(0.02)
    raise AssertionError("timed out")


SEATS = [{"name": "Me", "model": None}, {"name": "Bot", "model": "mock/fast"}]


def test_api_flow(tmp_path: Any, monkeypatch: Any) -> None:
    _offline_pricing(monkeypatch)
    app = create_app(str(tmp_path / "t.db"), fake_openrouter()[0])
    with TestClient(app) as c:
        assert c.get("/").status_code == 200
        assert {r["id"] for r in c.get("/api/rulesets").json()} == {"lite", "suber"}
        assert c.get("/api/models").json()[0]["id"] == "cheap/model"
        est = c.post(
            "/api/estimate",
            json={
                "seats": [
                    {"name": "A", "model": "cheap/model"},
                    {"name": "B", "model": "cheap/model"},
                ],
                "judge": {"model": "cheap/model"},
            },
        ).json()
        assert est["rounds"] == 13 and 0 < est["total"] < 1

        bad = c.post("/api/games", json={"seats": [{"name": "A", "model": "mock/x"}]})
        assert bad.status_code == 422
        g = c.post(
            "/api/games",
            json={"seats": SEATS, "judge": {"model": "mock/judge"}, "discussion": False, "seed": 3},
        ).json()
        gid = g["id"]
        st = _wait(c, gid, lambda s: s["pending"])
        req = st["pending"][0]
        assert req["kind"] == "propose"
        # invalid human answers are rejected before reaching the game
        assert (
            c.post(f"/api/games/{gid}/pending/{req['id']}", json={"op": "dance"}).status_code == 400
        )
        ok = c.post(
            f"/api/games/{gid}/pending/{req['id']}",
            json={"op": "enact", "target_rule": None, "text": "Hats.", "summary": "hats"},
        )
        assert ok.status_code == 200

        with c.websocket_connect(f"/ws/games/{gid}") as ws:
            snap = ws.receive_json()
            assert snap["type"] == "snapshot" and snap["events"][0]["type"] == "game_created"

        t = c.get(f"/api/games/{gid}/transcript")
        assert t.status_code == 200 and "attachment" in t.headers["content-disposition"]
        assert t.text.startswith(f"# Nomic game {gid}") and "Hats." in t.text
        assert "## Rules at the end" in t.text

        c.post(f"/api/games/{gid}/control", json={"action": "pause"})
        r = c.post(
            f"/api/games/{gid}/intervene",
            json={"kind": "set_score", "data": {"player": "Bot", "score": 12}},
        )
        assert r.json()["players"][1]["score"] == 12
        assert c.post(f"/api/games/{gid}/intervene", json={"kind": "nope"}).status_code == 400

        forked = c.post(f"/api/games/{gid}/fork", json={"seq": 3}).json()
        assert forked["status"] == "paused"
        games = c.get("/api/games").json()
        assert {x["id"] for x in games} == {gid, forked["id"]}

        c.post(f"/api/games/{gid}/intervene", json={"kind": "end_game", "data": {"winner": "Bot"}})
        st = _wait(c, gid, lambda s: s["status"] == "over")
        assert st["winner"] == "Bot"
        assert c.delete(f"/api/games/{forked['id']}").json() == {"ok": "deleted"}
