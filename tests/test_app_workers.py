"""Workers at once from the companion app: [brain] max_tasks is live (walter/live.py)
and /api/app/workers reads/sets it through Live.set (same path as walterctl config set)."""
import asyncio
from types import SimpleNamespace

import pytest

from walter import live
from walter.config import load
from tests.test_app import PROXY, iso, make_app  # noqa: F401  (iso is a fixture)

CFG = "# my settings\n[brain]\nmax_tasks = 1           # workers at once\nmodel = \"sonnet\"\n"


def test_max_tasks_is_live_and_validated(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(CFG)
    cfg = load(path)
    lv = live.Live(cfg, path)
    assert live.key_status("brain", "max_tasks") == "hot" and live.key_status("brain", "model") == "restart"
    r = lv.set("brain.max_tasks", "4")
    assert r["applied"] == ["brain.max_tasks"] and cfg.brain.max_tasks == 4 and not r["needs_restart"]
    assert "max_tasks = 4           # workers at once" in path.read_text()
    assert lv.snapshot()["config"]["brain"] == {"max_tasks": 4}
    for bad in ("0", "9", "2.5", "true", "many"):
        with pytest.raises(ValueError):
            live.set_in_file(path, "brain.max_tasks", bad)
    assert load(path).brain.max_tasks == 4 and cfg.brain.max_tasks == 4


def test_tasks_read_the_live_value_at_spawn(tmp_path):
    from walter.tasks import Tasks
    cfg = live.config_mod.Config()
    t = Tasks.__new__(Tasks)
    t.cfg, t.items = cfg, [{"id": 1, "status": "running"}]
    cfg.brain.max_tasks = 1
    with pytest.raises(RuntimeError, match="max 1"):
        asyncio.run(t.start("x", "y"))
    cfg.brain.max_tasks = 2       # a live reload sets it on the same Config object
    t.launcher = SimpleNamespace(launch=None)
    with pytest.raises(Exception) as e:
        asyncio.run(t.start("x", "y"))
    assert "max" not in str(e.value)   # got past the limit check


def test_route_needs_pairing_validates_and_applies_live(iso):  # noqa: F811
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        app, apphub, cond, hub = make_app(iso)
        cfg = apphub.cfg
        path = iso / "config.toml"
        path.write_text(CFG)
        cfg.brain.max_tasks = 1
        cond.live = live.Live(cfg, path)
        async with TestClient(TestServer(app)) as c:
            assert (await c.get("/api/app/workers", headers=PROXY)).status == 401
            assert (await c.post("/api/app/workers", json={"max_tasks": 3}, headers=PROXY)).status == 401
            assert cfg.brain.max_tasks == 1
            code = apphub.store.start_pairing()["code"]
            token = (await (await c.post("/api/app/pair", json={"code": code, "name": "Phone"},
                                         headers=PROXY)).json())["token"]
            auth = {**PROXY, "Authorization": f"Bearer {token}"}
            j = await (await c.get("/api/app/workers", headers=auth)).json()
            assert j["max_tasks"] == 1 and (j["min"], j["max"]) == (1, 8)
            assert j["threads"] >= 1 and "running" in j and j["queued"] == 0
            r = await c.post("/api/app/workers", json={"max_tasks": 3}, headers=auth)
            assert r.status == 200 and (await r.json())["max_tasks"] == 3
            assert cfg.brain.max_tasks == 3 and load(path).brain.max_tasks == 3
            assert "# workers at once" in path.read_text()
            for bad in ({"max_tasks": 0}, {"max_tasks": 9}, {"max_tasks": "4"}, {"max_tasks": 2.0},
                        {"max_tasks": True}, {"max_tasks": "1; rm -rf /"}, {}):
                r = await c.post("/api/app/workers", json=bad, headers=auth)
                assert r.status == 400 and (await r.json())["max_tasks"] == 3, bad
            assert load(path).brain.max_tasks == 3
            r = await c.post("/api/app/workers", data="max_tasks=2",
                             headers={**auth, "Content-Type": "text/plain"})
            assert r.status == 415
            assert (await c.get("/api/app/workers")).status == 200   # the kiosk, like other controls
    asyncio.run(go())


def test_running_and_queued_are_counted(iso):  # noqa: F811
    from unittest.mock import MagicMock
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        rem = MagicMock(items=[{"id": 1, "text": "QUEUED [Big job]: silently run: ...", "kind": "do"},
                               {"id": 2, "text": "tea", "kind": "remind"}])
        app, apphub, cond, hub = make_app(iso, reminders=rem, tasks=MagicMock(items=[], running=2))
        async with TestClient(TestServer(app)) as c:
            j = await (await c.get("/api/app/workers")).json()
            assert j["queued"] == 1 and j["running"] == 2
    asyncio.run(go())
