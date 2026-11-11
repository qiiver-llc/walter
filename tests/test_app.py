"""Companion app: pairing, tokens, rate limits, the proxy/LAN gate, the /api/app
routes, the app websocket and the conductor's per-turn reply route.
Never touches ~/.walter (WALTER_DATA and every store path point at tmp_path)."""
import asyncio
import json
import os
import struct
import time
from unittest.mock import MagicMock

import pytest

from walter import devices as D
from walter.config import Config


@pytest.fixture
def iso(tmp_path, monkeypatch):
    monkeypatch.setenv("WALTER_DATA", str(tmp_path / "data"))
    import walter.dashboard as dash
    monkeypatch.setattr(dash, "FACE_DIR", tmp_path / "face")
    return tmp_path


# ---------------------------------------------------------------- the store
def test_pairing_happy_path_and_token_is_hashed(tmp_path):
    st = D.DeviceStore(tmp_path / "devices.json")
    code = st.start_pairing()["code"]
    assert len(code) == 6 and code.isdigit() and st.pairing_open()
    dev, token = st.pair(code, "Phone", "Alex")
    assert dev["name"] == "Phone" and dev["owner"] == "Alex" and len(token) >= 40
    raw = (tmp_path / "devices.json").read_text()
    assert token not in raw and D.token_hash(token) in raw
    assert oct(os.stat(tmp_path / "devices.json").st_mode & 0o777) == "0o600"
    assert st.check(token)["id"] == dev["id"] and "token_sha256" not in st.check(token)
    assert st.check("nope") is None and st.check("") is None and st.check(None) is None
    # one device per code
    assert not st.pairing_open()
    with pytest.raises(PermissionError):
        st.pair(code, "Second")


def test_wrong_codes_burn_the_code(tmp_path):
    st = D.DeviceStore(tmp_path / "devices.json")
    code = st.start_pairing()["code"]
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    for _ in range(D.PAIR_MAX_TRIES):
        with pytest.raises(PermissionError):
            st.pair(wrong, "Phone")
    with pytest.raises(PermissionError):   # even the right code is dead now
        st.pair(code, "Phone")
    assert st.list() == []


def test_code_expires(tmp_path):
    st = D.DeviceStore(tmp_path / "devices.json")
    code = st.start_pairing(now=1000, ttl=60)["code"]
    with pytest.raises(PermissionError):
        st.pair(code, "Phone", now=1061)


def test_names_cleaned_and_unique_and_revoke(tmp_path):
    st = D.DeviceStore(tmp_path / "devices.json")
    with pytest.raises(ValueError):
        st.pair(st.start_pairing()["code"], "  <> ")
    a, ta = st.pair(st.start_pairing()["code"], "Phone<script>")
    b, tb = st.pair(st.start_pairing()["code"], "phone script")
    assert a["name"] == "Phone script" and b["name"] == "phone script 2"
    assert st.revoke("PHONE SCRIPT") == ["Phone script"]
    assert st.check(ta) is None and st.check(tb) is not None
    assert st.revoke(b["id"]) == ["phone script 2"] and st.list() == []


def test_touch_is_throttled(tmp_path):
    st = D.DeviceStore(tmp_path / "devices.json")
    dev, _ = st.pair(st.start_pairing()["code"], "Phone")
    st.touch(dev["id"], "10.0.0.5", now=dev["created"] + 10)
    assert st.list()[0]["last_ip"] == "10.0.0.5"
    st.touch(dev["id"], "10.0.0.5", now=dev["created"] + 20)   # same ip, too soon: no write
    assert st.list()[0]["last_seen"] == dev["created"] + 10


# ---------------------------------------------------------------- rate limits
def test_rate_limiter_per_key_and_global():
    rl = D.RateLimiter(max_fails=3, window_s=60, block_s=100, global_max=5)
    for _ in range(3):
        assert rl.retry_after("a", now=0) == 0
        rl.fail("a", now=0)
    assert rl.retry_after("a", now=1) == 99
    assert rl.retry_after("b", now=1) == 0
    rl.fail("b", now=1); rl.fail("c", now=1)        # 5 failures in all: everyone waits
    assert rl.retry_after("d", now=2) > 0
    assert rl.retry_after("d", now=62) == 0          # the window slid past them
    assert rl.retry_after("a", now=101) == 0


def test_pair_lockout_behind_cloudflare_tunnel():
    """Through the tunnel every request comes from 127.0.0.1, so the lockout must
    key on CF-Connecting-IP: 5 wrong codes = that address waits 15 minutes."""
    rl = D.RateLimiter(max_fails=5, window_s=600, block_s=900, global_max=20)
    hdrs = lambda ip: {"Host": "walter.example.com", "CF-Connecting-IP": ip}
    a = D.client_ip("127.0.0.1", hdrs("203.0.113.7"))
    b = D.client_ip("127.0.0.1", hdrs("198.51.100.9"))
    assert a != b and a != "proxy"
    for _ in range(5):
        rl.fail(a, now=0)
    assert rl.retry_after(a, now=1) == pytest.approx(899)
    assert rl.retry_after(b, now=1) == 0
    assert rl.retry_after(a, now=901) == 0


# ---------------------------------------------------------------- who's asking
def test_proxy_and_kiosk_detection():
    local = {"Host": "127.0.0.1:8765"}
    assert D.is_kiosk("127.0.0.1", local) and D.is_kiosk("::1", {"Host": "localhost:8765"})
    assert not D.is_kiosk("192.168.1.9", local)
    via = {"Host": "walter.example.ts.net", "X-Forwarded-For": "1.2.3.4, 100.101.102.103"}
    assert D.proxied("127.0.0.1", via) and not D.is_kiosk("127.0.0.1", via)
    assert D.client_ip("127.0.0.1", via) == "100.101.102.103"     # the hop our proxy added
    assert D.client_ip("127.0.0.1", {**via, "CF-Connecting-IP": "203.0.113.7"}) == "203.0.113.7"
    # a proxy that sends no forwarding headers is still caught by its Host name
    assert D.proxied("127.0.0.1", {"Host": "walter.example.com"})
    # forwarding headers from the network are ignored
    assert D.client_ip("192.168.1.9", via) == "192.168.1.9"
    assert D.is_tailnet("100.88.1.2") and not D.is_tailnet("192.168.1.2")


def test_gate_rules():
    from walter.server import gate_ok
    cfg = Config()
    lan, local = {"Host": "192.168.1.5:8765"}, {"Host": "127.0.0.1:8765"}
    proxy = {"Host": "walter.example.ts.net", "X-Forwarded-For": "100.64.0.9"}
    assert gate_ok(cfg, "/api/tasks", "127.0.0.1", local)            # the kiosk: anything
    assert not gate_ok(cfg, "/api/tasks", "127.0.0.1", proxy)        # ...but not through a proxy
    assert not gate_ok(cfg, "/", "127.0.0.1", proxy)
    assert not gate_ok(cfg, "/dashboard/add", "127.0.0.1", proxy)
    assert not gate_ok(cfg, "/ws", "127.0.0.1", proxy)
    assert gate_ok(cfg, "/app/", "127.0.0.1", proxy) and gate_ok(cfg, "/api/app/me", "127.0.0.1", proxy)
    assert gate_ok(cfg, "/app/ws", "192.168.1.30", lan)              # home network
    assert gate_ok(cfg, "/app/", "100.70.1.2", lan)                  # tailnet address
    assert not gate_ok(cfg, "/app/", "8.8.8.8", lan)                 # internet, direct
    assert gate_ok(cfg, "/dashboard/add", "192.168.1.30", lan)       # existing phone page rule
    assert not gate_ok(cfg, "/api/tasks", "192.168.1.30", lan)
    assert not gate_ok(cfg, "/ws", "192.168.1.30", lan)
    cfg.app.allow_lan = False
    assert not gate_ok(cfg, "/app/", "192.168.1.30", lan)
    assert gate_ok(cfg, "/app/", "127.0.0.1", proxy)
    cfg.app.enabled = False
    assert not gate_ok(cfg, "/app/", "127.0.0.1", proxy)
    cfg.dashboard.phone_page = False
    assert gate_ok(cfg, "/anything", "192.168.1.30", lan)            # loopback only: no gate (unreachable)


def test_wall_phrase():
    from walter.app_server import WALL_RE, bump_version, parse_backlog
    assert WALL_RE.search("tell the kids dinner is ready, say it on the wall")
    assert WALL_RE.search("announce it through the wall speaker")
    assert not WALL_RE.search("what's on my calendar")
    assert bump_version("1.0.9") == "1.0.10" and bump_version("7") == "8" and bump_version("beta") == "beta.1"
    items = parse_backlog("# t\n- [ready] **Fix it**: details here\n- [done 2026-11-04 09:00] **Old**: x\ntext")
    assert items == [{"status": "ready", "title": "Fix it", "details": "details here"},
                     {"status": "done 2026-11-04 09:00", "title": "Old", "details": "x"}]


# ---------------------------------------------------------------- HTTP routes
PROXY = {"Host": "walter.example.ts.net", "X-Forwarded-For": "100.64.0.9"}


def make_app(iso, conductor=None, app_dir=None, reminders=None, tasks=None):
    from walter.app_server import AppHub
    from walter.server import Hub, build_app
    cfg = Config()
    cfg.brain.home = str(iso / "home")
    (iso / "home").mkdir(exist_ok=True)
    conductor = conductor or MagicMock(state="idle", sleeping=False, user_muted=False, busy=False, speech=None)
    hub = Hub()
    apphub = AppHub(cfg, conductor, D.DeviceStore(iso / "data" / "devices.json"), app_dir=app_dir)
    tasks = tasks or MagicMock(items=[{"id": 1, "title": "Build", "status": "done", "result": "ok"}])
    reminders = reminders or MagicMock(items=[{"id": 2, "due": 1.0, "text": "tea", "kind": "remind", "repeat": None}])
    app = build_app(cfg, hub, conductor, reminders, MagicMock(), tasks, MagicMock(), MagicMock(), apphub)
    return app, apphub, conductor, hub


def run(coro):
    return asyncio.run(coro)


def test_pair_then_use_the_api_through_a_proxy(iso):
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        app, apphub, cond, _ = make_app(iso)
        async with TestClient(TestServer(app)) as c:
            # no pairing open
            r = await c.post("/api/app/pair", json={"code": "123456", "name": "Phone"}, headers=PROXY)
            assert r.status == 403
            code = apphub.store.start_pairing()["code"]
            r = await c.post("/api/app/pair", data="code=1", headers={**PROXY, "Content-Type": "text/plain"})
            assert r.status == 415
            r = await c.post("/api/app/pair", json={"code": code, "name": "Phone", "owner": "Alex"}, headers=PROXY)
            assert r.status == 200
            token = (await r.json())["token"]
            auth = {**PROXY, "Authorization": f"Bearer {token}"}
            r = await c.get("/api/app/me", headers=auth)
            j = await r.json()
            assert r.status == 200 and j["device"]["name"] == "Phone" and "token_sha256" not in j["device"]
            assert (await c.get("/api/app/me", headers=PROXY)).status == 401
            assert (await c.get("/api/app/version", headers=PROXY)).status == 200   # public
            # the rest of Walter's API is closed to proxied requests, token or not
            for path in ("/api/status", "/api/tasks", "/", "/dashboard/add", "/api/dashboard"):
                assert (await c.get(path, headers=auth)).status == 403, path
            # panels
            r = await c.post("/api/app/list", json={"action": "add", "text": "shopping: eggs", "quick": True}, headers=auth)
            item = (await r.json())["item"]
            assert item["section"] == "shopping"
            r = await c.post("/api/app/list", json={"action": "done", "id": item["id"]}, headers=auth)
            assert (await r.json())["item"]["done_at"]
            r = await c.post("/api/app/list", json={"action": "remove", "id": item["id"]}, headers=auth)
            assert r.status == 200 and (await r.json())["items"] == []
            assert (await c.post("/api/app/list", json={"action": "boom"}, headers=auth)).status == 400
            (iso / "home" / "backlog.md").write_text("- [ready] **Thing**: do it\n")
            assert (await (await c.get("/api/app/backlog", headers=auth)).json())["items"][0]["title"] == "Thing"
            j = await (await c.get("/api/app/tasks", headers=auth)).json()
            assert j["tasks"][0]["title"] == "Build" and j["reminders"][0]["text"] == "tea"
            assert (await (await c.get("/api/app/events", headers=auth)).json())["events"] == []
            r = await c.post("/api/app/control", json={"action": "mute"}, headers=auth)
            assert r.status == 200 and cond.set_user_mute.call_args.args == (True,)
            await c.post("/api/app/control", json={"action": "sleep"}, headers=auth)
            assert cond.go_to_sleep.called
            assert (await c.post("/api/app/control", json={"action": "explode"}, headers=auth)).status == 400
            # kiosk-only endpoints refuse proxied requests
            assert (await c.post("/api/app/bump", headers=auth)).status == 403
            assert (await c.post("/api/app/bump")).status == 200
            # unpair: the token stops working
            assert (await (await c.post("/api/app/unpair", headers=auth)).json())["ok"]
            assert (await c.get("/api/app/me", headers=auth)).status == 401
    run(go())


def test_kiosk_needs_no_token(iso):
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        app, *_ = make_app(iso)
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/api/app/me")
            assert r.status == 200 and (await r.json())["device"]["id"] == "kiosk"
            # a wrong token from the kiosk is still wrong
            assert (await c.get("/api/app/me", headers={"Authorization": "Bearer x"})).status == 401
    run(go())


def test_bad_tokens_are_rate_limited(iso):
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        app, apphub, *_ = make_app(iso)
        async with TestClient(TestServer(app)) as c:
            bad = {**PROXY, "Authorization": "Bearer wrong"}
            codes = [(await c.get("/api/app/me", headers=bad)).status for _ in range(apphub.limiter.max_fails + 1)]
            assert codes[:-1] == [401] * apphub.limiter.max_fails and codes[-1] == 429
            # another client address is unaffected
            other = {**bad, "X-Forwarded-For": "100.64.0.10"}
            assert (await c.get("/api/app/me", headers=other)).status == 401
            # pairing guesses too
            apphub.store.start_pairing()
            st = []
            for i in range(apphub.pair_limiter.max_fails + 1):
                r = await c.post("/api/app/pair", json={"code": "000000" if i % 2 else "999999", "name": "x"},
                                 headers={**PROXY, "X-Forwarded-For": "100.64.0.77"})
                st.append(r.status)
            assert st[-1] == 429 and set(st[:-1]) == {403}
    run(go())


def test_app_shell_and_service_worker_are_versioned(iso):
    from aiohttp.test_utils import TestClient, TestServer
    appdir = iso / "app"
    appdir.mkdir()
    (appdir / "VERSION").write_text("1.2.3\n")
    (appdir / "index.html").write_text("<html>v__VERSION__</html>")
    (appdir / "sw.js").write_text("const BUILD='__BUILD__'; const SHELL=__SHELL__;")
    (appdir / "app.js").write_text("1")

    async def go():
        app, apphub, *_ = make_app(iso, app_dir=appdir)
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/app/", headers=PROXY)
            assert r.status == 200 and "v1.2.3" in await r.text()
            assert "Content-Security-Policy" in r.headers and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
            sw1 = await (await c.get("/app/sw.js", headers=PROXY)).text()
            assert "1.2.3-" in sw1 and '"app.js"' in sw1
            assert (await c.get("/app/app.js", headers=PROXY)).status == 200
            # Walter edits a file: a new build without bumping, so installed copies update
            time.sleep(0.01)
            (appdir / "app.js").write_text("22")
            assert apphub.check_version()
            sw2 = await (await c.get("/app/sw.js", headers=PROXY)).text()
            assert sw2 != sw1
            (appdir / "VERSION").write_text("1.2.4\n")
            assert apphub.check_version() and apphub.version == "1.2.4"
            assert not apphub.check_version()
    run(go())


# ---------------------------------------------------------------- websocket
def test_websocket_auth_chat_and_talk(iso):
    from aiohttp.test_utils import TestClient, TestServer
    import numpy as np

    async def go():
        cond = MagicMock(state="idle", sleeping=False, user_muted=False, busy=False, speech=None)
        cond.ears = MagicMock()
        cond.ears.transcribe_pcm = MagicMock(return_value="what's on my list")
        app, apphub, cond, hub = make_app(iso, conductor=cond)
        code = apphub.store.start_pairing()["code"]
        _, token = apphub.store.pair(code, "Phone", "Alex")
        async with TestClient(TestServer(app)) as c:
            # no auth message: closed
            ws = await c.ws_connect("/app/ws", headers=PROXY)
            await ws.send_str(json.dumps({"t": "chat", "text": "hi"}))
            m = await ws.receive(timeout=5)
            assert m.type.name in ("CLOSE", "CLOSED", "CLOSING")
            # wrong token: refused
            ws = await c.ws_connect("/app/ws", headers=PROXY)
            await ws.send_str(json.dumps({"t": "auth", "token": "bad"}))
            assert (await ws.receive_json(timeout=5))["ok"] is False
            # right token
            ws = await c.ws_connect("/app/ws", headers=PROXY)
            await ws.send_str(json.dumps({"t": "auth", "token": token}))
            hello = await ws.receive_json(timeout=5)
            assert hello["ok"] and hello["device"]["name"] == "Phone"
            await ws.send_str(json.dumps({"t": "chat", "text": "hello there", "id": "m1"}))
            q = await ws.receive_json(timeout=5)
            assert q["t"] == "queued" and q["id"] == "m1" and not q["wall"]
            src, text, meta = cond.submit.call_args.args
            assert (src, text, meta["route"], meta["device"], meta["owner"]) == ("app", "hello there", "app", "Phone", "Alex")
            await ws.send_str(json.dumps({"t": "chat", "text": "dinner's ready, say it on the wall"}))
            assert (await ws.receive_json(timeout=5))["wall"] is True
            assert cond.submit.call_args.args[2]["route"] == "both"
            # wall state is forwarded
            hub.send({"t": "state", "state": "thinking"})
            assert (await ws.receive_json(timeout=5)) == {"t": "state", "state": "thinking"}
            hub.send({"t": "level", "v": 0.3})   # not forwarded
            # talk: PCM in, local transcript, heard turn
            pcm = (np.sin(np.arange(16000) / 5) * 3000).astype("<i2").tobytes()
            await ws.send_str(json.dumps({"t": "talk_start"}))
            await ws.send_bytes(b"\x01" + pcm)
            await ws.send_str(json.dumps({"t": "talk_end", "id": "v1"}))
            got = [await ws.receive_json(timeout=5) for _ in range(3)]
            assert [g["t"] for g in got] == ["transcribing", "heard", "queued"]
            assert got[1]["text"] == "what's on my list" and got[1]["provider"] == "local"
            assert cond.ears.transcribe_pcm.call_args.args[0] == pcm
            meta = cond.submit.call_args.args[2]
            assert meta["app_voice"] and meta["audio"] and meta["speaker"] == "speaker: Alex (app)"
            # notifications reach the app
            apphub.notify("reminder", "the tea is ready", "Reminder")
            n = await ws.receive_json(timeout=5)
            assert n["t"] == "notify" and n["text"] == "the tea is ready"
            # revoking closes the socket
            apphub.store.revoke("Phone")
            apphub.kick_revoked()
            assert (await ws.receive_json(timeout=5))["t"] == "revoked"
    run(go())


def test_kiosk_websocket_without_token(iso):
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        app, *_ = make_app(iso)
        async with TestClient(TestServer(app)) as c:
            ws = await c.ws_connect("/app/ws")
            await ws.send_str(json.dumps({"t": "auth"}))
            assert (await ws.receive_json(timeout=5))["device"]["id"] == "kiosk"
    run(go())


# ---------------------------------------------------------------- reply route
class FakeVoice:
    def __init__(self):
        self.said, self.cues, self.generation = [], [], 0

    def say(self, t, provider=None):
        self.said.append(t)

    def put(self, k, v=None, sound=True):
        self.cues.append((k, v))

    def stop(self):
        self.generation += 1

    async def wait_idle(self):
        pass


class FakeBrain:
    def __init__(self, reply="[happy] Sure thing. Here's a **link**: https://example.com ok."):
        self.reply, self.frames = reply, []

    async def interrupt(self):
        pass

    async def ask(self, text, model=None):
        self.frames.append(text)
        yield ("text_start", "")
        yield ("text", self.reply)
        yield ("done", self.reply)


class FakeApp:
    def __init__(self):
        self.replies = []

    def reply(self, meta):
        from walter.app_server import AppReply
        hub = MagicMock()
        hub.current = None
        r = AppReply(hub, meta)
        r.sent = hub.to_device.call_args_list
        self.replies.append(r)
        return r


def conductor():
    from walter.conductor import Conductor
    sent = []
    c = Conductor(Config(), brain=FakeBrain(), send=sent.append)
    c.voice, c.app = FakeVoice(), FakeApp()
    return c, sent


def test_app_turn_answers_only_the_app():
    c, sent = conductor()
    meta = {"route": "app", "device": "Phone", "device_id": "d1", "owner": "Alex", "app_turn": 4}
    asyncio.run(c._respond("app", "hi", meta))
    assert c.voice.said == [] and c.voice.cues == []          # the wall stays quiet
    assert "companion app on Phone (Alex)" in c.brain.frames[0] and "NOT out loud" in c.brain.frames[0]
    msgs = [call.args[1] for call in c.app.replies[0].sent]
    kinds = [m["t"] for m in msgs]
    assert kinds[0] == "typing" and "mood" in kinds and "say" in kinds and kinds[-1] == "typing"
    final = next(m for m in msgs if m["t"] == "reply")
    assert final["text"] == "Sure thing. Here's a **link**: https://example.com ok."   # formatting kept, tags gone
    assert next(m for m in msgs if m["t"] == "mood")["name"] == "happy"


def test_app_turn_on_the_wall_too():
    c, _ = conductor()
    meta = {"route": "both", "device": "Phone", "device_id": "d1", "app_turn": 1}
    asyncio.run(c._respond("app", "dinner is ready, say it on the wall", meta))
    assert c.voice.said and ("emotion", "happy") in c.voice.cues
    assert "spoken out loud on the wall speaker" in c.brain.frames[0]


def test_app_voice_turn_frame_and_sleeping_wall_untouched():
    c, sent = conductor()
    c.sleeping, c.state = True, "sleeping"
    meta = {"route": "app", "device": "Phone", "device_id": "d1", "app_voice": True,
            "speaker": "speaker: Alex (app)", "audio": True, "app_turn": 2}
    asyncio.run(c._respond("app", "what time is it", meta))
    assert "companion app's microphone on Phone" in c.brain.frames[0]
    assert "speaker: Alex (app)" in c.brain.frames[0]
    assert c.state == "sleeping" and not [m for m in sent if m.get("t") == "state"]


def test_wall_turn_unchanged():
    c, _ = conductor()
    asyncio.run(c._respond("voice", "hello"))
    assert c.voice.said and c.app.replies == []


def test_audio_frames_are_packed():
    from walter.app_server import pack, wav_bytes
    import numpy as np
    wav = wav_bytes(np.zeros(100, np.int16), 22050)
    b = pack({"t": "audio", "seq": 1}, wav)
    n = struct.unpack(">I", b[2:6])[0]
    assert b[:2] == b"WA" and json.loads(b[6:6 + n])["seq"] == 1 and b[6 + n:6 + n + 4] == b"RIFF"


# ---------------------------------------------------------------- walterctl
def test_walterctl_pair_devices_revoke(iso, monkeypatch, capsys):
    import runpy
    import subprocess
    from pathlib import Path
    import walter.config
    monkeypatch.setattr(walter.config, "DATA_DIR", iso / "data")   # no real config.toml either
    shown = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: shown.append(args))
    ctl = runpy.run_path(str(Path(__file__).resolve().parent.parent / "bin" / "walterctl"), run_name="walterctl")
    monkeypatch.setitem(ctl["app_cmd"].__globals__, "_quiet_call", lambda *a, **k: None)
    ctl["main"](["pair"])
    out = capsys.readouterr().out
    code = out.split("pairing code ")[1][:6]
    assert shown[0][1] == "--qr" and f"/app/#pair={code}" in shown[0][2]
    st = D.DeviceStore()        # WALTER_DATA = tmp: never the real ~/.walter
    assert str(st.path).startswith(str(iso))
    st.pair(code, "Tablet", "Sam")
    ctl["main"](["devices"])
    assert "Tablet (Sam)" in capsys.readouterr().out
    ctl["main"](["revoke", "tablet"])
    assert "unpaired: Tablet" in capsys.readouterr().out and st.list() == []
