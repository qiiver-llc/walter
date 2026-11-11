"""Workspaces scoping: worker tags + briefs (walterctl task), the task record, the cockpit board and
git lanes, `walterctl board`, the loop chip, and walled-workspace checks."""
import asyncio
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from walter import cockpit, loop as L, workspaces as W

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _example_projects(example_workspaces):
    """Home + Acme (normal) + Vault (walled): generic example projects (conftest.py)."""
    return example_workspaces



def seeded(tmp_path, backlogs=None):
    st = W.WorkspaceStore()
    st.ensure()
    # keep the real ~/.walter backlog out of tests: point acme at a temp one
    st.set("acme", "backlog", str(tmp_path / "acme-backlog.md"))
    for wid, text in (backlogs or {}).items():
        Path(W.expand(st.get(wid)["backlog"])).write_text(text)
    return st


def write_tasks(tmp_path, items):
    p = tmp_path / "tasks.json"
    p.write_text(json.dumps(items))
    return p


# ---------------------------------------------------------------- cockpit / board

def test_board_unchanged_without_workspaces(tmp_path):
    bl = tmp_path / "b.md"
    bl.write_text("- [ready] **Alpha**: x\n")
    tasks = write_tasks(tmp_path, [{"id": 1, "title": "W1", "status": "running"}])
    assert cockpit.scope() is None
    b = cockpit.task_board(backlog=bl, tasks=tasks, workspace=cockpit.scope())
    assert [c["title"] for c in b["ready"]] == ["Alpha"]
    assert [c["title"] for c in b["building"]] == ["W1"]


def test_board_shows_only_the_current_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(cockpit, "IDS_FILE", tmp_path / "ids.json")
    st = seeded(tmp_path, {"acme": "- [ready] **Installer polish**: x\n",
                           "vault": "- [ready] **Network app follow-up**: y\n",
                           "home": "- [ready] **Gutter quote**: z\n"})
    tasks = write_tasks(tmp_path, [
        {"id": 1, "title": "Old worker", "status": "running"},                 # untagged (before workspaces)
        {"id": 2, "title": "Site copy", "status": "running", "workspace": "acme"},
        {"id": 3, "title": "Private draft", "status": "running"},
    ])
    st.tag_task(3, "vault")                                              # sidecar tag
    st.switch("acme")
    ws = cockpit.scope()
    assert ws["id"] == "acme" and [l for l, _ in ws["repos"]] == ["Walter", "Site"]
    b = cockpit.task_board(tasks=tasks, workspace=ws)
    assert [c["title"] for c in b["ready"]] == ["Installer polish"]
    assert [c["title"] for c in b["building"]] == ["Old worker", "Site copy"]
    st.switch("vault work")
    b = cockpit.task_board(tasks=tasks, workspace=cockpit.scope())
    assert [c["title"] for c in b["ready"]] == ["Network app follow-up"]
    assert [c["title"] for c in b["building"]] == ["Private draft"]              # untagged hidden when walled
    assert cockpit.scope()["repos"] == []
    st.switch("home")
    b = cockpit.task_board(tasks=tasks, workspace=cockpit.scope())
    assert [c["title"] for c in b["ready"]] == ["Gutter quote"]
    assert "Private draft" not in [c["title"] for c in b["building"]]


def test_scope_drops_walled_repos_from_other_workspaces(tmp_path):
    st = seeded(tmp_path)
    secret = tmp_path / "lh-repo"
    secret.mkdir()
    st.set("vault", "repos", str(secret))
    st.set("home", "repos", f"~/walter,{secret}")   # a mistake: home lists a walled repo
    st.switch("home")
    assert [str(p) for _, p in cockpit.scope()["repos"]] == [str(W.expand("~/walter"))]


def test_cockpit_lanes_follow_the_workspace(tmp_path, monkeypatch):
    seeded(tmp_path).switch("vault")
    monkeypatch.setattr(cockpit, "remote_release", lambda now=None: None)
    monkeypatch.setattr(cockpit, "IDS_FILE", tmp_path / "ids.json")
    out = cockpit.cockpit(words=[])
    assert out["lanes"] == [] and out["workspace"] == {"id": "vault", "name": "Vault",
                                                       "walled": True}


def test_board_lookup_uses_workspace_backlog(tmp_path, monkeypatch):
    monkeypatch.setattr(cockpit, "IDS_FILE", tmp_path / "ids.json")
    st = seeded(tmp_path, {"home": "- [ready] **Gutter quote**: call two roofers\n"})
    st.switch("home")
    b = cockpit.task_board(workspace=cockpit.scope(), tasks=write_tasks(tmp_path, []))
    num = b["ready"][0]["num"]
    assert cockpit.board_lookup(f"T{num}")["raw"].startswith("**Gutter quote**")
    st.switch("acme")
    assert cockpit.board_lookup(f"T{num}") is None


# ---------------------------------------------------------------- loop chip

def test_loop_chip_prefix(tmp_path):
    ls = L.LoopStore(tmp_path / "loop.json", tmp_path / "face.json")
    ls.set_focus("Beta installer")
    assert json.loads((tmp_path / "face.json").read_text())["focus"] == "Beta installer"   # no workspaces
    seeded(tmp_path).switch("acme corp")
    ls.set_focus("Waitlist site")
    face = json.loads((tmp_path / "face.json").read_text())
    assert face["focus"] == "Acme: Waitlist site" and face["workspace"] == "Acme"
    assert L.format_status(ls.state()).startswith("Focus: Acme: Waitlist site")


# ---------------------------------------------------------------- task record

def test_task_record_keeps_workspace(tmp_path, monkeypatch):
    from test_tasks import FAKE, FakeSystemd, wait_for
    import walter.tasks as tasks_mod
    from walter.config import Config
    from walter.tasks import Tasks
    monkeypatch.setattr(Config, "data_dir", property(lambda self: tmp_path / "data"))
    monkeypatch.setattr(tasks_mod, "POLL_S", 0.05)
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "0.1")
    cfg = Config()
    cfg.brain.claude_bin = str(FAKE)
    cfg.brain.home = str(tmp_path)

    async def go():
        done = []
        t = Tasks(cfg, done.append, lambda n: None, launcher=FakeSystemd())
        item = await t.start("x", "y", workspace="acme")
        assert item["workspace"] == "acme"
        plain = await t.start("x", "y")
        assert "workspace" not in plain
        assert await wait_for(lambda: len(done) == 2)
    asyncio.run(go())


# ---------------------------------------------------------------- walterctl task / tasks / board / workspace

class _Fake(BaseHTTPRequestHandler):
    posted = []
    items = []

    def log_message(self, *a):
        pass

    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Fake.posted.append(d)
        item = {"id": 40 + len(_Fake.posted), "title": d["title"], "status": "running", "started": 1e9}
        _Fake.items.append(item)       # like a server from before the restart: no workspace field
        self._send(item)

    def do_GET(self):
        self._send(_Fake.items)


@pytest.fixture
def server():
    _Fake.posted, _Fake.items = [], []
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def ctl(tmp_path, url, *args):
    env = dict(os.environ, WALTER_DATA=str(tmp_path / "data"), WALTER_URL=url, WALTER_FACE="wall")
    code = ("import sys, runpy; from pathlib import Path; import walter.dashboard as D;"
            f"D.FACE_DIR = Path({str(tmp_path / 'face')!r});"
            f"sys.argv = ['walterctl'] + sys.argv[1:]; runpy.run_path({str(REPO / 'bin' / 'walterctl')!r}, run_name='__main__')")
    return subprocess.run([sys.executable, "-c", code, *args], env=env, cwd=REPO, capture_output=True,
                          text=True, timeout=30)


def test_cli_task_without_workspaces_is_unchanged(tmp_path, server):
    r = ctl(tmp_path, server, "task", "Fix it", "do the thing")
    assert r.stdout.strip() == "task #41 started: Fix it"
    assert _Fake.posted == [{"title": "Fix it", "prompt": "do the thing"}]
    assert not (tmp_path / "data" / "workspaces.json").exists()


def test_cli_workspace_and_task_tagging(tmp_path, server, example_workspaces):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "workspaces.json").write_text(json.dumps(example_workspaces))   # the CLI runs in its own process
    r = ctl(tmp_path, server, "workspace", "list")
    assert "* home" in r.stdout and "vault" in r.stdout and "walled" in r.stdout
    assert (tmp_path / "data" / "home" / "workspaces" / "vault" / "notes.md").exists()
    assert "now in Acme" in ctl(tmp_path, server, "workspace", "switch", "acme corp").stdout
    assert ctl(tmp_path, server, "workspace", "current").stdout.startswith("Acme (acme)")
    assert ctl(tmp_path, server, "workspace", "current", "--face", "kitchen").stdout.startswith("Home")
    r = ctl(tmp_path, server, "task", "Site copy", "write the hero text")
    assert r.stdout.strip() == "task #41 started: Site copy [acme]"
    sent = _Fake.posted[-1]
    assert sent["workspace"] == "acme" and sent["prompt"].startswith("[Workspace: Acme")
    assert sent["prompt"].endswith("write the hero text") and "Repos: ~/walter, ~/walter-site." in sent["prompt"]
    r = ctl(tmp_path, server, "task", "--workspace", "the vault", "Draft", "draft the reply")
    assert r.stdout.strip().endswith("[vault]")
    assert "WALLED" in _Fake.posted[-1]["prompt"]
    out = ctl(tmp_path, server, "tasks").stdout
    assert "[acme] Site copy" in out and "[vault] Draft" in out      # sidecar tags (pre-restart server)
    assert "no workspace" in ctl(tmp_path, server, "task", "--workspace", "garage", "x", "y").stderr
    r = ctl(tmp_path, server, "workspace", "set", "vault", "google_account", "owner@example.com")
    assert r.returncode == 0
    assert "owner@example.com" in ctl(tmp_path, server, "workspace", "show", "vault work").stdout
    assert "added Farm" in ctl(tmp_path, server, "workspace", "add", "Farm").stdout
    face = json.loads((tmp_path / "face" / "dashboard-workspace.json").read_text())
    assert face["id"] == "acme"


def test_cli_board_shows_workspace(tmp_path, server):
    ctl(tmp_path, server, "workspace", "switch", "home")
    bl = tmp_path / "data" / "home" / "workspaces" / "home" / "backlog.md"
    bl.write_text(bl.read_text() + "- [ready] **Gutter quote**: z\n")
    out = ctl(tmp_path, server, "board").stdout
    assert out.startswith("[Home]") and "Gutter quote" in out
