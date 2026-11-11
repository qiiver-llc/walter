"""Companion app player: /api/app/player (walter/app_player.py). Nothing real plays:
the state and the subprocess runner are fakes."""
import asyncio
from pathlib import Path

import pytest

from walter import app_player as AP
from tests.test_app import PROXY, iso, make_app  # noqa: F401  (iso is a fixture)

POD = {"kind": "podcast", "title": "Ep 9", "show": "Net Show", "art": "", "pos": 100.0, "dur": 3600,
       "paused": False, "speed": 1.0, "volume": 80, "ducked": False, "can_next": False,
       "upnext": [{"guid": "g-1", "show": "Net Show", "title": "Ep 8", "resume": True}]}


def test_argv_whitelist_and_numbers():
    assert AP.argv_for("podcast", "back", {}) == ["walter.podcast", "back", "15"]
    assert AP.argv_for("podcast", "forward", {}) == ["walter.podcast", "skip", "30"]
    assert AP.argv_for("podcast", "seek", {"value": 61.6}) == ["walter.podcast", "seekto", "62"]
    assert AP.argv_for("podcast", "speed", {"value": 1.25}) == ["walter.podcast", "speed", "1.25"]
    assert AP.argv_for("youtube", "seek", {"value": 90}) == ["walter.media", "seek", "90"]
    assert AP.argv_for("youtube", "next", {}) == ["walter.media", "next"]
    assert AP.argv_for("podcast", "play", {"guid": "g-1"}, {"g-1"}) == ["walter.podcast", "play-guid", "g-1"]
    for kind, action, d in [("podcast", "rm", {}), ("podcast", "next", {}), ("none", "pause", {}),
                            ("podcast", "speed", {"value": 9}), ("podcast", "speed", {"value": "1; rm -rf /"}),
                            ("youtube", "volume", {"value": True}), ("youtube", "volume", {"value": 101}),
                            ("podcast", "seek", {"value": float("nan")}), ("podcast", "seek", {}),
                            ("podcast", "play", {"guid": "--help"})]:
        with pytest.raises(ValueError):
            AP.argv_for(kind, action, d, {"g-1"})


def test_routes_need_pairing_and_validate(iso, monkeypatch, tmp_path):  # noqa: F811
    from aiohttp.test_utils import TestClient, TestServer
    ran = []

    async def fake_run(argv, timeout=30):
        ran.append(argv)
        return 0, "ok"
    monkeypatch.setattr(AP, "player_state", lambda: dict(POD))
    monkeypatch.setattr(AP, "run_argv", fake_run)
    art = Path(AP.P.UPNEXT_FILE).with_name(AP.P.FACE_ART)

    async def go():
        app, apphub, _, _ = make_app(iso)
        async with TestClient(TestServer(app)) as c:
            assert (await c.get("/api/app/player", headers=PROXY)).status == 401
            assert (await c.post("/api/app/player/pause", json={}, headers=PROXY)).status == 401
            assert (await c.get("/api/app/player/art/0123456789abcdef.jpg", headers=PROXY)).status == 401
            assert ran == []
            code = apphub.store.start_pairing()["code"]
            token = (await (await c.post("/api/app/pair", json={"code": code, "name": "Phone"},
                                         headers=PROXY)).json())["token"]
            auth = {**PROXY, "Authorization": f"Bearer {token}"}
            j = await (await c.get("/api/app/player", headers=auth)).json()
            assert j["kind"] == "podcast" and j["title"] == "Ep 9"
            r = await c.post("/api/app/player/pause", json={}, headers=auth)
            assert r.status == 200 and ran[-1] == ["walter.podcast", "pause"]
            r = await c.post("/api/app/player/seek", json={"value": 1200}, headers=auth)
            assert r.status == 200 and ran[-1] == ["walter.podcast", "seekto", "1200"]
            r = await c.post("/api/app/player/play", json={"guid": "g-1"}, headers=auth)
            assert r.status == 200 and ran[-1] == ["walter.podcast", "play-guid", "g-1"]
            n = len(ran)
            for action, body in [("shutdown", {}), ("speed", {"value": 5}), ("volume", {"value": "50"}),
                                 ("seek", {"value": -1}), ("play", {"guid": "not-listed"}), ("next", {})]:
                r = await c.post(f"/api/app/player/{action}", json=body, headers=auth)
                assert r.status == 400, action
            assert len(ran) == n
            r = await c.post("/api/app/player/pause", data="x", headers={**auth, "Content-Type": "text/plain"})
            assert r.status == 415
            # art: only hex names from face/podcast-art
            for bad in ("..%2Fapp.js", "x.jpg", "0123456789abcdef.png"):
                # 403: the server's gate refuses dot segments before the handler's own 404
                assert (await c.get(f"/api/app/player/art/{bad}", headers=auth)).status in (403, 404)
            if art.is_dir():
                real = next((p for p in art.glob("*.jpg") if AP.ART_RE.match(p.name)), None)
                if real:
                    r = await c.get(f"/api/app/player/art/{real.name}", headers=auth)
                    assert r.status == 200 and r.content_type == "image/jpeg"
    asyncio.run(go())


def test_run_failure_is_reported(iso, monkeypatch):  # noqa: F811
    from aiohttp.test_utils import TestClient, TestServer

    async def fake_run(argv, timeout=30):
        return 1, "Nothing playing"
    monkeypatch.setattr(AP, "player_state", lambda: dict(POD))
    monkeypatch.setattr(AP, "run_argv", fake_run)

    async def go():
        app, _, _, _ = make_app(iso)
        async with TestClient(TestServer(app)) as c:   # the kiosk on localhost needs no token
            r = await c.post("/api/app/player/resume", json={})
            assert r.status == 502 and (await r.json())["error"] == "Nothing playing"
    asyncio.run(go())


def test_app_has_the_player():
    app = Path(__file__).resolve().parents[1] / "app"
    html, js = (app / "index.html").read_text(), (app / "player.js").read_text()
    assert 'id="miniPlayer"' in html and 'id="playerSheet"' in html and 'src="player.js"' in html
    assert '"/api/app/player"' in js and "Needs one restart to enable" in js
    assert "visibilityState" in js and "1500" in js
