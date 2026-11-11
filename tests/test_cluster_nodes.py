"""Two (and three) real ClusterNodes talking over HTTP on 127.0.0.1 (aiohttp test servers):
joining with the code, keys spreading through the lead, heartbeats + election, a follower pulling
and pushing shared files, failover with a fake clock, the face hello brain list, signed-only
routes, and remote jobs (placement, lead-only authorisation, fallback). Temp folders only."""
import asyncio
import json
from unittest.mock import MagicMock

import pytest

from walter import cluster as C
from walter.config import Config


class FakeTasks:
    def __init__(self, max_tasks=3):
        self.items, self.max_tasks = [], max_tasks
        self.on_finish = MagicMock()

    @property
    def running(self):
        return sum(1 for i in self.items if i["status"] == "running")

    async def start(self, title, prompt, quiet=False, model=None, workspace=None):
        if self.running >= self.max_tasks:
            raise RuntimeError("full")
        item = {"id": len(self.items) + 1, "title": title, "prompt": prompt, "status": "running",
                "quiet": quiet, "workspace": workspace, "started": 1.0, "result": None}
        self.items.append(item)
        return item


class Clock:
    def __init__(self):
        import time
        self.t = time.time()

    def __call__(self):
        return self.t


WS = [{"id": "acme", "privacy": "normal"}, {"id": "vault", "privacy": "walled"}]


async def start_node(tmp_path, name, clock, priority=50):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    cfg = Config()
    cfg.cluster.name, cfg.cluster.priority = name, priority
    data, home = tmp_path / name / "data", tmp_path / name / "home"
    data.mkdir(parents=True)
    home.mkdir(parents=True)
    tasks = FakeTasks()
    node = C.ClusterNode(cfg, C.ClusterStore(data / "cluster.json"), tasks=tasks, on_finish=tasks.on_finish,
                         workspaces=lambda: WS, clock=clock, data=data, home=home)
    app = web.Application()
    C.add_routes(app, cfg, node)
    srv = TestServer(app, host="127.0.0.1")
    await srv.start_server()
    cfg.cluster.url = f"http://127.0.0.1:{srv.port}"
    node.srv = srv
    return node


async def join(lead, new):
    lead.persist()
    code = lead.store.start_pairing()["code"]
    new.persist()
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: C.join(lead.my_url(), code, new.me["name"], new.my_url(), 50, new.store))
    new.tick()


async def beat(*nodes, rounds=2):
    for _ in range(rounds):
        for n in nodes:
            await n.heartbeat_once()


async def stop(*nodes):
    for n in nodes:
        if n.session is not None:
            await n.session.close()
        await n.srv.close()


def run(coro):
    return asyncio.run(coro)


def test_join_elect_sync_and_brain_list(tmp_path):
    async def go():
        clock = Clock()
        a = await start_node(tmp_path, "Wall", clock, priority=60)
        b = await start_node(tmp_path, "Office", clock)
        assert a.tick() == "solo" and not (tmp_path / "Wall" / "data" / "cluster.json").exists()
        (a.shared.home / "memory.md").write_text("# memory\n- the owner likes tea\n")
        await join(a, b)
        assert a.role == "lead" and b.store.key_for(a.me["id"])
        await beat(a, b)
        assert b.role == "follower" and b.election.leader == a.me["id"]
        # pull
        r = await b.sync_once()
        assert "home/memory.md" in r["pulled"]
        assert (b.shared.home / "memory.md").read_text().endswith("likes tea\n")
        # push a follower edit, the lead takes it
        (b.shared.home / "memory.md").write_text("# memory\n- the owner likes coffee now\n")
        r = await b.sync_once()
        assert r["pushed"] == ["home/memory.md"]
        assert "coffee" in (a.shared.home / "memory.md").read_text()
        # brain list for faces: the lead first, both installs
        fh = MagicMock()
        assert b.brain_urls()[0] == a.my_url() and set(b.brain_urls()) == {a.my_url(), b.my_url()}
        from walter.face_server import FaceHub
        fh = FaceHub.__new__(FaceHub)
        fh.cluster = b
        assert FaceHub.brains(fh)["lead"] == a.my_url()
        await stop(a, b)
    run(go())


def test_third_install_gets_keys_through_the_lead_and_failover(tmp_path):
    async def go():
        clock = Clock()
        a = await start_node(tmp_path, "Wall", clock, priority=60)
        b = await start_node(tmp_path, "Office", clock, priority=50)
        c = await start_node(tmp_path, "Garage", clock, priority=40)
        await join(a, b)
        await beat(a, b)
        await join(a, c)
        await beat(a, b, c, rounds=3)
        assert b.store.key_for(c.me["id"]) and c.store.key_for(b.me["id"])
        assert b.store.key_for(c.me["id"]) == c.store.key_for(b.me["id"])
        assert {b.role, c.role} == {"follower"}
        # the lead goes away: after 3 missed heartbeats (9 s) B takes over
        await a.srv.close()
        for _ in range(4):
            clock.t += 3
            await beat(b, c, rounds=1)
        assert b.role == "lead" and c.role == "follower" and c.election.leader == b.me["id"]
        assert c.brain_urls()[0] == b.my_url()
        await stop(b, c)
        if a.session is not None:
            await a.session.close()
    run(go())


def test_peer_routes_refuse_unsigned_and_wrong_keys(tmp_path):
    async def go():
        import aiohttp
        clock = Clock()
        a = await start_node(tmp_path, "Wall", clock)
        b = await start_node(tmp_path, "Office", clock)
        await join(a, b)
        async with aiohttp.ClientSession() as s:
            for path in ("/api/cluster/heartbeat", "/api/cluster/snapshot", "/api/cluster/write",
                         "/api/cluster/job", "/api/cluster/leave"):
                async with s.post(a.my_url() + path, json={}) as r:
                    assert r.status == 401, path
            body = json.dumps({"key": "home/memory.md", "content": "evil"}).encode()
            h = C.signed_headers(b.me["id"], "x" * 43, "POST", "/api/cluster/write", body)
            async with s.post(a.my_url() + "/api/cluster/write", data=body, headers=h) as r:
                assert r.status == 401
            # wrong join code
            async with s.post(a.my_url() + "/api/cluster/join",
                              json={"code": "000000", "node": {"id": "n-dddddddddddd", "url": "http://127.0.0.1:1"}}) as r:
                assert r.status == 403
        await stop(a, b)
    run(go())


def test_gate_keeps_cluster_off_the_tunnel():
    from walter.server import gate_ok
    cfg = Config()
    assert gate_ok(cfg, "/api/cluster/heartbeat", "192.168.1.9", {})
    assert gate_ok(cfg, "/api/cluster/heartbeat", "100.101.1.2", {})
    assert not gate_ok(cfg, "/api/cluster/heartbeat", "8.8.8.8", {})
    assert not gate_ok(cfg, "/api/cluster/heartbeat", "127.0.0.1", {"X-Forwarded-For": "8.8.8.8"})
    cfg.cluster.enabled = False
    assert not gate_ok(cfg, "/api/cluster/heartbeat", "192.168.1.9", {})


def test_remote_job_placement_lead_only_and_fallback(tmp_path):
    async def go():
        clock = Clock()
        a = await start_node(tmp_path, "Wall", clock, priority=60)
        b = await start_node(tmp_path, "Office", clock)
        await join(a, b)
        await beat(a, b)
        # make B clearly the best node
        a.caps[b.me["id"]] = {"cores": 64, "load1": 0, "mem_free_mb": 64000, "max_tasks": 3, "running": 0,
                              "jobs": True}
        r = await a.submit("Research", "find things", None, "best")
        assert r["node"] == b.me["id"] and b.tasks.items[0]["title"].startswith("[Wall] Research")
        assert b.tasks.items[0]["quiet"] is True        # B doesn't speak it; A gets the report
        # finish on B; A's poll picks it up and reports it like a local worker
        b.tasks.items[0].update(status="done", result="found 3 things")
        await a.poll_jobs()
        item = a.tasks.on_finish.call_args[0][0]
        assert item["status"] == "done" and "found 3 things" in item["result"] and "Office" in item["title"]
        # a follower can't hand out jobs: B -> A /api/cluster/job is refused
        with pytest.raises(RuntimeError, match="403"):
            await b.request(b.store.peers()[a.me["id"]], "POST", "/api/cluster/job",
                            {"job": b.me["id"] + ":R123456", "title": "x", "prompt": "rm -rf /"})
        assert a.tasks.items == []
        # a follower asks the lead to place a job; the lead picks itself when B is "full"
        a.caps[b.me["id"]]["running"] = 3
        r = await b.submit("Draft", "write a draft", None, "best")
        assert r["node"] == a.me["id"] and a.tasks.items[-1]["title"].startswith("[Office] Draft")
        a.tasks.items[-1].update(status="done", result="draft ready")
        await b.poll_jobs()
        assert "draft ready" in b.tasks.on_finish.call_args[0][0]["result"]
        # walled workspace never goes to an unassigned node
        with pytest.raises(PermissionError):
            await a.submit("Secret", "x", "vault", "Office")
        # the target is unreachable: falls back to running here
        await b.srv.close()
        a.caps[b.me["id"]]["running"] = 0
        r = await a.submit("Fallback", "x", None, "Office")
        assert r["node"] == a.me["id"] and "fell back" in r["note"]
        await stop(a)
        if b.session is not None:
            await b.session.close()
    run(go())


def test_leave_removes_keys_on_both_sides(tmp_path):
    async def go():
        from aiohttp.test_utils import TestClient
        clock = Clock()
        a = await start_node(tmp_path, "Wall", clock)
        b = await start_node(tmp_path, "Office", clock)
        await join(a, b)
        await beat(a, b)
        # B leaves through its local endpoint (as walterctl does)
        async with TestClient(b.srv) as cl:
            r = await cl.post("/api/cluster/local/leave", json={})
            assert r.status == 200
        assert b.store.peers() == {} and b.role == "solo"
        assert a.store.key_for(b.me["id"]) is None
        await stop(a, b)
    run(go())
