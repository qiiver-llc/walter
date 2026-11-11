"""GitHub Actions status for the build cockpit's Tests card (the owner, 11 Nov 10:44: "under tests in the
same card, show actions status if the repo has any actions").

For the current workspace's repos (walter/cockpit.scope): every GitHub remote of each repo (origin, org, ...)
plus the org mirror that tools/release/publish-org.sh pushes to (it never adds a remote to the dev clone),
then the latest run per workflow from the GitHub REST API. Run only by the build helper timer
(walter/buildview.py -> walter/cockpit.py), never from the face.

  * Auth: repos owned by ORG_OWNER use ~/.walter/secrets/github-<owner>.token (read here, sent only as an
    HTTP header, never printed, logged, written or put in argv); other repos use `gh api` (gh keeps its own).
  * Cache: ~/.walter/cache/actions-<owner>-<repo>.json, refreshed at most every CACHE_MAX_AGE s; the whole
    fetch round has a TIME_BUDGET (8 s); on errors the last good result stays, marked offline with its age.
  * A repo the token can't read (403/404) but that has local .github/workflows is reported as "no access"
    so the card can say so; one with no workflows anywhere is hidden.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()
DATA = HOME / ".walter"
CACHE_DIR = DATA / "cache"
SECRETS = DATA / "secrets"
CACHE_MAX_AGE = 120
TIME_BUDGET = 8.0
ORG_OWNERS = ("qiiver-llc",)
API = "https://api.github.com/"
MAX_ITEMS = 12

_HTTPS = re.compile(r"^(?:https?|git|ssh)://(?:[^@/]+@)?github\.com(?::\d+)?/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", re.I)
_SCP = re.compile(r"^(?:[\w.-]+@)?github\.com:/?([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", re.I)
_PUBLISH_URL = re.compile(r'^URL="(https://github\.com/[\w.-]+/[\w.-]+?(?:\.git)?)"', re.M)

OK, BAD, RUN, OFF = "ok", "bad", "run", "off"
_BAD = {"failure", "timed_out", "startup_failure", "action_required"}
_OFF = {"cancelled", "skipped", "neutral", "stale"}


def parse_remote(url: str) -> tuple[str, str] | None:
    """A GitHub remote URL (https, ssh://, git@github.com:owner/repo.git) -> (owner, repo); else None."""
    u = str(url or "").strip()
    m = _HTTPS.match(u) or _SCP.match(u)
    if not m:
        return None
    return m.group(1), m.group(2)


def _git_remotes(repo: Path) -> list[tuple[str, str]]:
    try:
        out = subprocess.run(["git", "-C", str(repo), "config", "--get-regexp", r"^remote\..*\.url$"],
                             capture_output=True, text=True, timeout=4).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        key, _, url = line.partition(" ")
        name = key[len("remote."):-len(".url")] if key.startswith("remote.") and key.endswith(".url") else ""
        if name:
            rows.append((name, url.strip()))
    return rows


def publish_target(repo: Path) -> str | None:
    """The org mirror URL tools/release/publish-org.sh pushes the repo to (if it has that script)."""
    try:
        m = _PUBLISH_URL.search((Path(repo) / "tools" / "release" / "publish-org.sh").read_text(errors="replace"))
    except OSError:
        return None
    return m.group(1) if m else None


def short_label(remote: str) -> str:
    return "dev" if remote == "origin" else re.sub(r"[^\w.-]", "", remote)[:12] or "repo"


def github_repos(repos, remotes=None) -> list[dict]:
    """[(label, path)] -> [{owner, name, label, local, has_local}] for every GitHub remote, de-duplicated.
    remotes(path) -> [(remote name, url)] (for tests)."""
    remotes = remotes or _git_remotes
    out, seen = [], set()
    many = len(list(repos or [])) > 1
    for label, path in repos or []:
        path = Path(path)
        rs = list(remotes(path))
        pub = publish_target(path)
        if pub and not any(parse_remote(u) == parse_remote(pub) for _, u in rs):
            rs.append(("org", pub))
        has_local = (path / ".github" / "workflows").is_dir()
        for rname, url in rs:
            p = parse_remote(url)
            if not p or p[0].lower() + "/" + p[1].lower() in seen:
                continue
            seen.add(p[0].lower() + "/" + p[1].lower())
            lab = short_label(rname)
            if many:
                lab = f"{label} {lab}"
            out.append({"owner": p[0], "name": p[1], "label": lab, "local": str(label), "has_local": has_local})
    return out


# ---- fetching ----------------------------------------------------------------------------------------

class NoAccess(Exception):
    pass


def _token_for(owner: str) -> str | None:
    if owner.lower() not in ORG_OWNERS:
        return None
    try:
        t = (SECRETS / f"github-{owner.lower()}.token").read_text().strip()
    except OSError:
        return None
    return t or None


def _http_get(path: str, token: str, timeout: float) -> dict:
    req = urllib.request.Request(API + path, headers={
        "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "walter-cockpit"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read(2_000_000).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise NoAccess(str(e.code)) from None
        raise RuntimeError(f"http {e.code}") from None


def _gh_bin() -> str | None:
    p = HOME / ".local" / "bin" / "gh"
    return str(p) if p.exists() else shutil.which("gh")


def _gh_get(path: str, timeout: float) -> dict:
    gh = _gh_bin()
    if not gh:
        raise NoAccess("no gh")
    try:
        r = subprocess.run([gh, "api", path], capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"})
    except subprocess.TimeoutExpired:
        raise RuntimeError("timeout") from None
    except OSError:
        raise NoAccess("no gh") from None
    if r.returncode != 0:
        if re.search(r"HTTP (401|403|404)|not accessible|Not Found", r.stdout + r.stderr):
            raise NoAccess("denied")
        raise RuntimeError("gh failed")
    return json.loads(r.stdout or "{}")


def default_get(owner: str, path: str, timeout: float) -> dict:
    tok = _token_for(owner)
    if tok:
        return _http_get(path, tok, timeout)
    return _gh_get(path, timeout)


def _ts(s) -> float | None:
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def state_of(status, conclusion) -> str:
    status, conclusion = str(status or "").lower(), str(conclusion or "").lower()
    if status and status != "completed":
        return RUN
    if conclusion == "success":
        return OK
    if conclusion in _BAD:
        return BAD
    return OFF


def parse_runs(workflows: dict, runs: dict) -> list[dict]:
    """GitHub /actions/workflows + /actions/runs JSON -> the latest run per active workflow:
    [{name, state, status, conclusion, branch, created, updated}] (workflows that never ran are left out)."""
    active = {}
    for w in (workflows or {}).get("workflows") or []:
        if isinstance(w, dict) and str(w.get("state") or "active").startswith("active"):
            active[w.get("id")] = str(w.get("name") or w.get("path") or "workflow")
    latest = {}
    for r in (runs or {}).get("workflow_runs") or []:
        if not isinstance(r, dict):
            continue
        wid = r.get("workflow_id")
        if active and wid not in active:
            continue
        created = _ts(r.get("created_at")) or 0.0
        if wid in latest and latest[wid]["created"] >= created:
            continue
        latest[wid] = {"name": active.get(wid) or str(r.get("name") or "workflow"),
                       "state": state_of(r.get("status"), r.get("conclusion")),
                       "status": str(r.get("status") or ""), "conclusion": str(r.get("conclusion") or ""),
                       "branch": str(r.get("head_branch") or ""), "created": created,
                       "updated": _ts(r.get("updated_at")) or created}
    return sorted(latest.values(), key=lambda x: -x["updated"])


def _cache_path(owner: str, name: str, cache_dir: Path | None = None) -> Path:
    safe = re.sub(r"[^\w.-]", "_", f"{owner}-{name}".lower())
    return Path(cache_dir or CACHE_DIR) / f"actions-{safe}.json"


def _read(p: Path) -> dict:
    try:
        d = json.loads(p.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(p: Path, d: dict) -> None:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(d))
        os.replace(tmp, p)
    except OSError:
        pass


def repo_actions(owner: str, name: str, now: float | None = None, get=None, cache_dir: Path | None = None,
                 timeout: float = TIME_BUDGET, max_age: float = CACHE_MAX_AGE) -> dict:
    """{runs: [...], ok_at, access: True|False, offline: bool} for one GitHub repo, cached on disk."""
    now = time.time() if now is None else now
    p = _cache_path(owner, name, cache_dir)
    c = _read(p)
    if c and now - float(c.get("fetched") or 0) < max_age:
        return c
    if timeout <= 0.2:   # out of budget this round: the old data, try again next tick
        return c or {"runs": [], "access": None}
    get = get or default_get
    start = time.monotonic()
    try:
        wf = get(owner, f"repos/{owner}/{name}/actions/workflows?per_page=100", timeout)
        runs = {"workflow_runs": []}
        if (wf or {}).get("total_count") or (wf or {}).get("workflows"):
            left = timeout - (time.monotonic() - start)
            if left <= 0.2:
                raise RuntimeError("timeout")
            runs = get(owner, f"repos/{owner}/{name}/actions/runs?per_page=50&exclude_pull_requests=true", left)
        c = {"fetched": now, "ok_at": now, "access": True, "offline": False, "runs": parse_runs(wf, runs)[:MAX_ITEMS]}
    except NoAccess:
        c = {"fetched": now, "ok_at": c.get("ok_at"), "access": False, "offline": False, "runs": []}
    except Exception:
        c = {"fetched": now, "ok_at": c.get("ok_at"), "access": c.get("access"), "offline": True,
             "runs": c.get("runs") or []}
    _write(p, c)
    return c


def actions_state(repos, now: float | None = None, get=None, remotes=None, cache_dir: Path | None = None,
                  budget: float = TIME_BUDGET, words=None) -> dict | None:
    """The Actions block for the cockpit's Tests card, or None (hide it) when the workspace's repos have no
    GitHub remote or no workflows: {items: [{name, state, repo, branch, ts}], multi, offline_since, noaccess}."""
    from walter.buildview import clean_subject
    now = time.time() if now is None else now
    gh = github_repos(repos, remotes)
    if not gh:
        return None
    deadline = time.monotonic() + budget
    items, noaccess, offline, labels = [], [], None, set()
    for r in gh:
        left = deadline - time.monotonic()
        a = repo_actions(r["owner"], r["name"], now, get=get, cache_dir=cache_dir, timeout=min(budget, left))
        if a.get("access") is False:
            if r["has_local"]:
                noaccess.append(r["label"])
            continue
        if a.get("offline") and a.get("runs"):
            ok_at = a.get("ok_at")
            offline = min(offline or now, float(ok_at)) if ok_at else (offline or now)
        for x in a.get("runs") or []:
            labels.add(r["label"])
            items.append({"name": clean_subject(x.get("name"), words, 40), "state": x.get("state") or OFF,
                          "repo": r["label"], "branch": clean_subject(x.get("branch"), words, 30),
                          "ts": float(x.get("updated") or x.get("created") or 0)})
    if not items and not noaccess:
        return None
    # failures and running ones first, then newest
    rank = {BAD: 0, RUN: 1, OK: 2, OFF: 3}
    items.sort(key=lambda x: (rank.get(x["state"], 4), -x["ts"]))
    return {"items": items[:MAX_ITEMS], "multi": len(labels) > 1, "offline_since": offline, "noaccess": noaccess}
