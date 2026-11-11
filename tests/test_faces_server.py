"""Face hub routes (walter/face_server.py): pairing over HTTP, auth failures, own settings only,
the cookie face page and its own files, heard turns -> arbitration -> conductor, replies on the
winning face, handoff, the LAN gate and the conductor's face frame. Never touches ~/.walter."""
import asyncio
import json
import time
from unittest.mock import MagicMock

import pytest

from walter import faces as F
from walter.config import Config


@pytest.fixture
def iso(tmp_path, monkeypatch):
    monkeypatch.setenv("WALTER_DATA", str(tmp_path / "data"))
    import walter.dashboard as dash
    (tmp_path / "face").mkdir()
    monkeypatch.setattr(dash, "FACE_DIR", tmp_path / "face")
    return tmp_path


def make_app(iso, conductor=None, window=0.05):
    from walter.face_server import FaceHub
    from walter.server import Hub, build_app
    cfg = Config()
    cfg.brain.home = str(iso / "home")
    cfg.faces.window_s = window
    cfg.faces.mdns = False
    (iso / "home").mkdir(exist_ok=True)
    import re
    conductor = conductor or MagicMock(state="idle", sleeping=False, user_muted=False, busy=False, speech=None,
                                       voice=None, ears=None, wake_re=re.compile(r"\b(walter)\b", re.I))
    hub = Hub()
    facehub = FaceHub(cfg, conductor, hub, F.FaceStore(iso / "data" / "faces.json"))
    reminders = MagicMock(items=[{"id": 2, "due": 1.0, "text": "tea", "kind": "remind", "repeat": None},
                                 {"id": 3, "due": 1.0, "text": "brief", "kind": "do", "repeat": None}])
    app = build_app(cfg, hub, conductor, reminders, MagicMock(), MagicMock(items=[]), MagicMock(), MagicMock(),
                    None, facehub)
    return app, facehub, conductor, hub, reminders


def run(coro):
    return asyncio.run(coro)


async def pair(c, facehub, name="Kitchen", **kw):
    code = facehub.store.start_pairing(name)["code"]
    r = await c.post("/api/face/pair", json={"code": code, "kind": "tablet", **kw})
    assert r.status == 200, await r.text()
    j = await r.json()
    return j["face"], j.get("token")


def test_pair_then_hello_settings_and_auth_failures(iso):
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        app, facehub, cond, hub, _ = make_app(iso)
        async with TestClient(TestServer(app)) as c:
            r = await c.post("/api/face/pair", json={"code": "123456", "name": "X"})
            assert r.status == 403
            assert (await c.post("/api/face/pair", data="x", headers={"Content-Type": "text/plain"})).status == 415
            face, token = await pair(c, facehub, room="kitchen")
            assert face["id"] == "kitchen" and face["room"] == "kitchen" and "token_sha256" not in face
            assert (iso / "face" / "display-kitchen.json").exists()   # seeded from the wall's
            auth = {"Authorization": f"Bearer {token}"}
            # no kiosk exemption: a face always needs its token, even from this machine
            assert (await c.get("/api/face/settings")).status == 401
            assert (await c.get("/api/face/settings", headers={"Authorization": "Bearer nope"})).status == 401
            r = await c.post("/api/face/hello", json={"ears": True, "speaker": True, "mic_quality": 0.7},
                             headers=auth)
            j = await r.json()
            assert r.status == 200 and j["face"]["name"] == "Kitchen" and j["page"] == "/face/?face=kitchen"
            assert facehub.ears_active() and facehub.has_speaker("kitchen")
            assert facehub.store.get("kitchen")["mic_quality"] == 0.7
            # own settings only; never brain config
            r = await c.post("/api/face/settings", json={"volume": 30, "theme": "ocean", "text_scale": 1.2},
                             headers=auth)
            j = await r.json()
            assert r.status == 200 and j["settings"]["volume"] == 30 and j["display"]["theme"] == "ocean"
            assert not (iso / "face" / "display.json").exists()   # the wall's file untouched
            for bad in ({"brain.model": "x"}, {"volume": 900}, {"theme": "nope"}, {"layout": "huge"},
                        {"run": "rm -rf /"}):
                assert (await c.post("/api/face/settings", json=bad, headers=auth)).status == 400, bad
            # kiosk-only routes refuse other callers' headers (proxied = not kiosk)
            proxied = {"Host": "x.example", "X-Forwarded-For": "100.64.0.9"}
            assert (await c.get("/api/faces/status", headers=proxied)).status == 403
            assert (await c.get("/api/faces/status")).status == 200
            # forgotten = locked out
            facehub.store.forget("kitchen")
            assert (await c.get("/api/face/settings", headers=auth)).status == 401
    run(go())


def test_wrong_tokens_are_rate_limited(iso):
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        app, facehub, *_ = make_app(iso)
        async with TestClient(TestServer(app)) as c:
            for _ in range(8):
                await c.get("/api/face/settings", headers={"Authorization": "Bearer wrong"})
            assert (await c.get("/api/face/settings", headers={"Authorization": "Bearer wrong"})).status == 429
    run(go())


def test_heard_best_mic_wins_and_reply_goes_to_that_face(iso):
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        app, facehub, cond, hub, _ = make_app(iso, window=0.1)
        async with TestClient(TestServer(app)) as c:
            _, kt = await pair(c, facehub, "Kitchen")
            _, gt = await pair(c, facehub, "Garage")
            k, g = {"Authorization": f"Bearer {kt}"}, {"Authorization": f"Bearer {gt}"}
            for h in (k, g):
                await c.post("/api/face/hello", json={"ears": True, "speaker": False}, headers=h)
            # not addressed: ignored
            r = await c.post("/api/face/heard", json={"text": "pass the salt", "score": 0.9}, headers=k)
            assert (await r.json())["status"] == "ignored"
            # the same words on two faces: one turn, best mic
            r1 = await c.post("/api/face/heard", json={"text": "Walter, what's the weather?", "score": 0.4}, headers=k)
            r2 = await c.post("/api/face/heard", json={"text": "walter whats the weather", "score": 0.8}, headers=g)
            assert (await r1.json())["status"] == (await r2.json())["status"] == "queued"
            facehub.offer_wall("Walter what's the weather", {"speaker": "x"}, 0.2)
            await asyncio.sleep(0.25)
            assert cond.submit.call_count == 1
            src, text, meta = cond.submit.call_args.args
            assert src == "face" and meta["face"] == "Garage" and meta["face_id"] == "garage"
            assert set(meta["heard_by"]) == {"Kitchen", "Garage", "Walter"} and not meta["named"]
            assert facehub.last_decision["face"] == "Garage"
            # the conductor asks for the reply object; it streams to Garage only
            rep = facehub.reply(meta)
            rep.state("thinking")
            rep.say("Sunny and cold.")
            rep.raw("[happy] Sunny and cold.")
            rep.finish()
            sent = [m for _, m in facehub.pending("garage", 0)]
            assert [m["t"] for m in sent][-3:] == ["state", "say", "reply"]
            assert sent[-1]["text"] == "Sunny and cold."
            assert not any(m["t"] == "say" for _, m in facehub.pending("kitchen", 0))
            assert facehub.followup["garage"] > time.time()   # Garage listens without its name now
            r = await c.post("/api/face/heard", json={"text": "and tomorrow?", "score": 0.5}, headers=g)
            assert (await r.json())["status"] == "queued"
            # name beats best mic
            cond.submit.reset_mock()
            await asyncio.sleep(0.2)
            cond.submit.reset_mock()
            await c.post("/api/face/heard", json={"text": "hey kitchen set a timer", "score": 0.1}, headers=k)
            await c.post("/api/face/heard", json={"text": "hey kitchen, set a timer", "score": 0.9}, headers=g)
            await asyncio.sleep(0.25)
            src, text, meta = cond.submit.call_args.args
            assert meta["face_id"] == "kitchen" and meta["named"] and text == "hey kitchen, set a timer"
            # the wall wins: an ordinary voice turn with the wall's own meta
            cond.submit.reset_mock()
            facehub.offer_wall("Walter lights off", {"speaker": "speaker: someone"}, 0.9)
            await c.post("/api/face/heard", json={"text": "walter lights off", "score": 0.3}, headers=k)
            await asyncio.sleep(0.25)
            assert cond.submit.call_args.args == ("voice", "Walter lights off", {"speaker": "speaker: someone"})
            # a muted face hears nothing
            await c.post("/api/face/settings", json={"muted": True}, headers=k)
            r = await c.post("/api/face/heard", json={"text": "walter hi", "score": 0.9}, headers=k)
            assert (await r.json())["status"] == "muted"
            assert (await c.post("/api/face/heard", json={"text": 5}, headers=k)).status == 400
    run(go())


def test_stop_on_the_face_cancels_its_reply(iso):
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        app, facehub, cond, *_ = make_app(iso)
        cond.brain.interrupt = MagicMock(return_value=asyncio.sleep(0))
        async with TestClient(TestServer(app)) as c:
            _, kt = await pair(c, facehub, "Kitchen")
            rep = facehub.reply({"face_id": "kitchen", "face_turn": 1})
            r = await c.post("/api/face/heard", json={"text": "stop", "score": 0.5},
                             headers={"Authorization": f"Bearer {kt}"})
            assert (await r.json())["status"] == "stopped" and rep.cancelled
            assert facehub.pending("kitchen", 0)[-1][1]["t"] == "stop"
    run(go())


def test_events_stream_delivers_in_order(iso):
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        app, facehub, *_ = make_app(iso)
        async with TestClient(TestServer(app)) as c:
            _, kt = await pair(c, facehub, "Kitchen")
            facehub.emit("kitchen", {"t": "say", "text": "old"})
            r = await c.get("/api/face/events", params={"after": "1"}, headers={"Authorization": f"Bearer {kt}"})
            assert r.status == 200 and r.headers["Content-Type"].startswith("text/event-stream")
            facehub.emit("kitchen", {"t": "card", "title": "hi"})
            got = []
            while len(got) < 2:
                line = (await r.content.readline()).decode().strip()
                if line.startswith("data: "):
                    got.append(json.loads(line[6:]))
            assert [m["t"] for m in got] == ["say", "card"]
            assert facehub.online["kitchen"]["streams"] == 1
            r.close()
            assert (await c.get("/api/face/events")).status == 401
    run(go())


def test_face_page_cookie_and_own_files(iso):
    from aiohttp.test_utils import TestClient, TestServer
    face_dir = iso / "face"

    async def go():
        import walter.server as S
        old = S.FACE_DIR
        S.FACE_DIR = face_dir
        try:
            (face_dir / "index.html").write_text("<html>face</html>")
            (face_dir / "walter.js").write_text("//js")
            (face_dir / "browser.json").write_text('{"token": "secret"}')
            (face_dir / "display.json").write_text('{"theme": "cream"}')
            app, facehub, *_ = make_app(iso)
            async with TestClient(TestServer(app)) as c:
                _, kt = await pair(c, facehub, "Kitchen")
                _, gt = await pair(c, facehub, "Garage")
                (face_dir / "display-garage.json").write_text('{"theme": "forest"}')
                (face_dir / "display-kitchen.json").write_text('{"theme": "ocean"}')
                r = await c.get("/face/", allow_redirects=False)
                assert r.status == 302 and r.headers["Location"] == "/face/pair"
                assert (await c.get("/face/walter.js")).status == 401
                assert (await c.get("/face/?token=bad", allow_redirects=False)).status == 401
                r = await c.get(f"/face/?token={kt}", allow_redirects=False)
                assert r.status == 302 and r.headers["Location"] == "/face/?face=kitchen"
                ck = r.headers.get("Set-Cookie", "")
                assert "HttpOnly" in ck and "SameSite=Strict" in ck and "Path=/face" in ck
                r = await c.get("/face/?face=garage", allow_redirects=False)   # a face can't be another
                assert r.status == 302 and r.headers["Location"] == "/face/?face=kitchen"
                r = await c.get("/face/?face=kitchen")
                assert r.status == 200 and "face" in await r.text()
                assert (await c.get("/face/walter.js")).status == 200
                j = await (await c.get("/face/display.json")).json()
                assert j.get("theme") == "ocean"                       # its own, not the wall's
                assert (await c.get("/face/display-kitchen.json")).status == 200
                assert (await c.get("/face/display-garage.json")).status == 404   # not another face's
                assert (await c.get("/face/browser.json")).status == 404          # never the browser token
                assert (await c.get("/face/card.json")).status == 200
                assert (await c.get("/face/%2e%2e/data/faces.json")).status in (403, 404)
                assert (await c.get("/face/pair")).status == 200
        finally:
            S.FACE_DIR = old
    run(go())


def test_browser_pairing_sets_cookie_without_showing_token(iso):
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        app, facehub, *_ = make_app(iso)
        async with TestClient(TestServer(app)) as c:
            code = facehub.store.start_pairing("Kitchen")["code"]
            r = await c.post("/api/face/pair", json={"code": code, "cookie": True})
            j = await r.json()
            assert r.status == 200 and "token" not in j and j["page"] == "/face/?face=kitchen"
            assert "walter_face=" in r.headers.get("Set-Cookie", "")
    run(go())


def test_handoff_send_text_card_media_reminder(iso):
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        app, facehub, cond, hub, reminders = make_app(iso)
        async with TestClient(TestServer(app)) as c:
            _, kt = await pair(c, facehub, "Kitchen")
            k = {"Authorization": f"Bearer {kt}"}
            # no speaker agent: text becomes a card there; media is refused
            r = await c.post("/api/faces/send", json={"face": "kitchen", "kind": "text", "text": "Tea's ready"})
            j = await r.json()
            assert r.status == 200 and j["spoken"] is False
            card = json.loads((iso / "face" / "card-kitchen.json").read_text())
            assert card["body"] == "Tea's ready"
            r = await c.post("/api/faces/send", json={"face": "kitchen", "kind": "media", "url": "https://x.example/a.mp3"})
            assert r.status == 409
            await c.post("/api/face/hello", json={"speaker": True}, headers=k)
            r = await c.post("/api/faces/send", json={"face": "Kitchen", "kind": "media",
                                                      "url": "https://x.example/a.mp3", "title": "Show", "start": 61})
            assert r.status == 200
            ev = facehub.pending("kitchen", 0)[-1][1]
            assert ev["t"] == "media" and ev["start"] == 61 and ev["url"] == "https://x.example/a.mp3"
            for bad in ({"url": "file:///etc/passwd"}, {"url": "javascript:x"}):
                r = await c.post("/api/faces/send", json={"face": "kitchen", "kind": "media", **bad})
                assert r.status == 400
            r = await c.post("/api/faces/send", json={"face": "kitchen", "kind": "text", "text": "[happy] Hi there"})
            assert (await r.json())["spoken"] is True
            assert facehub.pending("kitchen", 0)[-1][1] == {**facehub.pending("kitchen", 0)[-1][1], "t": "say", "text": "Hi there"}
            r = await c.post("/api/faces/send", json={"face": "kitchen", "kind": "card", "title": "List", "body": "eggs"})
            assert r.status == 200 and json.loads((iso / "face" / "card-kitchen.json").read_text())["title"] == "List"
            assert (await c.post("/api/faces/send", json={"face": "nobody", "kind": "text", "text": "x"})).status == 404
            assert (await c.post("/api/faces/send", json={"face": "kitchen", "kind": "exec"})).status == 400
            # reminders move; routines don't
            r = await c.post("/api/faces/send", json={"face": "kitchen", "kind": "reminder", "id": 2})
            assert r.status == 200 and reminders.items[0]["face"] == "kitchen" and reminders._save.called
            assert (await c.post("/api/faces/send", json={"face": "kitchen", "kind": "reminder", "id": 3})).status == 400
            assert (await c.post("/api/faces/send", json={"face": "kitchen", "kind": "reminder", "id": 99})).status == 404
            assert facehub.deliver_reminder("kitchen", "tea") is True
            assert facehub.deliver_reminder("nobody", "tea") is False
            # a face can't use the handoff API (kiosk only)
            r = await c.post("/api/faces/send", json={"face": "kitchen", "kind": "text", "text": "x"},
                             headers={"Host": "x.example", "X-Forwarded-For": "100.64.0.9", **k})
            assert r.status == 403
    run(go())


def test_gate_faces_lan_only_never_through_the_tunnel():
    from walter.server import gate_ok, lan_open
    cfg = Config()
    proxied = {"Host": "walter.example.ts.net", "X-Forwarded-For": "1.2.3.4"}
    assert lan_open(cfg)
    assert gate_ok(cfg, "/api/face/hello", "192.168.1.20", {})
    assert gate_ok(cfg, "/face/", "192.168.1.20", {})
    assert gate_ok(cfg, "/face/walter.js", "100.70.1.2", {})          # tailnet
    assert not gate_ok(cfg, "/face/", "8.8.8.8", {})                  # internet
    assert not gate_ok(cfg, "/api/face/hello", "127.0.0.1", proxied)  # the public tunnel
    assert not gate_ok(cfg, "/api/faces/send", "192.168.1.20", {})    # handoff: this machine only
    assert not gate_ok(cfg, "/face/../display.json", "192.168.1.20", {})
    cfg.app.allow_lan = False                                        # same switch as paired app devices
    assert not gate_ok(cfg, "/face/", "192.168.1.20", {})
    cfg.app.allow_lan = True
    cfg.faces.enabled = False
    assert not gate_ok(cfg, "/face/", "192.168.1.20", {})


# ---------------------------------------------------------------- conductor
def test_conductor_frames_face_turns_and_routes_replies():
    from walter.conductor import Conductor
    meta = {"face": "Kitchen", "room": "kitchen", "score": 0.81, "named": False,
            "heard_by": ["Walter", "Kitchen"], "audio": True, "workspace": "Home"}
    out = Conductor._face_frame("what's the weather", meta, "Tuesday")
    assert out.startswith("[heard on face Kitchen (room: kitchen) at Tuesday; best mic, score 0.81; "
                          "also heard by Walter;")
    assert "NOT on the wall" in out and "spoken on that face's speaker" in out and "workspace: Home" in out
    assert out.endswith("]\nwhat's the weather")
    out = Conductor._face_frame("x", {"face": "Garage", "named": True, "audio": False}, "now")
    assert "called that face by name" in out and "captions" in out


def test_wall_turn_joins_arbiter_only_when_faces_listen():
    from walter.conductor import Conductor
    c = Conductor.__new__(Conductor)
    c._utt_levels = [0.4, 0.5]
    c.ears = MagicMock(_floor=50.0)
    assert 0 < c.wall_mic_score() <= 1
    c.faces = None
    assert c._face_named("hey kitchen hi") is False
    c.faces = MagicMock()
    c.faces.named.return_value = {"id": "kitchen"}
    assert c._face_named("hey kitchen hi") is True
