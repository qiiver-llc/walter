"""First-run wizard: step state machine, gating, and the /api/firstrun endpoints."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from walter import firstrun as F


# ------------------------------------------------------------ state machine
def test_steps_advance_skip_and_resume(tmp_path):
    t = [100.0]
    w = F.Wizard(tmp_path / "state.json", now=lambda: t[0])
    assert w.current == "welcome"
    w.start()
    w.complete("welcome", {"walter_name": "Wally"})
    assert w.current == "network"
    w.skip("network")
    assert w.current == "audio" and w.status("network") == "skipped"
    w.complete("audio")
    # a reboot resumes at the same step with the same data
    w2 = F.Wizard(tmp_path / "state.json")
    assert w2.current == "claude"
    assert w2.data("welcome")["walter_name"] == "Wally"
    assert w2.status("network") == "skipped"


def test_revisit_and_wraparound(tmp_path):
    w = F.Wizard(tmp_path / "s.json")
    for k in F.STEPS[:-1]:
        w.skip(k)
    assert w.current == "hello"
    w.goto("network")                  # revisit a skipped step
    assert w.current == "network"
    w.complete("network", {"tz": "UTC"})
    assert w.status("network") == "done"
    assert w.current == "hello"        # nothing else pending: back to the end
    w.skip("network")                  # skipping a done step keeps it done
    assert w.status("network") == "done"


def test_unknown_step_rejected(tmp_path):
    w = F.Wizard(tmp_path / "s.json")
    with pytest.raises(ValueError):
        w.complete("nope")


def test_corrupt_state_starts_fresh(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{not json")
    assert F.Wizard(p).current == "welcome"
    p.write_text(json.dumps({"current": "evil", "steps": {"welcome": "hacked", "x": "done"}, "data": {"x": {}}}))
    w = F.Wizard(p)
    assert w.current == "welcome" and w.status("welcome") == "pending" and w.s["data"] == {}


def test_first_talk_timing_once(tmp_path):
    t = [1000.0]
    w = F.Wizard(tmp_path / "s.json", now=lambda: t[0])
    assert w.first_talk() is None      # not started
    w.start()
    t[0] = 1600.0
    assert w.first_talk() == 600.0
    t[0] = 2000.0
    assert w.first_talk() is None


def test_level_of():
    import array
    assert F.level_of(b"") == 0
    assert F.level_of(array.array("h", [0] * 3200).tobytes()) == 0
    loud = F.level_of(array.array("h", [16000, -16000] * 1600).tobytes())
    quiet = F.level_of(array.array("h", [30, -30] * 1600).tobytes())
    assert loud > 80 and quiet < 20


# ------------------------------------------------------------ service
def make(tmp_path, pending=True, done=False):
    etc = tmp_path / "etc"
    (etc / "firstrun").mkdir(parents=True)
    if pending:
        (etc / "firstrun" / "pending").write_text("")
    if done:
        (etc / "firstrun-done").write_text("")
    (etc / "invite").write_text("BETA-123\n")
    sent, applied, ran = [], [], []

    async def fake_run(*argv, **kw):
        ran.append(argv)
        return 0

    cfg = SimpleNamespace(server=SimpleNamespace(port=8765), app=SimpleNamespace(public_url="http://w.test"))
    fr = F.FirstRun(cfg, sent.append, setup=None, etc=etc, data_dir=tmp_path / "data",
                    apply_config=lambda k, v: applied.append((k, v)), run=fake_run)
    return SimpleNamespace(fr=fr, sent=sent, applied=applied, ran=ran, etc=etc, data=tmp_path / "data")


def test_gating(tmp_path):
    assert make(tmp_path / "a").fr.active
    assert not make(tmp_path / "b", pending=False).fr.active        # existing installs
    assert not make(tmp_path / "c", done=True).fr.active


def run(c):
    return asyncio.run(c)


def test_actions_and_finish(tmp_path):
    e = make(tmp_path)
    fr = e.fr
    fr.start()
    r = run(fr.handle("welcome", {"data": {"walter_name": "Wally<script>", "household": "The Testers"}}))
    assert r["ok"] and r["current"] == "network"
    assert fr.wizard.data("welcome")["walter_name"] == "Wallyscript"
    assert ("ears.wake_words", ["wallyscript", "walter"]) in e.applied
    r = run(fr.handle("network", {"data": {"place": {"name": "Somewhere", "lat": 47.6, "lon": -122.3}}}))
    assert r["ok"] and ("dashboard.lat", 47.6) in e.applied
    assert not run(fr.handle("network", {"step": "network", "data": {"place": {"lat": 999, "lon": 0}}}))["ok"]
    fr.wizard.goto("audio")
    run(fr.handle("skip", {"step": "audio"}))
    run(fr.handle("skip", {"step": "claude"}))
    r = run(fr.handle("privacy", {"data": {"camera": "yes", "permission_mode": "rm -rf"}}))
    p = fr.wizard.data("privacy")
    assert p["camera"] is False and p["permission_mode"] == "acceptEdits" and p["full_admin"] is False
    assert ("brain.permission_mode", "acceptEdits") in e.applied
    assert json.loads((e.data / "speech.json").read_text()) == {"tts": "local", "stt": "local"}
    run(fr.handle("extras", {"data": {"teams": ["Mariners"], "podcasts": []}}))
    r = run(fr.handle("cloudlink", {"data": {"want": True}}))
    req = json.loads((e.etc / "firstrun" / "cloudlink-request.json").read_text())
    assert req["invite"] == "BETA-123" and req["household"] == "The Testers"
    run(fr.handle("skip", {"step": "pair"}))
    assert fr.wizard.current == "hello"
    fr.on_heard("hey walter hello")
    r = run(fr.handle("hello", {}))
    assert r["ok"] and r["finished"]
    assert not fr.active and (e.etc / "firstrun" / "done").exists()
    lines = [json.loads(x) for x in (e.data / "firstrun-metrics.jsonl").read_text().splitlines()]
    assert [x["event"] for x in lines][-1] == "finished"
    assert any(x["event"] == "first_talk" for x in lines)


def test_cloudlink_no_network_calls(tmp_path, monkeypatch):
    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    e = make(tmp_path)
    assert run(e.fr.handle("cloudlink", {"data": {"want": True}}))["ok"]
    assert e.ran == []


# ------------------------------------------------------------ endpoints
class FakeHub:
    def __init__(self):
        self.store = SimpleNamespace(start_pairing=lambda: {"code": "123456", "expires": 0})

    def auth(self, request):
        if request.headers.get("Authorization") != "Bearer good":
            raise web.HTTPUnauthorized()
        return {"id": "phone"}


async def client_for(fr, apphub=None):
    app = web.Application()
    F.add_routes(app, fr, apphub)
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


def test_endpoints_auth(tmp_path, monkeypatch):
    from walter import devices as D
    monkeypatch.setattr(D, "is_kiosk", lambda peer, headers: False)   # act like a LAN phone
    e = make(tmp_path)
    fr = e.fr

    async def go():
        c = await client_for(fr, FakeHub())
        try:
            assert (await c.post("/api/firstrun/state", json={})).status == 403
            assert (await c.post("/api/firstrun/state", json={"k": "wrong"})).status == 403
            r = await c.post("/api/firstrun/state", json={"k": fr.key})
            assert r.status == 200 and (await r.json())["current"] == "welcome"
            assert (await c.get("/firstrun?k=nope")).status == 403
            assert (await c.get(f"/firstrun?k={fr.key}")).status == 200
            assert (await c.get("/api/firstrun/qr.svg")).status == 403          # wall only
            r = await c.post("/api/firstrun/paircode", json={"k": fr.key})
            assert (await r.json())["code"] == "123456"
            assert (await c.post("/api/firstrun/bogus", json={"k": fr.key})).status == 404
            # after the wizard: the key stops working; a paired device can still revisit
            (e.etc / "firstrun-done").write_text("")
            assert (await c.post("/api/firstrun/state", json={"k": fr.key})).status == 403
            assert (await c.post("/api/firstrun/state", json={}, headers={"Authorization": "Bearer bad"})).status == 401
            r = await c.post("/api/firstrun/goto", json={"step": "privacy"}, headers={"Authorization": "Bearer good"})
            assert r.status == 200 and (await r.json())["current"] == "privacy"
        finally:
            await c.close()
    run(go())


def test_endpoints_no_app(tmp_path, monkeypatch):
    from walter import devices as D
    monkeypatch.setattr(D, "is_kiosk", lambda peer, headers: False)
    e = make(tmp_path, pending=False)

    async def go():
        c = await client_for(e.fr, None)
        try:
            assert (await c.post("/api/firstrun/state", json={"k": e.fr.key})).status == 403
            assert (await c.get(f"/firstrun?k={e.fr.key}")).status == 403
        finally:
            await c.close()
    run(go())


def test_app_settings_page_needs_app(tmp_path, monkeypatch):
    from walter import devices as D
    monkeypatch.setattr(D, "is_kiosk", lambda peer, headers: False)
    e = make(tmp_path, done=True)

    async def go():
        c = await client_for(e.fr, FakeHub())
        try:
            r = await c.get("/firstrun?app=1")
            assert r.status == 200 and e.fr.key not in await r.text()
        finally:
            await c.close()
    run(go())
