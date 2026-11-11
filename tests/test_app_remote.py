"""Companion app Remote tab (walter/app_remote.py): auth, the daemon protocol, the
screencast relay, and that typed text never comes back or reaches the logs.
Uses a fake browser daemon (unix socket + MJPEG port); never touches ~/.walter."""
import asyncio
import json
import logging
import time

import pytest

from walter import app_remote as R
from tests.test_app import PROXY, make_app, run

SECRET = "hunter2-Do-Not-Log"


@pytest.fixture
def iso(tmp_path, monkeypatch):
    monkeypatch.setenv("WALTER_DATA", str(tmp_path / "data"))
    import walter.dashboard as dash
    monkeypatch.setattr(dash, "FACE_DIR", tmp_path / "face")
    monkeypatch.setattr(R, "SOCK", tmp_path / "b.sock")
    monkeypatch.setattr(R, "CTRL", tmp_path / "browser.json")
    return tmp_path


# ---------------------------------------------------------------- pure helpers
def test_url_bar():
    assert R.to_url("example.com") == "https://example.com"
    assert R.to_url("http://x.org/a?b=1") == "http://x.org/a?b=1"
    assert R.to_url("weather in town").startswith("https://www.google.com/search?q=weather+in+town")
    assert R.to_url("localhost").startswith("https://www.google.com/search")
    assert R.to_url("") == ""
    assert R.to_url("192.168.1.1:8080/setup") == "http://192.168.1.1:8080/setup"
    with pytest.raises(ValueError):
        R.to_url("javascript:alert(1)")
    with pytest.raises(ValueError):
        R.to_url("file:///etc/passwd")
    for u in ("http://127.0.0.1:8765/api/app/me", "https://localhost/", "http://[::1]/", "http://169.254.169.254/"):
        assert R.local_host(u), u
    assert not R.local_host("https://example.com/") and not R.local_host("http://192.168.1.20:8000/t.html")


def test_events_are_checked_and_clamped():
    evs = R.clean_events([{"t": "down", "x": 5000, "y": -3, "b": 7, "n": 9}, {"t": "kd", "k": "a"},
                          {"t": "wheel", "x": 1, "y": 2, "dx": "nan", "dy": 1e9}, "junk", {"t": "release"}], 1024, 810)
    assert evs == [{"t": "down", "x": 1023, "y": 0, "b": 0, "n": 3},
                   {"t": "wheel", "x": 1, "y": 2, "dx": 0.0, "dy": 3000}, {"t": "release"}]
    assert R.key_events("Shift+Tab")[0] == {"t": "kd", "k": "Shift"} and len(R.key_events("Enter")) == 2


# ---------------------------------------------------------------- a fake browser daemon
class FakeDaemon:
    def __init__(self, tmp):
        self.tmp, self.got, self.viewers = tmp, [], 0

    async def start(self):
        self.sock_srv = await asyncio.start_unix_server(self.on_sock, str(self.tmp / "b.sock"))
        self.http_srv = await asyncio.start_server(self.on_http, "127.0.0.1", 0)
        self.port = self.http_srv.sockets[0].getsockname()[1]
        return self

    def panel(self, open_=True):
        (self.tmp / "browser.json").write_text(json.dumps({
            "id": "1", "open": open_, "port": self.port, "token": "tok", "w": 1024, "h": 810,
            "alive": int(time.time()), "title": "Test page", "url": "192.168.1.20/t.html"}))

    async def on_sock(self, r, w):
        req = json.loads(await r.readline())
        self.got.append(req)
        a = req.get("args") or {}
        if req["cmd"] == "pad" and a.get("action") == "type":
            res = {"ok": True, "msg": f"typed {len(a['text'])} characters into the password box", "field": "the password box"}
        elif req["cmd"] == "pad":
            res = {"ok": True, "field": 'the "Email" box'}
        elif req["cmd"] == "input":
            res = {"ok": True, "edit": True}
        else:
            res = {"ok": True, "msg": req["cmd"], "title": "Test page", "url": "https://example.com/p?secret=1"}
        w.write((json.dumps(res) + "\n").encode())
        await w.drain()
        w.close()

    async def on_http(self, r, w):
        head = await r.readuntil(b"\r\n\r\n")
        assert b"/stream.mjpg?t=tok" in head
        self.viewers += 1
        w.write(b"HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=wframe\r\n\r\n")
        for i in range(3):
            jpg = b"\xff\xd8JPEG" + bytes([i])
            w.write(b"--wframe\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() +
                    b"\r\n\r\n" + jpg + b"\r\n")
            await w.drain()
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        w.close()

    def close(self):
        self.sock_srv.close()
        self.http_srv.close()


def paired(apphub):
    _, token = apphub.store.pair(apphub.store.start_pairing()["code"], "Phone")
    return {**PROXY, "Authorization": f"Bearer {token}"}


def test_remote_routes(iso, caplog):
    from aiohttp.test_utils import TestClient, TestServer
    caplog.set_level(logging.DEBUG)

    async def go():
        app, apphub, *_ = make_app(iso)
        fake = await FakeDaemon(iso).start()
        async with TestClient(TestServer(app)) as c:
            auth = paired(apphub)
            for path in ("/api/app/remote/state", "/api/app/remote/frame"):
                assert (await c.get(path, headers=PROXY)).status == 401
            assert (await c.post("/api/app/remote/type", json={"text": "x"}, headers=PROXY)).status == 401
            # closed panel
            st = await (await c.get("/api/app/remote/state", headers=auth)).json()
            assert st["open"] is False
            r = await c.post("/api/app/remote/key", json={"key": "Enter"}, headers=auth)
            assert (await r.json())["ok"] is False
            fake.panel()
            st = await (await c.get("/api/app/remote/state?peek=1", headers=auth)).json()
            assert st["open"] and st["title"] == "Test page" and st["field"] == 'the "Email" box'
            # raw input: forwarded as the daemon's "input" command
            r = await c.post("/api/app/remote/input", json={"ev": [{"t": "down", "x": 10, "y": 20}, {"t": "up", "x": 10, "y": 20}]}, headers=auth)
            assert (await r.json()) == {"ok": True, "edit": True}
            assert fake.got[-1]["cmd"] == "input" and fake.got[-1]["args"]["ev"][0]["t"] == "down"
            assert (await c.post("/api/app/remote/key", json={"key": "rm -rf"}, headers=auth)).status == 400
            await c.post("/api/app/remote/key", json={"key": "Shift+Tab"}, headers=auth)
            assert [e["k"] for e in fake.got[-1]["args"]["ev"]] == ["Shift", "Tab", "Tab", "Shift"]
            # typing: reaches the page, never comes back
            r = await c.post("/api/app/remote/type", json={"text": SECRET, "kind": "password", "enter": True}, headers=auth)
            body = await r.text()
            assert r.status == 200 and SECRET not in body and json.loads(body)["ok"]
            req = fake.got[-1]
            assert req["cmd"] == "pad" and req["person"] is True and req["args"]["text"] == SECRET
            assert req["args"]["kind"] == "password" and req["args"]["clear"] is False and req["args"]["enter"]
            assert (await c.post("/api/app/remote/type", json={"text": "x" * 501}, headers=auth)).status == 400
            # navigation
            j = await (await c.post("/api/app/remote/nav", json={"action": "back"}, headers=auth)).json()
            assert j["ok"] and j["url"] == "example.com/p" and fake.got[-1] == {"cmd": "back", "person": True}
            j = await (await c.post("/api/app/remote/nav", json={"action": "goto", "url": "news.example.org"}, headers=auth)).json()
            assert fake.got[-1]["cmd"] == "goto" and fake.got[-1]["args"]["url"] == "https://news.example.org"
            n = len(fake.got)
            j = await (await c.post("/api/app/remote/nav", json={"action": "goto", "url": "127.0.0.1:8765/api/app/me"}, headers=auth)).json()
            assert not j["ok"] and len(fake.got) == n
            j = await (await c.post("/api/app/remote/nav", json={"action": "close"}, headers=auth)).json()
            assert fake.got[-1]["cmd"] == "hide"
            # live preview relay
            r = await c.get("/api/app/remote/frame?after=-1", headers=auth)
            assert r.status == 200 and r.headers["Content-Type"] == "image/jpeg"
            seq = int(r.headers["X-Frame-Seq"])
            assert (await r.read()).startswith(b"\xff\xd8JPEG")
            r = await c.get(f"/api/app/remote/frame?after={seq}", headers=auth)
            assert r.status in (200, 204)
            assert fake.viewers >= 1
        fake.close()
        apphub.remote.wanted = 0
        await asyncio.sleep(0.05)
        assert SECRET not in caplog.text

    run(go())


def test_daemon_down_is_a_plain_message(iso):
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        app, apphub, *_ = make_app(iso)
        async with TestClient(TestServer(app)) as c:
            auth = paired(apphub)
            j = await (await c.post("/api/app/remote/nav", json={"action": "back"}, headers=auth)).json()
            assert j["ok"] is False and "isn't running" in j["error"]
            j = await (await c.post("/api/app/remote/type", json={"text": SECRET}, headers=auth)).json()
            assert j["ok"] is False and SECRET not in json.dumps(j)
    run(go())
