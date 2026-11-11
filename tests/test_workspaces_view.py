"""Workspaces view in the build cockpit (owner approved 11 Nov 09:37): the per-workspace data behind the
pill bar and the "projects" zoom, the walled-workspace privacy rule (counts only), the cockpit following
the current workspace, the tap-to-switch endpoint and the "projects" zoom whitelists."""
import json
import re
import subprocess
import time
from pathlib import Path

import pytest

from walter import app_mode, buildview, cockpit, present, server, workspaces as W

REPO = Path(__file__).resolve().parents[1]
SECRETS = ("Secret intake form", "Private draft letter", "Hidden focus words", "Confidential commit subject",
           "privaterepo")


@pytest.fixture(autouse=True)
def _example_projects(example_workspaces):
    """Home + Acme (normal) + Vault (walled): generic example projects (conftest.py)."""
    return example_workspaces



def git_repo(path: Path, subject: str) -> Path:
    path.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)
    run("init", "-q")
    (path / "f.txt").write_text("x\n")
    run("add", "f.txt")
    run("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", subject)
    return path


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Three workspaces; the walled one has a backlog, a running worker, the loop focus and a repo, all
    with words that must never show up anywhere outside it."""
    monkeypatch.setattr(cockpit, "IDS_FILE", tmp_path / "ids.json")
    st = W.WorkspaceStore()
    st.ensure()
    st.set("acme", "backlog", str(tmp_path / "acme-backlog.md"))
    today = time.strftime("%Y-%m-%d")
    Path(W.expand(st.get("acme")["backlog"])).write_text(
        "- [ready] **Installer polish**: x\n- [ready] **Site copy**: y\n"
        f"- [done {today} 09:00] **Pill bar**: z\n- [done 2020-01-01 09:00] **Old**: z\n- [done] **Undated**: z\n")
    Path(W.expand(st.get("vault")["backlog"])).write_text(
        f"- [ready] **{SECRETS[0]}**: details\n- [in progress #9] **Another one**: x\n")
    priv = git_repo(tmp_path / "privaterepo", SECRETS[3])
    st.set("vault", "repos", str(priv))
    pub = git_repo(tmp_path / "pubrepo", "Public commit subject")
    st.set("acme", "repos", str(pub))
    tasks = [{"id": 1, "title": SECRETS[1], "status": "running", "workspace": "vault"},
             {"id": 2, "title": "Pill bar worker", "status": "running", "workspace": "acme"},
             {"id": 3, "title": "Queued thing", "status": "queued", "workspace": "acme"},
             {"id": 4, "title": "Old untagged", "status": "running"},
             {"id": 5, "title": "Done today", "status": "done", "workspace": "acme", "finished": time.time()}]
    loop = {"focus": SECRETS[2], "focus_ws": "Vault"}
    st.switch("acme")
    return {"st": st, "tasks": tasks, "loop": loop, "tmp": tmp_path}


def overview(world, **kw):
    return cockpit.workspaces_overview(store=world["st"], tasks=world["tasks"], loop=world["loop"], words=[], **kw)


def by_id(items):
    return {w["id"]: w for w in items}


# ---------------------------------------------------------------- data

def test_no_workspaces_no_pills():
    assert cockpit.workspaces_overview(tasks=[], loop={}) == []


def test_every_workspace_with_badges(world):
    o = by_id(overview(world))
    assert list(o) == ["home", "acme", "vault"]
    q = o["acme"]
    assert q["current"] and not q["walled"]
    # running: its tagged worker + the untagged older one (the board shows those in non-walled workspaces)
    assert q["badges"] == {"running": 2, "queued": 1, "ready": 2, "done_today": 1}
    assert q["last_commit"]["subject"] == "Public commit subject" and q["last_commit"]["ago"]
    assert o["home"]["badges"]["running"] == 0          # untagged workers count only where they show
    assert o["vault"]["badges"] == {"running": 1, "queued": 0, "ready": 1, "done_today": 0}
    assert o["vault"]["walled"] and not o["vault"]["current"]
    shares = [w["budget_share"] for w in o.values()]
    assert abs(sum(shares) - 1) < 0.01
    assert o["acme"]["usage_share"] == pytest.approx(0.75)   # 3 of the 4 tagged recent workers


def test_walled_workspace_exposes_counts_only(world):
    items = overview(world)
    lh = by_id(items)["vault"]
    assert lh["focus"] is None and lh["last_commit"] is None
    assert set(lh) == {"id", "name", "current", "walled", "badges", "usage_share", "budget_share", "focus",
                       "last_commit"}
    blob = json.dumps(items)
    for s in SECRETS:
        assert s.lower() not in blob.lower(), s


def test_walled_workspace_shows_its_own_details_when_current(world):
    world["st"].switch("vault")
    lh = by_id(overview(world))["vault"]
    assert lh["current"]
    assert lh["focus"] == SECRETS[2]
    assert lh["last_commit"]["subject"] == SECRETS[3]
    # and nothing of the walled one leaks into the others' cards either way
    q = by_id(overview(world))["acme"]
    assert q["focus"] is None and q["badges"]["running"] == 1   # untagged workers hide in a walled view


def test_cockpit_data_file_never_carries_walled_words(world, monkeypatch):
    monkeypatch.setattr(cockpit, "remote_release", lambda now=None: None)
    monkeypatch.setattr(cockpit, "site_deploy", lambda: None)
    monkeypatch.setattr(cockpit, "DATA", world["tmp"])
    (world["tmp"] / "tasks.json").write_text(json.dumps(world["tasks"]))
    (world["tmp"] / "loop.json").write_text(json.dumps(world["loop"]))
    out = cockpit.cockpit(words=[])
    assert [w["id"] for w in out["workspaces"]] == ["home", "acme", "vault"]
    blob = json.dumps(out)
    for s in SECRETS:
        assert s.lower() not in blob.lower(), s


def test_backlog_counts(tmp_path):
    p = tmp_path / "b.md"
    p.write_text("- [ready] **A**: x\n- [in progress #4] **B**\n- [done 2026-11-11 08:00] **C**\n- [done] **D**\n")
    assert cockpit.backlog_counts(p, "2026-11-11") == {"ready": 1, "building": 1, "done_today": 1}
    assert cockpit.backlog_counts(tmp_path / "missing.md", "2026-11-11") == {"ready": 0, "building": 0,
                                                                            "done_today": 0}


# ---------------------------------------------------------------- the cockpit follows the workspace

def test_snapshot_follows_the_workspace(world, monkeypatch):
    monkeypatch.setattr(cockpit, "remote_release", lambda now=None: None)
    monkeypatch.setattr(cockpit, "DATA", world["tmp"])
    snap = buildview.snapshot(words=[])
    assert [c["line"] for c in snap["shipped"]] == ["Public commit subject"]
    assert [ln["repo"] for ln in snap["cockpit"]["lanes"]] == ["Pubrepo"]
    # a test run of another repo (the Walter repo by default) stays out of this workspace's cockpit
    (world["tmp"] / "last-test.json").write_text(json.dumps({"passed": 3, "failed": 0, "ok": True,
                                                             "repo": str(REPO)}))
    snap = buildview.snapshot(words=[])
    assert snap["cockpit"]["tests"] is None and snap["cockpit"]["tests_hidden"] is True
    (world["tmp"] / "last-test.json").write_text(json.dumps({"passed": 3, "ok": True,
                                                             "repo": str(world["tmp"] / "pubrepo")}))
    snap = buildview.snapshot(words=[])
    assert snap["cockpit"]["tests"]["passed"] == 3 and not snap["cockpit"]["tests_hidden"]


def test_snapshot_in_a_workspace_without_repos_shows_none(world, monkeypatch):
    monkeypatch.setattr(cockpit, "remote_release", lambda now=None: {"version": "9.9"})
    monkeypatch.setattr(cockpit, "DATA", world["tmp"])
    world["st"].switch("home")
    snap = buildview.snapshot(words=[])
    assert snap["shipped"] == [] and snap["repo"] is None and snap["cockpit"]["lanes"] == []
    # Home has no repos: no ship pipeline at all (the Walter release isn't Home's)
    assert snap["cockpit"]["pipeline"] == [] and snap["cockpit"]["pipeline_preset"] == "none"


def test_snapshot_unchanged_without_workspaces(monkeypatch):
    assert buildview.scoped_repos() is None


def test_face_test_mirror_drops_the_repo_path(tmp_path):
    cockpit._pub(tmp_path / "last.json", {"passed": 1, "repo": "/somewhere/repo", "failures": []},
                 face=tmp_path / "face.json")
    assert json.loads((tmp_path / "last.json").read_text())["repo"] == "/somewhere/repo"
    assert "repo" not in json.loads((tmp_path / "face.json").read_text())


# ---------------------------------------------------------------- tap to switch

def test_switch_request_whitelist(world):
    st = world["st"]
    for body in (None, [], {}, {"id": 3}, {"id": ""}, {"id": "../etc"}, {"id": "Q" * 80}, {"id": "Acme"}):
        status, out = W.switch_request(body, store=st)
        assert status == 400 and "error" in out
    assert W.switch_request({"id": "akme"}, store=st)[0] == 400       # an alias, not an id
    assert W.switch_request({"id": "garage"}, store=st)[0] == 400
    assert st.current_id() == "acme"
    status, out = W.switch_request({"id": "vault"}, store=st)
    assert status == 200 and out == {"ok": True, "id": "vault", "name": st.get("vault")["name"],
                                     "walled": True, "face": "wall"}
    assert st.current_id() == "vault"
    pub = json.loads(Path(W.WorkspaceStore.DEFAULT_FACE_OUT).read_text())
    assert pub["id"] == "vault"


def test_switch_request_without_workspaces():
    assert W.switch_request({"id": "home"})[0] == 404


LOCAL = {"Host": "127.0.0.1:8765", "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}


def test_switch_endpoint_local_only(world, monkeypatch):
    calls = []
    monkeypatch.setattr(W, "refresh_build_view", lambda: calls.append(1))
    st = world["st"]
    sw = lambda peer, headers, body: server.workspace_switch(peer, headers, headers.get("Host", ""), 20, body, st)
    assert sw("127.0.0.1", LOCAL, {"id": "home"})[0] == 200 and calls == [1]
    assert st.current_id() == "home"
    # through a proxy / the public tunnel, or from the internet: no
    assert sw("127.0.0.1", {**LOCAL, "X-Forwarded-For": "8.8.8.8"}, {"id": "acme"})[0] == 403
    assert sw("8.8.8.8", {**LOCAL, "Host": "example.com", "Origin": "http://example.com"}, {"id": "acme"})[0] == 403
    # a web page can't post a plain form at it, and other origins are refused
    assert sw("127.0.0.1", {**LOCAL, "Content-Type": "text/plain"}, {"id": "acme"})[0] == 415
    assert sw("127.0.0.1", {**LOCAL, "Origin": "http://evil.example"}, {"id": "acme"})[0] == 403
    assert sw("127.0.0.1", LOCAL, {"id": "nope"})[0] == 400
    assert st.current_id() == "home" and calls == [1]
    # home network is fine (the server's gate decides whether the LAN reaches it at all)
    lan = {**LOCAL, "Host": "192.168.1.20:8765", "Origin": "http://192.168.1.20:8765"}
    assert sw("192.168.1.30", lan, {"id": "acme"})[0] == 200


def test_switch_route_is_registered():
    src = (REPO / "walter" / "server.py").read_text()
    assert '@routes.post("/api/workspace/switch")' in src


def test_refresh_build_view_skips_other_data_dirs(monkeypatch):
    monkeypatch.setenv("WALTER_DATA", "/tmp/elsewhere")
    started = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: started.append(a))
    W.refresh_build_view()
    assert started == []


# ---------------------------------------------------------------- zoom whitelists

def test_projects_zoom_whitelisted_everywhere():
    assert "projects" in app_mode.ZOOMS
    assert "projects" in present.BUILD_ZOOMS
    ctl = (REPO / "bin" / "walterctl").read_text()
    m = re.search(r"^ZOOMS = \((.*)\)$", ctl, re.M)
    assert m and '"projects"' in m.group(1)
