"""walter/ghactions.py: GitHub Actions status for the cockpit's Tests card (mocked API, no network)."""
import json
from pathlib import Path

import pytest

from walter import ghactions as G

SECRET = "ghp_" + "Z" * 36


@pytest.mark.parametrize("url,want", [
    ("https://github.com/acme/widget.git", ("acme", "widget")),
    ("https://github.com/acme/widget", ("acme", "widget")),
    ("https://x-access-token" + "@" + "github.com/acme/widget.git/", ("acme", "widget")),
    ("git@github.com:acme/widget.git", ("acme", "widget")),
    ("github.com:acme/my.site", ("acme", "my.site")),
    ("ssh://git@github.com/acme-llc/widget.git", ("acme-llc", "widget")),
    ("ssh://git@github.com:22/acme/widget", ("acme", "widget")),
    ("https://gitlab.com/acme/widget.git", None),
    ("/srv/git/widget.git", None),
    ("", None),
])
def test_parse_remote(url, want):
    assert G.parse_remote(url) == want


def _repo(tmp_path, name, workflows=False, publish=None):
    p = tmp_path / name
    p.mkdir()
    if workflows:
        (p / ".github" / "workflows").mkdir(parents=True)
    if publish:
        (p / "tools" / "release").mkdir(parents=True)
        (p / "tools" / "release" / "publish-org.sh").write_text(f'#!/bin/sh\nURL="{publish}"\n')
    return p


def test_github_repos_labels_dedup_and_publish_target(tmp_path):
    a = _repo(tmp_path, "a", workflows=True, publish="https://github.com/org-x/a.git")
    b = _repo(tmp_path, "b")
    rem = {a: [("origin", "git@github.com:me/a.git"), ("mirror", "https://github.com/me/a")],
           b: [("origin", "https://github.com/me/b.git"), ("org", "https://github.com/org-x/b.git"),
               ("local", "/srv/b.git")]}
    got = G.github_repos([("A", a), ("B", b)], remotes=lambda p: rem[p])
    assert [(r["owner"], r["name"], r["label"]) for r in got] == [
        ("me", "a", "A dev"), ("org-x", "a", "A org"), ("me", "b", "B dev"), ("org-x", "b", "B org")]
    assert got[0]["has_local"] and not got[2]["has_local"]
    # one repo: plain labels; no GitHub remote at all: nothing
    one = G.github_repos([("A", a)], remotes=lambda p: rem[p])
    assert [r["label"] for r in one] == ["dev", "org"]
    assert G.github_repos([("B", b)], remotes=lambda p: [("local", "/srv/b.git")]) == []
    assert G.github_repos([]) == []


WF = {"total_count": 3, "workflows": [
    {"id": 1, "name": "Tests", "state": "active"},
    {"id": 2, "name": "USB installer", "state": "active"},
    {"id": 3, "name": "Old", "state": "disabled_manually"},
    {"id": 4, "name": "Never ran", "state": "active"}]}
RUNS = {"workflow_runs": [
    {"workflow_id": 1, "name": "Tests", "status": "in_progress", "conclusion": None, "head_branch": "main",
     "created_at": "2026-11-11T10:00:00Z", "updated_at": "2026-11-11T10:01:00Z"},
    {"workflow_id": 1, "name": "Tests", "status": "completed", "conclusion": "success", "head_branch": "main",
     "created_at": "2026-11-11T09:00:00Z", "updated_at": "2026-11-11T09:05:00Z"},
    {"workflow_id": 2, "name": "USB installer", "status": "completed", "conclusion": "failure", "head_branch": "dev",
     "created_at": "2026-11-11T08:00:00Z", "updated_at": "2026-11-11T08:20:00Z"},
    {"workflow_id": 3, "name": "Old", "status": "completed", "conclusion": "success",
     "created_at": "2026-11-11T07:00:00Z", "updated_at": "2026-11-11T07:00:00Z"}]}


def test_parse_runs_latest_per_active_workflow():
    got = G.parse_runs(WF, RUNS)
    assert [(r["name"], r["state"], r["branch"]) for r in got] == [("Tests", "run", "main"), ("USB installer", "bad", "dev")]
    assert got[0]["updated"] > got[1]["updated"]


@pytest.mark.parametrize("status,conclusion,want", [
    ("completed", "success", "ok"), ("completed", "failure", "bad"), ("completed", "timed_out", "bad"),
    ("queued", None, "run"), ("in_progress", None, "run"), ("waiting", None, "run"),
    ("completed", "cancelled", "off"), ("completed", "skipped", "off"), ("completed", None, "off")])
def test_state_of(status, conclusion, want):
    assert G.state_of(status, conclusion) == want


def _getter(calls, fail=None):
    def get(owner, path, timeout):
        calls.append(path)
        if fail:
            raise fail
        return WF if "/workflows" in path else RUNS
    return get


def test_cache_staleness_and_error_fallback(tmp_path):
    calls = []
    a = G.repo_actions("o", "r", now=1000.0, get=_getter(calls), cache_dir=tmp_path)
    assert a["access"] and not a["offline"] and len(a["runs"]) == 2 and len(calls) == 2
    # fresh: no call
    G.repo_actions("o", "r", now=1000.0 + 60, get=_getter(calls), cache_dir=tmp_path)
    assert len(calls) == 2
    # stale + network error: last good runs kept, marked offline with the old ok time
    b = G.repo_actions("o", "r", now=1000.0 + 200, get=_getter(calls, RuntimeError("down")), cache_dir=tmp_path)
    assert len(calls) == 3 and b["offline"] and b["ok_at"] == 1000.0 and len(b["runs"]) == 2
    # denied: no access, no runs
    c = G.repo_actions("o", "r", now=1000.0 + 400, get=_getter(calls, G.NoAccess("403")), cache_dir=tmp_path)
    assert c["access"] is False and c["runs"] == []
    assert (tmp_path / "actions-o-r.json").exists()


def test_no_workflows_skips_runs_call(tmp_path):
    calls = []
    def get(owner, path, timeout):
        calls.append(path)
        return {"total_count": 0, "workflows": []}
    a = G.repo_actions("o", "empty", now=1.0, get=get, cache_dir=tmp_path)
    assert a["runs"] == [] and len(calls) == 1


def test_actions_state_hide_offline_noaccess(tmp_path):
    a = _repo(tmp_path, "a", workflows=True)
    rem = lambda p: [("origin", "https://github.com/me/a.git"), ("org", "https://github.com/org-x/a.git")]
    cache = tmp_path / "cache"

    def get(owner, path, timeout):
        if owner == "me":
            raise G.NoAccess("403")
        return WF if "/workflows" in path else RUNS
    st = G.actions_state([("A", a)], now=2e9, get=get, remotes=rem, cache_dir=cache, words=[])
    assert [(i["name"], i["state"], i["repo"]) for i in st["items"]] == [("USB installer", "bad", "org"), ("Tests", "run", "org")]
    assert st["noaccess"] == ["dev"] and st["multi"] is False and st["offline_since"] is None
    # later: the network is down -> same items, offline since the last good fetch
    def down(owner, path, timeout):
        if owner == "me":
            raise G.NoAccess("403")
        raise TimeoutError()
    st2 = G.actions_state([("A", a)], now=2e9 + 300, get=down, remotes=rem, cache_dir=cache, words=[])
    assert len(st2["items"]) == 2 and st2["offline_since"] == 2e9
    # no workflows anywhere + no local workflows dir -> hidden
    b = _repo(tmp_path, "b")
    empty = lambda o, p, t: {"total_count": 0, "workflows": []}
    assert G.actions_state([("B", b)], now=1.0, get=empty, remotes=lambda p: [("origin", "https://github.com/me/b.git")],
                           cache_dir=tmp_path / "c2", words=[]) is None
    denied = lambda o, p, t: (_ for _ in ()).throw(G.NoAccess("403"))
    assert G.actions_state([("B", b)], now=1.0, get=denied, remotes=lambda p: [("origin", "https://github.com/me/b2.git")],
                           cache_dir=tmp_path / "c3", words=[]) is None
    # no GitHub remote (e.g. Home with no repos) -> hidden, no calls
    assert G.actions_state([], now=1.0, get=lambda *a: 1 / 0, cache_dir=tmp_path) is None


def test_budget_exhausted_returns_cache_without_calls(tmp_path):
    calls = []
    G.repo_actions("o", "r", now=1.0, get=_getter(calls), cache_dir=tmp_path)
    got = G.repo_actions("o", "r", now=1e6, get=_getter(calls), cache_dir=tmp_path, timeout=0.0)
    assert len(calls) == 2 and len(got["runs"]) == 2


def test_token_never_lands_in_output_or_cache(tmp_path, monkeypatch):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "github-qiiver-llc.token").write_text(SECRET + "\n")
    monkeypatch.setattr(G, "SECRETS", secrets)
    seen = {}

    class Resp:
        def __init__(self, body): self.body = body
        def read(self, n=-1): return json.dumps(self.body).encode()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        seen["auth"] = req.get_header("Authorization")
        seen["url"] = req.full_url
        # a hostile API echoing the token back in a name must not leak it either
        if "/workflows" in req.full_url:
            return Resp({"total_count": 1, "workflows": [{"id": 9, "name": "CI " + SECRET, "state": "active"}]})
        return Resp({"workflow_runs": [{"workflow_id": 9, "status": "completed", "conclusion": "success",
                                        "head_branch": SECRET, "created_at": "2026-11-11T10:00:00Z"}]})
    monkeypatch.setattr(G.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(G.subprocess, "run", lambda *a, **k: pytest.fail("gh must not be used for the org"))
    a = _repo(tmp_path, "a")
    st = G.actions_state([("A", a)], now=5.0, remotes=lambda p: [("org", "https://github.com/qiiver-llc/a.git")],
                         cache_dir=tmp_path / "cache", words=[])
    assert seen["auth"] == "Bearer " + SECRET and SECRET not in seen["url"]
    assert st and st["items"]
    assert SECRET not in json.dumps(st)
    # the cache holds the parsed runs; it must not hold the token in any field we add
    cached = (tmp_path / "cache" / "actions-qiiver-llc-a.json").read_text()
    assert "Bearer" not in cached and "token" not in json.loads(cached)


def test_personal_repo_uses_gh_without_token(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "SECRETS", tmp_path)
    monkeypatch.setattr(G, "_gh_bin", lambda: "/usr/bin/gh")
    calls = []

    class R:
        returncode = 1
        stdout = ""
        stderr = "gh: Resource not accessible by personal access token (HTTP 403)"

    def run(argv, **kw):
        calls.append(argv)
        return R()
    monkeypatch.setattr(G.subprocess, "run", run)
    a = G.repo_actions("me", "a", now=1.0, cache_dir=tmp_path)
    assert a["access"] is False
    assert calls and calls[0][:2] == ["/usr/bin/gh", "api"] and not any("Bearer" in x for x in calls[0])
