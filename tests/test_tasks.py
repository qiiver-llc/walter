"""Background workers: launched detached, survive a restart (re-adoption), and
report exactly like before. Uses the real taskrun.py runner and fake_claude.py,
with a stand-in for systemd so no real units are created."""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

import walter.tasks as tasks_mod
from walter.config import Config
from walter.tasks import DirectLauncher, Tasks

FAKE = Path(__file__).with_name("fake_claude.py")


class FakeSystemd(DirectLauncher):
    """Pretends to be the systemd launcher: same kind, so items say "systemd",
    and its processes outlive any one Tasks instance (like a real unit)."""
    kind = "systemd"

    def __init__(self, fail=False):
        super().__init__()
        self.fail = fail
        self.launched = []
        self.stopped = []

    async def launch(self, tid, runner_cmd, timeout):
        if self.fail:
            raise RuntimeError("no user bus")
        self.launched.append(tid)
        await super().launch(tid, runner_cmd, timeout)

    def stop(self, tid, pid):
        self.stopped.append(tid)
        super().stop(tid, pid)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "data_dir", property(lambda self: tmp_path / "data"))
    monkeypatch.setattr(tasks_mod, "POLL_S", 0.05)
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "0.3")
    cfg = Config()
    cfg.brain.claude_bin = str(FAKE)
    cfg.brain.home = str(tmp_path)
    return cfg, tmp_path


def make(cfg, launcher=None):
    finished, counts = [], []
    t = Tasks(cfg, finished.append, counts.append, launcher=launcher or FakeSystemd())
    return t, finished, counts


async def wait_for(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


def test_worker_reports_like_before(env, monkeypatch):
    cfg, tmp = env
    idfile = tmp / "seen"
    monkeypatch.setenv("FAKE_CLAUDE_TASK_ID_FILE", str(idfile))

    async def go():
        t, finished, counts = make(cfg)
        item = await t.start("find flights", "compare these flights")
        assert item["id"] == 1 and item["launcher"] == "systemd" and t.running == 1
        assert counts[-1] == 1
        assert await wait_for(lambda: finished)
        assert finished[0]["status"] == "done"
        assert "Finished 'find flights'" in finished[0]["result"]
        assert counts[-1] == 0
        saved = json.loads((tmp / "data" / "tasks.json").read_text())
        assert saved[0]["status"] == "done"
        # same environment as before: task id and Walter's home as cwd
        assert idfile.read_text() == f"1 {tmp}"
        d = tmp / "data" / "tasks"
        assert oct(d.stat().st_mode & 0o777) == "0o700"
        assert (d / "1.out").exists() and not (d / "1.spec").exists() and not (d / "1.rc").exists()
    asyncio.run(go())


def test_survives_restart_and_is_readopted(env, monkeypatch):
    cfg, _ = env
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "1.0")
    sysd = FakeSystemd()

    async def go():
        t1, fin1, _ = make(cfg, sysd)
        await t1.start("long job", "do it")
        await asyncio.sleep(0.2)
        await t1.close()                       # service stops: must not kill the worker
        assert sysd.stopped == []
        assert await sysd.alive(1, None)
        t2, fin2, counts2 = make(cfg, sysd)      # service starts again
        assert t2.running == 1                   # still "running", not "lost"
        await asyncio.sleep(0)                   # let the scheduled adopt() run
        assert counts2 and counts2[-1] == 1
        assert await wait_for(lambda: fin2)
        assert fin1 == [] and fin2[0]["status"] == "done"
        assert "Finished 'long job'" in fin2[0]["result"]
    asyncio.run(go())


def test_finished_while_away(env):
    cfg, tmp = env

    async def go():
        t1, _, _ = make(cfg)
        await t1.start("quick", "x")
        await t1.close()
        await asyncio.sleep(1.0)                 # ends while nobody is watching
        t2, fin2, _ = make(cfg, FakeSystemd())   # fresh launcher: knows nothing about it
        assert await wait_for(lambda: fin2)
        assert fin2[0]["status"] == "done" and "Finished 'quick'" in fin2[0]["result"]
    asyncio.run(go())


def test_unit_gone_with_json_but_no_rc(env):
    cfg, tmp = env
    d = tmp / "data" / "tasks"
    d.mkdir(parents=True)
    (d / "7.out").write_text('noise\n{"type":"result","is_error":true,"result":"it broke"}\n')
    (tmp / "data" / "tasks.json").write_text(json.dumps([
        {"id": 7, "title": "t", "prompt": "p", "status": "running", "started": time.time(),
         "finished": None, "result": None, "launcher": "systemd"}]))

    async def go():
        t, fin, _ = make(cfg)
        assert await wait_for(lambda: fin)
        assert fin[0]["status"] == "failed" and fin[0]["result"] == "it broke"
    asyncio.run(go())


def test_unit_gone_without_result_is_lost(env):
    cfg, tmp = env
    (tmp / "data").mkdir()
    (tmp / "data" / "tasks.json").write_text(json.dumps([
        {"id": 3, "title": "t", "prompt": "p", "status": "running", "started": time.time(),
         "finished": None, "result": None}]))     # old-style item, no launcher key

    async def go():
        t, fin, counts = make(cfg)
        assert await wait_for(lambda: fin)
        assert fin[0]["status"] == "failed" and fin[0]["result"].startswith("worker lost")
        assert counts[-1] == 0
    asyncio.run(go())


def test_empty_report(env, tmp_path):
    cfg, _ = env
    script = tmp_path / "quiet.sh"
    script.write_text('#!/bin/sh\necho \'{"result": ""}\'\n')
    script.chmod(0o755)
    cfg.brain.claude_bin = str(script)

    async def go():
        t, fin, _ = make(cfg)
        await t.start("q", "q")
        assert await wait_for(lambda: fin)
        assert fin[0]["status"] == "done" and fin[0]["result"] == "(no report)"
    asyncio.run(go())


def test_nonzero_exit_without_json(env, tmp_path):
    cfg, _ = env
    script = tmp_path / "bad.sh"
    script.write_text('#!/bin/sh\necho "boom" >&2\nexit 2\n')
    script.chmod(0o755)
    cfg.brain.claude_bin = str(script)

    async def go():
        t, fin, _ = make(cfg)
        await t.start("b", "b")
        assert await wait_for(lambda: fin)
        assert fin[0]["status"] == "failed" and "boom" in fin[0]["result"]
    asyncio.run(go())


def test_cancel_stops_worker(env, monkeypatch):
    cfg, _ = env
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    sysd = FakeSystemd()

    async def go():
        t, fin, counts = make(cfg, sysd)
        await t.start("slow", "s")
        await asyncio.sleep(0.3)
        assert t.cancel(1) and sysd.stopped == [1]
        assert t.items[-1]["status"] == "cancelled" and counts[-1] == 0
        assert await wait_for(lambda: not sysd.procs.get(1) or sysd.procs[1].poll() is not None, 5)
        await asyncio.sleep(0.2)
        assert fin == []                          # cancelled tasks don't report, as before
        assert not t.cancel(1)
    asyncio.run(go())


def test_timeout(env, monkeypatch):
    cfg, _ = env
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    cfg.brain.task_timeout = 1
    sysd = FakeSystemd()

    async def go():
        t, fin, _ = make(cfg, sysd)
        await t.start("slow", "s")
        assert await wait_for(lambda: fin, 5)
        assert fin[0]["status"] == "failed" and fin[0]["result"] == "timed out after 1s"
        assert sysd.stopped == [1]
    asyncio.run(go())


def test_max_tasks(env, monkeypatch):
    cfg, _ = env
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    cfg.brain.max_tasks = 1

    async def go():
        t, _, _ = make(cfg)
        await t.start("a", "a")
        with pytest.raises(RuntimeError):
            await t.start("b", "b")
        t.cancel(1)
    asyncio.run(go())


def test_falls_back_when_systemd_run_fails(env):
    cfg, _ = env

    async def go():
        t, fin, _ = make(cfg, FakeSystemd(fail=True))
        item = await t.start("fb", "x")
        assert item["launcher"] == "direct"
        assert await wait_for(lambda: fin)
        assert fin[0]["status"] == "done"
    asyncio.run(go())


def test_systemd_launcher_command(monkeypatch):
    calls = []

    async def fake_run(self, *args):
        calls.append(args)
        return (0, "active") if args[0] == "systemctl" else (0, "")
    monkeypatch.setattr(tasks_mod.SystemdLauncher, "_run", fake_run)
    l = tasks_mod.SystemdLauncher()

    async def go():
        await l.launch(5, ["py", "taskrun.py", "spec"], 3600)
        assert await l.alive(5, None)
    asyncio.run(go())
    a = calls[0]
    assert a[:3] == ("systemd-run", "--user", "--collect")
    assert "--unit=walter-task-5.service" in a
    assert a[a.index("--") + 1:] == ("py", "taskrun.py", "spec")
    assert calls[1][-1] == "walter-task-5.service"


def test_systemd_launcher_low_priority_props(monkeypatch):
    calls = []

    async def fake_run(self, *args):
        calls.append(args)
        return 0, ""
    monkeypatch.setattr(tasks_mod.SystemdLauncher, "_run", fake_run)
    l = tasks_mod.SystemdLauncher()
    asyncio.run(l.launch(7, ["py", "x"], 60))
    a = calls[0]
    head = a[:a.index("--")]
    assert "--property=CPUWeight=20" in head and "--property=IOWeight=20" in head
    assert "--property=Nice=10" in head


def test_worker_priority_clamps_and_defaults():
    from types import SimpleNamespace
    assert tasks_mod.worker_priority(None) == {"cpu_weight": 20, "io_weight": 20, "nice": 10}
    b = SimpleNamespace(worker_cpu_weight=0, worker_io_weight="bad", worker_nice=99)
    assert tasks_mod.worker_priority(b) == {"cpu_weight": 1, "io_weight": 20, "nice": 19}


def test_start_reads_priority_from_config_file(env):
    cfg, tmp = env
    (tmp / "data").mkdir(exist_ok=True)
    (tmp / "data" / "config.toml").write_text("[brain]\nworker_cpu_weight = 50\nworker_nice = 5\n")
    seen = []

    class Rec(FakeSystemd):
        async def launch(self, tid, runner_cmd, timeout):
            seen.append(dict(self.priority))
            await super().launch(tid, runner_cmd, timeout)

    async def go():
        t, _, _ = make(cfg, Rec())
        item = await t.start("x", "y")
        assert item["priority"] == {"cpu_weight": 50, "io_weight": 20, "nice": 5}
        assert await wait_for(lambda: t.running == 0)
    asyncio.run(go())
    assert seen == [{"cpu_weight": 50, "io_weight": 20, "nice": 5}]


def test_quiet_task_and_model_override(env):
    cfg, _ = env
    cfg.brain.task_model = "opus"

    async def go():
        t, finished, _ = make(cfg)
        assert "opus" in t._cmd("x", "y")
        cmd = t._cmd("x", "y", "sonnet")
        assert cmd[cmd.index("--model") + 1] == "sonnet"
        item = await t.start("sleep review", "look at logs", quiet=True, model="sonnet")
        assert item["quiet"] is True
        assert await wait_for(lambda: finished)
        assert finished[0]["quiet"] is True
        plain = await t.start("normal", "job")
        assert "quiet" not in plain
    asyncio.run(go())
