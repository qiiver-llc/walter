"""Restart Walter from the companion app (walter/app_restart.py + /api/app/restart).
Nothing here restarts anything: the launcher and compile check are fakes."""
import asyncio
import json
from unittest.mock import MagicMock

import pytest

from walter import app_restart as R
from tests.test_app import PROXY, iso, make_app  # noqa: F401  (iso is a fixture)


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def restarter(tmp_path, items=(), compile_err="", clock=None):
    launched = []
    r = R.Restarter(MagicMock(items=list(items)), tmp_path, compile_fn=lambda: compile_err,
                    launch_fn=lambda: launched.append(1) or "fake", clock=clock or Clock())
    return r, launched


DEV = {"id": "d1", "name": "Phone"}


def test_happy_path_logs_and_launches(tmp_path):
    r, launched = restarter(tmp_path)
    status, body = asyncio.run(r.request(DEV))
    assert status == 200 and body["restarting"] and launched == [1]
    assert "Phone: restarting (fake)" in (tmp_path / "app-restarts.log").read_text()
    assert json.loads((tmp_path / "app-restart.json").read_text())["by"] == "Phone"


def test_compile_failure_refuses_with_the_error(tmp_path):
    r, launched = restarter(tmp_path, compile_err="SyntaxError: invalid syntax (brain.py, line 3)")
    status, body = asyncio.run(r.request(DEV, force=True))
    assert status == 409 and body["reason"] == "compile" and "brain.py" in body["error"]
    assert launched == [] and r.retry_after() == 0      # a refusal doesn't use up the 30 s


def test_running_workers_need_restart_anyway(tmp_path):
    items = [{"id": 7, "title": "Research", "status": "running", "started": 1.0},
             {"id": 6, "title": "Old", "status": "done"}]
    r, launched = restarter(tmp_path, items)
    status, body = asyncio.run(r.request(DEV))
    assert status == 409 and body["reason"] == "workers" and launched == []
    assert [w["id"] for w in body["workers"]] == [7] and body["workers"][0]["title"] == "Research"
    assert r.info()["workers"][0]["id"] == 7
    status, body = asyncio.run(r.request(DEV, force=True))
    assert status == 200 and launched == [1]


def test_one_restart_per_30_seconds_survives_the_restart(tmp_path):
    clock = Clock()
    r, launched = restarter(tmp_path, clock=clock)
    assert asyncio.run(r.request(DEV))[0] == 200
    clock.t += 10
    status, body = asyncio.run(r.request(DEV))
    assert status == 429 and body["reason"] == "too_soon" and body["retry_after"] == 21
    # a fresh process (after the restart) still remembers
    r2, launched2 = restarter(tmp_path, clock=clock)
    assert asyncio.run(r2.request(DEV))[0] == 429 and launched2 == []
    clock.t += 21
    assert asyncio.run(r2.request(DEV))[0] == 200
    assert "too soon" in (tmp_path / "app-restarts.log").read_text()


def test_launch_failure_is_reported(tmp_path):
    def boom():
        raise OSError("no systemctl")
    r = R.Restarter(MagicMock(items=[]), tmp_path, compile_fn=lambda: "", launch_fn=boom, clock=Clock())
    status, body = asyncio.run(r.request(DEV))
    assert status == 500 and "no systemctl" in body["error"]


def test_compile_check_real(tmp_path):
    (tmp_path / "walter").mkdir()
    (tmp_path / "walter" / "good.py").write_text("x = 1\n")
    assert R.compile_check(tmp_path) == ""
    (tmp_path / "walter" / "bad.py").write_text("def f(:\n")
    err = R.compile_check(tmp_path)
    assert "bad.py" in err and "SyntaxError" in err
    assert R.compile_check(tmp_path / "nowhere") == "no walter/*.py files found"


def test_launch_uses_systemd_run_detached(monkeypatch):
    calls = []
    monkeypatch.setattr(R.shutil, "which", lambda n: "/usr/bin/systemd-run")
    monkeypatch.setattr(R.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or MagicMock(returncode=0))
    monkeypatch.setattr(R.subprocess, "Popen", lambda *a, **k: pytest.fail("should not fall back"))
    assert R.launch_restart() == "systemd-run"
    cmd = calls[0]
    assert cmd[:2] == ["/usr/bin/systemd-run", "--user"] and "systemctl --user restart walter" in cmd[-1]
    assert cmd[-1].startswith("sleep ")          # the HTTP answer goes out first


def test_launch_falls_back_to_a_new_session(monkeypatch):
    got = {}
    monkeypatch.setattr(R.shutil, "which", lambda n: None)
    monkeypatch.setattr(R.subprocess, "Popen", lambda cmd, **kw: got.update(cmd=cmd, **kw))
    assert R.launch_restart() == "detached shell"
    assert got["start_new_session"] and "systemctl --user restart walter" in got["cmd"][-1]


# ---------------------------------------------------------------- the HTTP route
def test_route_needs_a_paired_device_and_runs_the_checks(iso):  # noqa: F811
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        app, apphub, cond, hub = make_app(iso)
        launched = []
        apphub.restarter.data_dir = iso / "data"      # never the real ~/.walter
        apphub.restarter.compile_fn = lambda: ""
        apphub.restarter.launch_fn = lambda: launched.append(1) or "fake"
        apphub.restarter.tasks = MagicMock(items=[{"id": 3, "title": "Build", "status": "running"}])
        async with TestClient(TestServer(app)) as c:
            assert (await c.get("/api/app/restart", headers=PROXY)).status == 401
            assert (await c.post("/api/app/restart", json={}, headers=PROXY)).status == 401
            code = apphub.store.start_pairing()["code"]
            token = (await (await c.post("/api/app/pair", json={"code": code, "name": "Phone"},
                                         headers=PROXY)).json())["token"]
            auth = {**PROXY, "Authorization": f"Bearer {token}"}
            j = await (await c.get("/api/app/restart", headers=auth)).json()
            assert j["available"] and j["workers"][0]["id"] == 3 and j["retry_after"] == 0
            v1 = await (await c.get("/api/app/version", headers=PROXY)).json()
            assert v1["started"] == apphub.started
            r = await c.post("/api/app/restart", json={}, headers=auth)
            assert r.status == 409 and (await r.json())["reason"] == "workers" and launched == []
            r = await c.post("/api/app/restart", json={"force": "yes"}, headers=auth)   # only a real true
            assert r.status == 409
            r = await c.post("/api/app/restart", json={"force": True}, headers=auth)
            assert r.status == 200 and (await r.json())["restarting"] and launched == [1]
            r = await c.post("/api/app/restart", json={"force": True}, headers=auth)
            assert r.status == 429 and launched == [1]
        assert "Phone" in (iso / "data" / "app-restarts.log").read_text()
    asyncio.run(go())
