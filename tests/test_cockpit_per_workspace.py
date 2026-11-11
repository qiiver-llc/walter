"""Every cockpit card follows the current project (owner, 11 Nov 10:34): test runs are recorded per
workspace, each workspace has its own plan, and its own ship pipeline preset (release | site | none).
Temp workspaces only; the live-site check is mocked."""
import json
import subprocess
import time
from pathlib import Path

import pytest

from walter import buildview, cockpit as C, workspaces as W

REPO = Path(__file__).resolve().parents[1]


def git_repo(path: Path, subject: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True)
    run("init", "-q")
    (path / "f.txt").write_text("x\n")
    run("add", "-A")
    run("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", subject)
    return path


def site_repo(path: Path, assets=("aaaaaa1111", "bbbbbb2222")) -> Path:
    (path / "src").mkdir(parents=True)
    (path / "src" / "layout.html").write_text("<html></html>")
    (path / "build.py").write_text("# build\n")
    (path / "site.config.json").write_text(json.dumps({"domain": "example.org"}))
    (path / "public").mkdir()
    old = time.time() - 600
    for f in (path / "src" / "layout.html", path / "build.py", path / "site.config.json"):
        import os
        os.utime(f, (old, old))
    (path / "public" / "index.html").write_text(
        "".join(f'<link href="/assets/x.css?v={a}">' for a in assets))
    (path / "DEPLOY.md").write_text("# Deploy\n- Redeploy: `python3 build.py && tools/deploy.sh`\n"
                                    "- 1 Nov deploy: `1111aaaa` (commit 1234567). Rollback target: `0000ffff-x`.\n"
                                    "- 2 Nov site v2 deploy: `c8f80a74` (commit 9bdedfc: more). Rollback target: `8a0446a7-y`.\n")
    return git_repo(path, "Site commit")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Code (the Walter repo itself), Site (a static site repo) and Home (no repos)."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(C, "DATA", data)
    monkeypatch.setattr(C, "IDS_FILE", data / "ids.json")
    monkeypatch.setattr(C, "remote_release", lambda now=None: {"version": "1.2.3", "signed": True})
    st = W.WorkspaceStore()
    site = site_repo(tmp_path / "site")
    st.save({"version": 1, "default": "home", "workspaces": [
        st._fill({"id": "home", "name": "Home", "repos": []}),
        st._fill({"id": "code", "name": "Code", "repos": [str(REPO)]}),
        st._fill({"id": "site", "name": "Site", "repos": [str(site)]}),
    ]})
    st.switch("site")
    return {"st": st, "data": data, "site": site, "face": C.FACE, "tmp": tmp_path}


# ---------------------------------------------------------------- test results go to their workspace

def test_targets_route_by_repo(world):
    d, f = world["data"], world["face"]
    assert C.test_targets(world["site"]) == [(d / "last-test" / "site.json", f / "dashboard-tests-site.json")]
    # the Walter repo keeps the old shared files too (compatibility)
    assert C.test_targets(REPO) == [(d / "last-test" / "code.json", f / "dashboard-tests-code.json"),
                                    (d / "last-test.json", f / "dashboard-tests.json")]
    other = world["tmp"] / "elsewhere"
    assert C.test_targets(other) == [(d / "last-test" / "repo-elsewhere.json", None)]


def test_targets_without_workspaces_are_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "DATA", tmp_path)
    assert C.test_targets(tmp_path / "any") == [(tmp_path / "last-test.json", C.FACE / "dashboard-tests.json")]


def test_run_in_the_site_repo_shows_only_in_the_site_cockpit(world):
    site = world["site"]
    (site / "tests").mkdir()
    (site / "tests" / "test_s.py").write_text("def test_ok():\n    pass\n")
    assert C.run_tests(site, node=False, echo=None) == 0
    d = json.loads((world["data"] / "last-test" / "site.json").read_text())
    assert d["passed"] == 1 and d["workspace"] == "site" and not d["running"]
    mirror = json.loads((world["face"] / "dashboard-tests-site.json").read_text())
    assert mirror["workspace"] == "site" and "repo" not in mirror
    assert not (world["data"] / "last-test.json").exists()          # the Walter copy is untouched
    assert not (world["face"] / "dashboard-tests.json").exists()
    out = C.cockpit(words=[])
    assert out["tests"]["passed"] == 1
    world["st"].switch("code")
    assert C.cockpit(words=[])["tests"] is None
    world["st"].switch("home")
    assert C.cockpit(words=[])["tests"] is None


def test_old_shared_run_still_shows_for_the_walter_workspace(world):
    (world["data"] / "last-test.json").write_text(json.dumps({"passed": 7, "ok": True, "repo": str(REPO)}))
    world["st"].switch("code")
    assert C.cockpit(words=[])["tests"]["passed"] == 7
    world["st"].switch("site")
    out = C.cockpit(words=[])
    assert out["tests"] is None and out["tests_hidden"] is True
    assert buildview.snapshot(words=[])["tests"] is None             # the strip too


# ---------------------------------------------------------------- one plan per workspace

def test_plan_per_workspace_set_clear_migrate(world):
    face = world["face"]
    (face / "dashboard-plan.md").write_text("# Old shared plan\n")
    assert C.migrate_plan(world["st"]) == "code"                     # the Walter repo's workspace
    assert (face / "dashboard-plan-code.md").read_text() == "# Old shared plan\n"
    assert not (face / "dashboard-plan.md").exists()
    assert C.migrate_plan(world["st"]) is None                       # once
    src = world["tmp"] / "p.md"
    src.write_text("# Site plan\n- [ ] copy\n")
    C.install_plan(src, words=[], out=C.plan_path("site"))
    assert C.plan_path("site").read_text().startswith("# Site plan")
    out = C.cockpit(words=[])                                        # current: site
    assert out["plan"]["set"] and out["plan"]["workspace"] == "site"
    world["st"].switch("home")
    assert C.cockpit(words=[])["plan"]["set"] is False
    C.plan_path("site").unlink()                                     # plan clear for site only
    assert C.plan_path("code").exists()
    assert C.plan_path(None) == face / "dashboard-plan.md"
    assert C.plan_path("../x").name == "dashboard-plan-x.md"         # never a path


def test_migrate_keeps_a_newer_workspace_plan(world):
    face = world["face"]
    C.plan_path("code").write_text("new\n")
    (face / "dashboard-plan.md").write_text("stale\n")
    C.migrate_plan(world["st"])
    assert C.plan_path("code").read_text() == "new\n" and not (face / "dashboard-plan.md").exists()


# ---------------------------------------------------------------- pipeline presets

def test_preset_defaults_and_override(world):
    st = world["st"]
    assert C.pipeline_preset(C.scope()) == "site"
    st.switch("code")
    assert C.pipeline_preset(C.scope()) == "release"
    st.switch("home")
    assert C.pipeline_preset(C.scope()) == "none"
    st.set("home", "pipeline", "release")
    assert C.pipeline_preset(C.scope()) == "release"
    with pytest.raises(ValueError):
        st.set("home", "pipeline", "rocket")
    assert C.pipeline_preset(None) == "release"                      # no workspaces: as before
    other = git_repo(world["tmp"] / "plain", "x")
    st.set("home", "pipeline", "auto")
    st.set("home", "repos", str(other))
    assert C.pipeline_preset(C.scope()) == "basic"


def test_release_pipeline_for_the_walter_workspace(world):
    world["st"].switch("code")
    P = C.cockpit(words=[])["pipeline"]
    assert [s["stage"] for s in P] == ["build", "test", "sign", "publish", "live"]
    assert {s["stage"]: s["note"] for s in P}["publish"].startswith("v1.2.3")


def test_home_has_no_pipeline_and_nothing_from_walter(world):
    world["st"].switch("home")
    (world["data"] / "last-test.json").write_text(json.dumps({"passed": 7, "ok": True, "repo": str(REPO)}))
    (world["data"] / "tasks.json").write_text(json.dumps([
        {"id": 1, "title": "Code worker", "status": "running", "workspace": "code"}]))
    out = C.cockpit(words=[])
    assert out["pipeline"] == [] and out["pipeline_preset"] == "none"
    assert out["lanes"] == [] and out["tests"] is None and out["board"]["building"] == []
    snap = buildview.snapshot(words=[])
    assert snap["shipped"] == [] and snap["diff"] is None and snap["release"] is None and snap["tests"] is None


def test_site_pipeline_lights(world):
    site, now = world["site"], time.time()
    local = C.build_id((site / "public" / "index.html").read_text())
    calls = []

    def fetch(url):
        calls.append(url)
        return 200, (site / "public" / "index.html").read_text()

    cache = world["tmp"] / "live.json"
    P = C.site_pipeline(site, {"ok": True, "passed": 4}, now, fetch=fetch, cache=cache)
    assert [s["stage"] for s in P] == ["build", "test", "deploy", "live"]
    by = {s["stage"]: s for s in P}
    assert by["build"]["state"] == "ok" and by["build"]["note"].startswith(local)
    assert by["test"]["note"] == "4 passing"
    assert by["deploy"]["state"] == "ok" and by["deploy"]["note"].startswith("c8f80a74")
    assert by["live"] == {"stage": "live", "state": "ok", "note": f"200 · {local}"}
    assert calls == ["https://example.org"]
    # cached for 5 minutes: no second request
    C.site_pipeline(site, None, now + 60, fetch=fetch, cache=cache)
    assert len(calls) == 1
    # after 5 minutes: an older build is live
    by = {s["stage"]: s for s in C.site_pipeline(site, None, now + 400, cache=cache,
                                                  fetch=lambda u: (200, '<a href="x?v=cccccc3333">'))}
    assert by["live"]["state"] == "run" and by["test"]["state"] == "idle"
    by = {s["stage"]: s for s in C.site_pipeline(site, None, now + 800, cache=cache, fetch=lambda u: (503, ""))}
    assert by["live"] == {"stage": "live", "state": "bad", "note": "HTTP 503"}

    def boom(u):
        raise OSError("offline")

    by = {s["stage"]: s for s in C.site_pipeline(site, None, now + 1200, cache=cache, fetch=boom)}
    assert by["live"]["note"] == "unreachable"


def test_site_build_goes_stale_when_the_source_changes(world):
    site = world["site"]
    (site / "src" / "layout.html").write_text("<html>new</html>")
    import os
    later = time.time() + 30
    os.utime(site / "src" / "layout.html", (later, later))
    assert C.site_build(site, time.time())["state"] == "run"
    assert C.site_build(world["tmp"] / "nothing", time.time()) == {"stage": "build", "state": "idle",
                                                                   "note": "not built"}


def test_site_deploy_log_wins_over_deploy_md(world):
    site = world["site"]
    (site / "deploy.log").write_text("ok\nDeploy failed: error 500\n")
    d = C.site_deploy_state(site, time.time())
    assert d["state"] == "bad"


def test_cockpit_for_the_site_workspace_uses_the_site_pipeline(world, monkeypatch):
    monkeypatch.setattr(C, "site_live", lambda url, now=None, cache=None, fetch=None: {"status": 200, "build_id": None})
    out = C.cockpit(words=[])
    assert out["pipeline_preset"] == "site"
    assert [s["stage"] for s in out["pipeline"]] == ["build", "test", "deploy", "live"]
    assert [ln["repo"] for ln in out["lanes"]] == ["Site"]
    assert out["workspace"]["id"] == "site"


def test_build_id_ignores_order_and_needs_stamps():
    assert C.build_id('a?v=bbbbbb22 b?v=aaaaaa11') == C.build_id('?v=aaaaaa11 ?v=bbbbbb22 ?v=aaaaaa11')
    assert C.build_id("<html>no stamps</html>") is None
