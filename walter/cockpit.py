"""Build mode v2 cockpit data (owner approved the plan 9 Nov 13:37): the extra panels of the build view.

Called from walter.buildview.snapshot() (walter-build.timer, every 15 s; no Walter restart needed) and
added to face/dashboard-build.json under "cockpit":

  board     Ready / Building / Done cards from ~/.walter/home/backlog.md ([ready] / [in progress] /
            [done] lines) plus the workers in ~/.walter/tasks.json (running / queued / finished)
  lanes     the last commits of ~/walter and ~/walter-site, one lane each (short redacted subjects)
  pipeline  ship lights, a preset per workspace (workspaces.json "pipeline": release|site|none, default
            by repo): release = build, test, sign, publish, live (git state, the test run, the latest
            release from releases.wallter.app latest.json, fetched at most every 5 min, cached);
            site = build (public/ vs src/), test, deploy (deploy log or DEPLOY.md), live (HTTP 200 and the
            same build id on the site, checked at most every 5 min); none = no pipeline (Home)
  gauges    CPU (from /proc/stat deltas between runs), memory, load, weekly usage (face/usage.json),
            worker count
  tests     the current workspace's last / running test run (walterctl test writes
            ~/.walter/last-test/<workspace>.json; the Walter repo's run also ~/.walter/last-test.json)
  media     True while a podcast / video plays (the face keeps its sound cues quiet then)
  plan      whether the current workspace's plan (face/dashboard-plan-<workspace>.md) is set
            (walterctl display build plan FILE [--workspace ID])

Every line goes through buildview.clean_subject (tokens, emails, IPs, paths, private words).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
DATA = HOME / ".walter"
REPO = Path(__file__).resolve().parent.parent
FACE = REPO / "face"
BACKLOG = DATA / "home" / "backlog.md"
RELEASE_URL = "https://releases.wallter.app/latest.json"
RELEASE_CACHE = DATA / "cache" / "release-latest.json"
RELEASE_MAX_AGE = 300
CPU_CACHE = DATA / "cache" / "cockpit-cpu.json"
SITE_DEPLOY_LOGS = [HOME / "walter-site" / "deploy.log", HOME / "walter-site" / ".deploy.log",
                    HOME / "walter-site" / "logs" / "deploy.log", DATA / "site-deploy.log"]

_LINE = re.compile(r"^\s*[-*]\s*\[(ready|in progress|done|ask|blocked|parked)\]\s*(.*)$", re.I)


def _clean(s, words, n=60):
    from walter.buildview import clean_subject
    return clean_subject(s, words, n)


def _read_json(p: Path, default=None):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return default


# ---- task board -----------------------------------------------------------------------------------

def parse_backlog(text: str, words=None) -> list[dict]:
    """backlog.md lines `- [status] **Title**: details` -> [{status, title}] (status lowercased)."""
    out = []
    for raw in str(text or "").splitlines():
        m = _LINE.match(raw)
        if not m:
            continue
        body = m.group(2).strip()
        t = re.match(r"\*\*(.+?)\*\*", body)
        title = t.group(1) if t else re.split(r"[:.]\s", body, maxsplit=1)[0]
        title = _clean(title.strip(" :*"), words, 48)
        if title:
            out.append({"status": m.group(1).lower(), "title": title, "full": (t.group(1) if t else re.split(r"[:.]\s", body, maxsplit=1)[0]).strip(" :*")})
    return out


IDS_FILE = DATA / "board-ids.json"


def _stable_id(key: str, ids_file: Path | None = None) -> int:
    """Permanent number per backlog item (never reused, never shifts when cards move)."""
    f = Path(ids_file or IDS_FILE)
    d = _read_json(f, {}) or {}
    ids = d.get("ids") if isinstance(d.get("ids"), dict) else {}
    if key not in ids:
        ids[key] = int(d.get("next", 1))
        d = {"next": ids[key] + 1, "ids": ids}
        try:
            f.write_text(json.dumps(d))
        except OSError:
            pass
    return ids[key]


def board_lookup(label: str, backlog: Path | None = None, tasks: Path | None = None) -> dict | None:
    """Resolve a permanent board number (T12, or just 12) to its card, with the full backlog line (`raw`)."""
    m = re.fullmatch(r"\s*t?\s*0*(\d+)\s*", str(label or ""), re.I)
    if not m:
        return None
    num = int(m.group(1))
    if backlog is None:
        ws = scope()
        if ws is not None:
            if ws["backlog"] is None:
                return None
            backlog = ws["backlog"]
    try:
        lines = Path(backlog or BACKLOG).read_text(errors="replace").splitlines()
    except OSError:
        return None
    for raw in lines:
        mm = _LINE.match(raw)
        if not mm:
            continue
        body = mm.group(2).strip()
        t = re.match(r"\*\*(.+?)\*\*", body)
        title = (t.group(1) if t else re.split(r"[:.]\s", body, maxsplit=1)[0]).strip(" :*")
        if _stable_id(title[:60]) == num:
            return {"label": f"T{num}", "title": title, "status": mm.group(1).lower(), "raw": body, "kind": "backlog"}
    return None


# ---- workspaces (walter/workspaces.py): the board and lanes show only the current one ---------------

def scope(face_id: str | None = None, store=None) -> dict | None:
    """The current workspace's slice for this face: {id, name, walled, backlog, repos, store}, or None
    when no workspaces are defined (then everything stays exactly as before)."""
    try:
        from walter import workspaces as WS
        store = store or WS.WorkspaceStore()
        if not store.defined():
            return None
        w = store.current(face_id or WS.current_face_id())
    except Exception:
        return None
    from walter.buildview import REPOS
    known = {str(Path(p).resolve()): label for label, p in REPOS}
    repos = []
    for r in w["repos"]:
        p = WS.expand(r)
        if store.path_allowed(p, w["id"]):
            repos.append((known.get(str(p.resolve()), p.name.replace("-", " ").title()), p))
    bl = WS.expand(w["backlog"])
    return {"id": w["id"], "name": w["name"], "walled": w["privacy"] == "walled",
            "backlog": bl if store.path_allowed(bl, w["id"]) else None, "repos": repos, "store": store,
            "pipeline": w.get("pipeline")}


def task_board(now: float | None = None, backlog: Path | None = None, tasks: Path | None = None,
               words=None, keep: int = 12, workspace: dict | None = None) -> dict:
    """workspace: a scope() dict: its backlog, and only the workers tagged with it (untagged older
    workers show everywhere except in walled workspaces)."""
    now = time.time() if now is None else now
    if workspace is not None and backlog is None:
        backlog = workspace["backlog"] or Path("/nonexistent/backlog.md")
    try:
        items = parse_backlog(Path(backlog or BACKLOG).read_text(errors="replace"), words)
    except OSError:
        items = []
    board = {"ready": [], "building": [], "done": []}
    for it in items:
        col = {"ready": "ready", "in progress": "building", "done": "done"}.get(it["status"])
        if col:
            board[col].append({"id": "b:" + it["title"][:30], "title": it["title"], "kind": "backlog",
                               "num": _stable_id(it["full"][:60])})
    T = _read_json(tasks or DATA / "tasks.json", [])
    T = [t for t in (T if isinstance(T, list) else []) if isinstance(t, dict)]
    work = []
    if workspace is not None:
        T = [t for t in T if workspace["store"].task_visible(t, workspace["id"])]
    for t in T:
        st = t.get("status")
        card = {"id": f"w:{t.get('id')}", "title": _clean(t.get("title") or "Worker", words, 48), "kind": "worker",
                "n": t.get("id"), "status": st}
        if st in ("running", "queued"):
            work.append(card)
        elif st in ("done", "failed", "cancelled") and t.get("finished") and now - float(t["finished"]) < 86400:
            card["finished"] = float(t["finished"])
            board["done"].insert(0, card)
    board["building"] = work + board["building"]
    board["done"].sort(key=lambda c: -(c.get("finished") or 0))
    counts = {k: len(v) for k, v in board.items()}
    return {"ready": board["ready"][:keep], "building": board["building"][:keep], "done": board["done"][:keep],
            "counts": counts}


# ---- all workspaces at a glance (the pill bar + the "projects" zoom; owner approved 11 Nov 09:37) -----

_DONE_DATED = re.compile(r"^\s*[-*]\s*\[done[^\]]*?(\d{4}-\d{2}-\d{2})", re.I)
_STATUS = re.compile(r"^\s*[-*]\s*\[(ready|in progress)", re.I)


def backlog_counts(path, today: str) -> dict:
    """Counts only (no titles ever leave here): ready, building (in progress), done today (dated lines)."""
    out = {"ready": 0, "building": 0, "done_today": 0}
    try:
        text = Path(path).read_text(errors="replace")
    except (OSError, TypeError):
        return out
    for raw in text.splitlines():
        m = _STATUS.match(raw)
        if m:
            out["ready" if m.group(1).lower() == "ready" else "building"] += 1
            continue
        m = _DONE_DATED.match(raw)
        if m and m.group(1) == today:
            out["done_today"] += 1
    return out


def _last_commit(repos, words, now) -> dict | None:
    from walter.buildview import _git, ago
    best = None
    for repo in repos:
        repo = Path(repo)
        if not (repo / ".git").exists():
            continue
        line = _git(repo, "log", "-1", "--format=%ct%x09%s").strip()
        ts, _, subj = line.partition("\t")
        try:
            t = float(ts)
        except ValueError:
            continue
        if best is None or t > best["ts"]:
            best = {"ts": t, "subject": _clean(subj, words, 60), "ago": ago(t, now)}
    return best


def workspaces_overview(face_id: str | None = None, store=None, now: float | None = None, words=None,
                        tasks=None, loop=None) -> list[dict]:
    """Every workspace for the pills and the "projects" zoom:
    [{id, name, current, walled, badges: {running, queued, ready, done_today}, focus, last_commit, usage_share,
      budget_share}].
    PRIVACY: a walled workspace that is NOT the current one gets only its name and numbers: no focus text,
    no commit subject, no repo names, no task titles (not even in the data file). [] without workspaces."""
    try:
        from walter import workspaces as WS
        store = store or WS.WorkspaceStore()
        if not store.defined():
            return []
        cur = store.current_id(face_id or WS.current_face_id())
        all_ws = store.all()
    except Exception:
        return []
    now = time.time() if now is None else now
    lt = time.localtime(now)
    today = time.strftime("%Y-%m-%d", lt)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    T = _read_json(DATA / "tasks.json", []) if tasks is None else tasks
    T = [t for t in (T if isinstance(T, list) else []) if isinstance(t, dict)]
    L = (_read_json(DATA / "loop.json", {}) if loop is None else loop) or {}
    shares = store.budget_shares()
    tagged = [store.task_workspace(t) for t in T]
    recent = [w for w in tagged[-12:] if w]
    by = {w["id"]: w for w in all_ws}
    cur_walled = by.get(cur, {}).get("privacy") == "walled"
    out = []
    for w in all_ws:
        wid, walled = w["id"], w["privacy"] == "walled"
        detail = (not walled) or wid == cur
        run = que = wdone = 0
        for t, tag in zip(T, tagged):
            # tagged workers count for their own workspace; untagged (older) ones only where the board
            # shows them: the current workspace, unless it is walled
            mine = tag == wid if tag else (wid == cur and not cur_walled)
            if not mine:
                continue
            st = t.get("status")
            run += st == "running"
            que += st == "queued"
            fin = t.get("finished")
            if st == "done" and isinstance(fin, (int, float)) and fin >= midnight:
                wdone += 1
        bl = backlog_counts(WS.expand(w["backlog"]), today)
        item = {"id": wid, "name": _clean(w["name"], words, 28) or wid, "current": wid == cur, "walled": walled,
                "badges": {"running": run, "queued": que, "ready": bl["ready"],
                           "done_today": max(bl["done_today"], wdone)},
                "usage_share": round(sum(1 for x in recent if x == wid) / len(recent), 3) if recent else 0.0,
                "budget_share": shares.get(wid, 0.0), "focus": None, "last_commit": None}
        if detail:
            fws = str(L.get("focus_ws") or "")
            if L.get("focus") and (WS.norm(fws) in (WS.norm(w["name"]), WS.norm(wid)) or (not fws and wid == cur)):
                item["focus"] = _clean(L["focus"], words, 70)
            repos = [WS.expand(r) for r in w["repos"] if store.path_allowed(WS.expand(r), wid)]
            lc = _last_commit(repos, words, now)
            if lc:
                item["last_commit"] = {"subject": lc["subject"], "ago": lc["ago"]}
        out.append(item)
    return out


# ---- git lanes ------------------------------------------------------------------------------------

def git_lanes(repos, words=None, now: float | None = None, n: int = 10) -> list[dict]:
    from walter.buildview import _git, ago
    now = time.time() if now is None else now
    lanes = []
    for label, repo in repos:
        repo = Path(repo)
        if not (repo / ".git").exists():
            continue
        commits = []
        for line in _git(repo, "log", f"-{n}", "--format=%h%x09%p%x09%ct%x09%s").splitlines():
            parts = line.split("\t", 3)
            if len(parts) < 4:
                continue
            h, parents, ts, subj = parts
            try:
                t = float(ts)
            except ValueError:
                continue
            commits.append({"h": h[:7], "s": _clean(subj, words, 50), "ts": t, "ago": ago(t, now),
                            "merge": len(parents.split()) > 1})
        dirty = bool(_git(repo, "status", "--porcelain", "--untracked-files=no").strip())
        ahead = _git(repo, "rev-list", "--count", "@{u}..HEAD").strip()
        branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() or None
        if branch == "HEAD":   # detached: show the short commit instead
            branch = "@" + (commits[0]["h"] if commits else "?")
        lanes.append({"repo": label, "commits": commits, "dirty": dirty, "branch": _clean(branch, words, 30) if branch else None,
                      "ahead": int(ahead) if ahead.isdigit() else None})
    return lanes


# ---- tests ----------------------------------------------------------------------------------------

def _store():
    try:
        from walter import workspaces as WS
        st = WS.WorkspaceStore()
        return st if st.defined() else None
    except Exception:
        return None


def workspace_of_repo(repo, store=None) -> str | None:
    """The workspace whose repos list holds `repo` (resolved paths), or None."""
    from walter import workspaces as WS
    store = store or _store()
    if store is None:
        return None
    me = _real(repo)
    for w in store.all():
        if any(_real(WS.expand(r)) == me for r in w["repos"]):
            return w["id"]
    return None


def test_targets(repo, store=None) -> list[tuple[Path, Path | None]]:
    """Where a test run in `repo` is recorded: [(result json, face mirror or None)].
    Its workspace's ~/.walter/last-test/<id>.json + face/dashboard-tests-<id>.json; the Walter repo's run
    also keeps the old last-test.json + dashboard-tests.json (compatibility). No workspaces: as before."""
    store = store if store is not None else _store()
    walter_repo = _real(repo) == _real(REPO)
    if store is None:
        return [(DATA / "last-test.json", FACE / "dashboard-tests.json")]
    out = []
    wid = workspace_of_repo(repo, store)
    if wid:
        out.append((DATA / "last-test" / f"{wid}.json", FACE / f"dashboard-tests-{wid}.json"))
    if walter_repo:
        out.append((DATA / "last-test.json", FACE / "dashboard-tests.json"))
    if not out:   # a repo no workspace lists: kept, but no cockpit shows it
        from walter import workspaces as WS
        out.append((DATA / "last-test" / f"repo-{WS.slug(Path(repo).name) or 'x'}.json", None))
    return out


def tests_file(ws: dict | None) -> Path | None:
    """The test result the cockpit shows for workspace scope `ws`: its own file, else the old shared
    last-test.json when that run was made in one of its repos (the Walter repo when none recorded)."""
    if ws is None:
        return DATA / "last-test.json"
    own = DATA / "last-test" / f"{ws['id']}.json"
    if own.exists():
        return own
    raw = _read_json(DATA / "last-test.json")
    if isinstance(raw, dict):
        mine = {_real(p) for _, p in ws["repos"]}
        if _real(raw.get("repo") or REPO) in mine:
            return DATA / "last-test.json"
    return None


def tests_state(path: Path | None = None, now: float | None = None, words=None) -> dict | None:
    """~/.walter/last-test.json (walterctl test / test-summary): counts, running flag, failures."""
    now = time.time() if now is None else now
    d = _read_json(path or DATA / "last-test.json")
    if not isinstance(d, dict):
        return None
    out = {}
    for k in ("passed", "failed", "skipped", "total", "done"):
        if isinstance(d.get(k), (int, float)):
            out[k] = int(d[k])
    running = bool(d.get("running"))
    started = d.get("started")
    if running and isinstance(started, (int, float)) and now - started > 1800:
        running = False  # a run that died without writing its result
    out["running"] = running
    out["ok"] = bool(d["ok"]) if d.get("ok") is not None else out.get("failed", 0) == 0
    w = d.get("when")
    if isinstance(w, (int, float)):
        out["when"] = float(w)
    if isinstance(started, (int, float)):
        out["started"] = float(started)
    out["failures"] = [_clean(f, words, 90) for f in (d.get("failures") or [])[:8] if f]
    out["suites"] = [{"name": _clean(s.get("name"), words, 20), "ok": bool(s.get("ok")),
                      "passed": int(s.get("passed") or 0), "failed": int(s.get("failed") or 0)}
                     for s in (d.get("suites") or [])[:4] if isinstance(s, dict)]
    if d.get("summary"):
        out["summary"] = _clean(d["summary"], words, 80)
    return out


# ---- release + pipeline ---------------------------------------------------------------------------

def remote_release(now: float | None = None, cache: Path | None = None, url: str = RELEASE_URL,
                   fetch=None) -> dict | None:
    """releases.wallter.app latest.json, fetched at most every RELEASE_MAX_AGE s, cached on disk."""
    now = time.time() if now is None else now
    cache = Path(cache or RELEASE_CACHE)
    c = _read_json(cache)
    if isinstance(c, dict) and now - float(c.get("fetched") or 0) < RELEASE_MAX_AGE:
        return c.get("data")
    data = c.get("data") if isinstance(c, dict) else None
    try:
        if fetch:
            body = fetch(url)
        else:
            req = urllib.request.Request(url, headers={"User-Agent": "walter-cockpit"})
            with urllib.request.urlopen(req, timeout=4) as r:
                body = r.read(200_000).decode("utf-8", "replace")
        got = json.loads(body)
        if isinstance(got, dict):
            data = {k: got.get(k) for k in ("version", "date", "published", "sha256", "signature", "sig")
                    if got.get(k) is not None}
            data["signed"] = bool(got.get("signature") or got.get("sig") or got.get("minisig")
                                  or any("sig" in str(k).lower() for k in got))
    except Exception:
        pass  # keep the old data, try again after RELEASE_MAX_AGE
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(cache.name + ".tmp")
        tmp.write_text(json.dumps({"fetched": now, "data": data}))
        os.replace(tmp, cache)
    except OSError:
        pass
    return data


def site_deploy(paths=None) -> dict | None:
    """Last line of the site's deploy log (if any): {when, ok}."""
    for p in paths or SITE_DEPLOY_LOGS:
        p = Path(p)
        try:
            st = p.stat()
            with open(p, "rb") as f:
                f.seek(max(0, st.st_size - 4000))
                lines = [x for x in f.read().decode(errors="replace").splitlines() if x.strip()]
        except OSError:
            continue
        last = lines[-1] if lines else ""
        bad = bool(re.search(r"\b(fail|failed|error)\b", last, re.I))
        return {"when": st.st_mtime, "ok": not bad}
    return None


def _ts_of(s) -> float | None:
    if isinstance(s, (int, float)):
        return float(s)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _test_light(tests: dict | None) -> dict:
    if not tests:
        return {"stage": "test", "state": "idle", "note": "no run yet"}
    if tests.get("running"):
        return {"stage": "test", "state": "run", "note": "running"}
    n = f"{tests.get('failed', 0)} failing" if not tests.get("ok") else f"{tests.get('passed', 0)} passing"
    return {"stage": "test", "state": "ok" if tests.get("ok") else "bad", "note": n}


def pipeline(lanes: list[dict], tests: dict | None, release: dict | None, deploy: dict | None,
             now: float | None = None) -> list[dict]:
    """Five lights: build, test, sign, publish, live -> [{stage, state: ok|run|bad|idle, note}]."""
    from walter.buildview import ago
    now = time.time() if now is None else now
    main = lanes[0] if lanes else None
    st = []
    if not main:
        st.append({"stage": "build", "state": "idle", "note": "no repo"})
    elif main["dirty"]:
        st.append({"stage": "build", "state": "run", "note": "changes in progress"})
    else:
        c = main["commits"][0] if main["commits"] else None
        st.append({"stage": "build", "state": "ok", "note": f"{c['h']} {c['ago']}" if c else "clean"})
    st.append(_test_light(tests))
    if not release or not release.get("version"):
        st.append({"stage": "sign", "state": "idle", "note": "no release"})
        st.append({"stage": "publish", "state": "idle", "note": "no release"})
    else:
        signed = release.get("signed") or release.get("sha256")
        st.append({"stage": "sign", "state": "ok" if signed else "idle",
                   "note": "signed" if release.get("signed") else ("checksum" if signed else "unsigned")})
        t = _ts_of(release.get("date") or release.get("published"))
        st.append({"stage": "publish", "state": "ok",
                   "note": "v" + str(release["version"])[:14] + (f" · {ago(t, now)}" if t else "")})
    if deploy:
        st.append({"stage": "live", "state": "ok" if deploy["ok"] else "bad", "note": "site " + ago(deploy["when"], now)})
    elif len(lanes) > 1 and lanes[1]["commits"]:
        c = lanes[1]["commits"][0]
        st.append({"stage": "live", "state": "ok" if not lanes[1]["dirty"] else "run",
                   "note": f"site {c['ago']}"})
    else:
        st.append({"stage": "live", "state": "idle", "note": "no deploy seen"})
    return st


# ---- pipeline presets per workspace (owner, 11 Nov 10:34: every card follows the current project) ---

PRESETS = ("release", "site", "none")
SITE_LIVE_MAX_AGE = 300
_ASSET_V = re.compile(r"\?v=([0-9a-f]{6,})")
_DEPLOY_MD = re.compile(r"deploy[^`\n]*`([0-9a-f]{6,})[^`\n]*`(?:[^\n]*?\bcommit\s+([0-9a-f]{7,}))?", re.I)


def is_site_repo(repo) -> bool:
    repo = Path(repo)
    return (repo / "build.py").is_file() and (repo / "public").is_dir()


def pipeline_preset(ws: dict | None) -> str:
    """release | site | none (| basic: build + test for any other code repo). A workspace's own
    "pipeline" setting wins; else by repo: the Walter repo -> release, a static site -> site, no repos -> none."""
    if ws is None:
        return "release"
    p = str(ws.get("pipeline") or "").lower()
    if p in PRESETS:
        return p
    repos = [Path(r) for _, r in ws.get("repos") or []]
    if not repos:
        return "none"
    if any(_real(r) == _real(REPO) for r in repos):
        return "release"
    if any(is_site_repo(r) for r in repos):
        return "site"
    return "basic"


def build_id(html: str) -> str | None:
    """A short id for a built page: the hash of its sorted ?v=<asset hash> stamps (build.py adds them), so
    the local build and the live site compare equal when they serve the same assets."""
    import hashlib
    hs = sorted(set(_ASSET_V.findall(str(html or ""))))
    if not hs:
        return None
    return hashlib.sha256(",".join(hs).encode()).hexdigest()[:8]


def _newest(paths, cap: int = 4000) -> float:
    best, n = 0.0, 0
    for p in paths:
        p = Path(p)
        try:
            it = [p] if p.is_file() else (x for x in p.rglob("*") if x.is_file())
            for f in it:
                n += 1
                if n > cap:
                    return best
                best = max(best, f.stat().st_mtime)
        except OSError:
            continue
    return best


def site_build(repo: Path, now: float) -> dict:
    """build light for a static site: public/index.html newer than src/ + build.py + site.config.json."""
    from walter.buildview import ago
    repo = Path(repo)
    idx = repo / "public" / "index.html"
    try:
        built, html = idx.stat().st_mtime, idx.read_text(errors="replace")
    except OSError:
        return {"stage": "build", "state": "idle", "note": "not built"}
    src = _newest([repo / "src", repo / "build.py", repo / "site.config.json"])
    bid = build_id(html)
    if src > built + 1:
        return {"stage": "build", "state": "run", "note": "source changed since build", "build_id": bid}
    return {"stage": "build", "state": "ok", "note": (f"{bid} · " if bid else "") + ago(built, now), "build_id": bid}


def site_deploy_state(repo: Path, now: float) -> dict:
    """deploy light: the site's deploy log if there is one, else the newest deploy id in DEPLOY.md."""
    from walter.buildview import ago, _git
    repo = Path(repo)
    logs = [repo / "deploy.log", repo / ".deploy.log", repo / "logs" / "deploy.log", DATA / "site-deploy.log"]
    d = site_deploy(logs)
    if d:
        return {"stage": "deploy", "state": "ok" if d["ok"] else "bad",
                "note": ("deployed " if d["ok"] else "failed ") + ago(d["when"], now)}
    md = repo / "DEPLOY.md"
    try:
        hits = _DEPLOY_MD.findall(md.read_text(errors="replace"))
    except OSError:
        hits = []
    if not hits:
        return {"stage": "deploy", "state": "idle", "note": "no deploy seen"}
    dep, commit = hits[-1]
    ts = _git(repo, "log", "-1", "--format=%ct", "--", "DEPLOY.md").strip()
    try:
        when = float(ts)
    except ValueError:
        when = md.stat().st_mtime
    return {"stage": "deploy", "state": "ok", "note": f"{dep[:8]} · {ago(when, now)}"}


def site_url(repo: Path) -> str | None:
    c = _read_json(Path(repo) / "site.config.json", {}) or {}
    u = str(c.get("url") or c.get("site_url") or c.get("domain") or "").strip().rstrip("/")
    if not u or not re.fullmatch(r"(https://)?[a-z0-9.-]+\.[a-z]{2,}(/[\w./-]*)?", u, re.I):
        return None
    return u if u.startswith("https://") else "https://" + u


def site_live(url: str, now: float | None = None, cache: Path | None = None, fetch=None) -> dict | None:
    """{status, build_id} of the live site, fetched at most every SITE_LIVE_MAX_AGE s (cached on disk).
    fetch(url) -> (status, body) for tests."""
    now = time.time() if now is None else now
    cache = Path(cache or DATA / "cache" / "site-live.json")
    c = _read_json(cache)
    if isinstance(c, dict) and c.get("url") == url and now - float(c.get("fetched") or 0) < SITE_LIVE_MAX_AGE:
        return c.get("data")
    try:
        if fetch:
            status, body = fetch(url)
        else:
            import urllib.error
            req = urllib.request.Request(url, headers={"User-Agent": "walter-cockpit"})
            try:
                with urllib.request.urlopen(req, timeout=4) as r:
                    status, body = r.status, r.read(400_000).decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                status, body = e.code, ""
        data = {"status": int(status), "build_id": build_id(body)}
    except Exception:
        data = {"status": None, "build_id": None}
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_name(cache.name + ".tmp")
        tmp.write_text(json.dumps({"fetched": now, "url": url, "data": data}))
        os.replace(tmp, cache)
    except OSError:
        pass
    return data


def site_pipeline(repo: Path, tests: dict | None, now: float | None = None, fetch=None,
                  cache: Path | None = None) -> list[dict]:
    """Four lights for a static site: build, test, deploy, live."""
    now = time.time() if now is None else now
    b = site_build(repo, now)
    st = [{k: v for k, v in b.items() if k != "build_id"}]
    st.append(_test_light(tests))
    st.append(site_deploy_state(repo, now))
    url = site_url(repo)
    live = site_live(url, now, cache, fetch) if url else None
    if not live:
        st.append({"stage": "live", "state": "idle", "note": "no site address"})
    elif live.get("status") is None:
        st.append({"stage": "live", "state": "bad", "note": "unreachable"})
    elif live["status"] != 200:
        st.append({"stage": "live", "state": "bad", "note": f"HTTP {live['status']}"})
    elif b.get("build_id") and live.get("build_id") and live["build_id"] != b["build_id"]:
        st.append({"stage": "live", "state": "run", "note": "200 · older build"})
    else:
        st.append({"stage": "live", "state": "ok", "note": "200" + (f" · {live['build_id']}" if live.get("build_id") else "")})
    return st


# ---- gauges ---------------------------------------------------------------------------------------

def cpu_percent(stat_text: str | None = None, cache: Path | None = None) -> float | None:
    """Busy % since the previous call (state in a small cache file between timer runs)."""
    cache = Path(cache or CPU_CACHE)
    try:
        line = (stat_text if stat_text is not None else Path("/proc/stat").read_text()).splitlines()[0]
        v = [int(x) for x in line.split()[1:]]
    except (OSError, ValueError, IndexError):
        return None
    idle, total = v[3] + (v[4] if len(v) > 4 else 0), sum(v)
    prev = _read_json(cache)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"idle": idle, "total": total}))
    except OSError:
        pass
    if not isinstance(prev, dict) or total <= prev.get("total", 0):
        return None
    dt, di = total - prev["total"], idle - prev["idle"]
    return round(max(0.0, min(100.0, 100.0 * (dt - di) / dt)), 1) if dt else None


def mem_percent(text: str | None = None) -> float | None:
    try:
        t = text if text is not None else Path("/proc/meminfo").read_text()
    except OSError:
        return None
    kv = {}
    for line in t.splitlines():
        k, _, v = line.partition(":")
        try:
            kv[k.strip()] = int(v.split()[0])
        except (ValueError, IndexError):
            pass
    tot, av = kv.get("MemTotal"), kv.get("MemAvailable")
    return round(100.0 * (tot - av) / tot, 1) if tot and av is not None else None


def gauges(workers: int = 0, usage: dict | None = None) -> dict:
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = None
    cores = os.cpu_count() or 1
    u = usage if usage is not None else _read_json(FACE / "usage.json", {})
    week = ((u or {}).get("week") or {}).get("pct")
    return {"cpu": cpu_percent(), "mem": mem_percent(), "load": round(load, 2) if load is not None else None,
            "cores": cores, "week": week if isinstance(week, (int, float)) else None, "workers": workers}


def _real(p) -> str:
    try:
        return str(Path(os.path.expanduser(str(p))).resolve())
    except OSError:
        return str(p)


def media_playing() -> bool:
    p = _read_json(FACE / "dashboard-podcast.json", {}) or {}
    m = _read_json(DATA / "media.json", {}) or {}
    return bool(p.get("playing")) or bool(m.get("active"))


def cockpit(now: float | None = None, repos=None, words=None) -> dict:
    from walter.buildview import REPOS
    now = time.time() if now is None else now
    ws = scope()
    board = task_board(now, words=words, workspace=ws)
    if repos is None and ws is not None:
        repos = ws["repos"]
    lanes = git_lanes(REPOS if repos is None else repos, words, now)
    tests_hidden = False
    if ws is None:
        tests = tests_state(now=now, words=words)
    else:
        # the whole cockpit follows the workspace: its own test run, plan and ship pipeline
        tf = tests_file(ws)
        tests = tests_state(tf, now=now, words=words) if tf else None
        tests_hidden = tests is None and (DATA / "last-test.json").exists()
    preset = pipeline_preset(ws)
    if preset == "release":
        release, deploy = remote_release(now), site_deploy()
        if ws is not None:
            mine = {_real(p) for _, p in ws["repos"]}
            if _real(REPO) not in mine:
                release = None
            if _real(HOME / "walter-site") not in mine:
                deploy = None
        pipe = pipeline(lanes, tests, release, deploy, now)
    elif preset == "site":
        site = next((Path(r) for _, r in ws["repos"] if is_site_repo(r)), Path(ws["repos"][0][1]) if ws["repos"] else None)
        pipe = site_pipeline(site, tests, now, cache=DATA / "cache" / f"site-live-{ws['id']}.json") if site else []
    elif preset == "basic":
        pipe = pipeline(lanes, tests, None, None, now)[:2]
    else:
        pipe = []
    try:  # GitHub Actions under the local tests (walter/ghactions.py; cached, 8 s budget, never from the face)
        from walter.ghactions import actions_state
        actions = actions_state(REPOS if repos is None else repos, now, words=words)
    except Exception:
        actions = None
    wid = ws["id"] if ws else None
    if ws is not None:
        migrate_plan()
    plan = plan_path(wid)
    return {"board": board, "lanes": lanes, "tests": tests, "tests_hidden": tests_hidden, "actions": actions,
            "pipeline": pipe, "pipeline_preset": preset,
            "gauges": gauges(workers=sum(1 for c in board["building"] if c.get("status") == "running")),
            "media": media_playing(),
            "workspace": {"id": ws["id"], "name": ws["name"], "walled": ws["walled"]} if ws else None,
            "workspaces": workspaces_overview(now=now, words=words),
            "plan": {"set": plan.exists(), "mtime": plan.stat().st_mtime if plan.exists() else None,
                     "workspace": wid}}


# ---- plan file (walterctl display build plan FILE|clear) ------------------------------------------

PLAN_OUT = FACE / "dashboard-plan.md"     # the old shared plan (used only without workspaces)
PLAN_MAX = 40_000


def plan_path(wid: str | None = None) -> Path:
    """The plan file of a workspace (face/dashboard-plan-<id>.md); the old shared one without workspaces."""
    if not wid:
        return FACE / "dashboard-plan.md"
    from walter import workspaces as WS
    return FACE / f"dashboard-plan-{WS.slug(wid) or 'x'}.md"


def plan_owner(store=None) -> str | None:
    """The workspace an old shared plan belongs to: the one holding the Walter repo, else the default."""
    store = store if store is not None else _store()
    if store is None:
        return None
    return workspace_of_repo(REPO, store) or store.default_id()


def migrate_plan(store=None) -> str | None:
    """Move an old shared face/dashboard-plan.md to its workspace's file (once). Returns that id."""
    legacy = FACE / "dashboard-plan.md"
    if not legacy.exists():
        return None
    wid = plan_owner(store)
    if not wid:
        return None
    target = plan_path(wid)
    try:
        if not target.exists():
            os.replace(legacy, target)
        else:
            legacy.unlink()     # the per-workspace file is newer: the shared copy is stale
    except OSError:
        return None
    return wid


def redact_plan(text: str, words=None) -> str:
    """Line by line: tokens, emails, IPs, private words out; paths cut to their last part."""
    from walter.buildview import clean_code, _PATH
    out = []
    for line in str(text or "")[:PLAN_MAX].splitlines():
        line = _PATH.sub(lambda m: m.group(0).rstrip("/").rsplit("/", 1)[-1], line)
        out.append(clean_code(line, words, 400))
    return "\n".join(out) + "\n"


def install_plan(src: Path, words=None, out: Path | None = None) -> Path:
    from walter.buildview import private_words
    words = private_words() if words is None else words
    out = Path(out or plan_path(None))
    body = redact_plan(Path(src).read_text(errors="replace"), words)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(body)
    os.replace(tmp, out)
    return out


# ---- walterctl test: pytest -q + node tests -> ~/.walter/last-test.json (with a running state) -----

def _write(path: Path, d: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    os.replace(tmp, path)


def parse_failures(text: str) -> list[str]:
    """pytest -q 'FAILED tests/x.py::test_y - AssertionError' lines and node 'AssertionError' file names."""
    out = []
    for line in str(text or "").splitlines():
        m = re.match(r"^(?:FAILED|ERROR)\s+(\S+)(?:\s+-\s+(.*))?", line)
        if m:
            out.append(m.group(1) + (" - " + m.group(2) if m.group(2) else ""))
    return out


FACE_TESTS = FACE / "dashboard-tests.json"


def _pub(path: Path, d: dict, face: Path | None = None) -> None:
    """last-test.json + a redacted mirror the face can poll fast (face/dashboard-tests.json).
    path may also be a list of (result, mirror-or-None) pairs (walterctl test: one per workspace file)."""
    if isinstance(path, list):
        for p, f in path:
            if f is None:
                _write(p, d)
            else:
                _pub(p, d, f)
        return
    _write(path, d)
    try:
        from walter.buildview import private_words
        words = private_words()
        m = dict(d)
        m.pop("repo", None)   # a path: stays in last-test.json, not in the face's file
        m["failures"] = [_clean(f, words, 90) for f in d.get("failures") or []][:8]
        m["summary"] = _clean(d.get("summary") or "", words, 80)
        _write(Path(face or FACE_TESTS), m)
    except OSError:
        pass


def run_tests(repo: Path | None = None, args=None, path: Path | None = None, node: bool = True,
              echo=print) -> int:
    """Run the suites, keeping last-test.json current (running -> result). Returns the exit code."""
    import subprocess
    import shutil
    from walter.buildview import parse_pytest
    repo = Path(repo or REPO)
    path = Path(path) if path else test_targets(repo)
    wid = workspace_of_repo(repo) if not isinstance(path, Path) else None
    started = time.time()
    node_files = sorted([*repo.glob("tests/*.test.js"), *repo.glob("tests/*.test.mjs")]) \
        if node and shutil.which("node") and not args else []
    state = {"running": True, "started": started, "when": started, "passed": 0, "failed": 0,
             "total": None, "done": 0, "ok": True, "suites": [], "failures": [], "summary": "running",
             "repo": str(repo), "workspace": wid}
    _pub(path, state)
    # pytest
    py = next((str(x) for x in (repo / ".venv" / "bin" / "python", REPO / ".venv" / "bin" / "python") if x.exists()),
              sys.executable or "python3")
    cmd = [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", *(args or ["tests"])]
    py_out = ""
    try:
        proc = subprocess.Popen(cmd, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        last = 0.0
        for line in proc.stdout:
            py_out += line
            if echo:
                echo(line.rstrip("\n"))
            dots = re.match(r"^[.FEsxX]+(?:\s+\[\s*(\d+)%\])?\s*$", line.strip())
            if dots:
                state["done"] += len(line.strip().split()[0])
                state["failed"] += sum(1 for ch in line.split()[0] if ch in "FE")
                state["passed"] = state["done"] - state["failed"]
                if dots.group(1):
                    pct = int(dots.group(1))
                    state["total"] = int(round(state["done"] * 100 / pct)) if pct else None
                if time.time() - last > 0.5:
                    last = time.time()
                    _pub(path, state)
        rc = proc.wait()
    except OSError as e:
        py_out, rc = str(e), 2
    p = parse_pytest(py_out) or {"passed": state["passed"], "failed": state["failed"] or (1 if rc else 0),
                                 "skipped": 0, "ok": rc == 0}
    suites = [{"name": "pytest", "ok": rc == 0 or rc == 5, "passed": p["passed"], "failed": p["failed"]}]
    failures = parse_failures(py_out)
    passed, failed, skipped = p["passed"], p["failed"], p.get("skipped", 0)
    state.update(passed=passed, failed=failed, suites=suites, failures=failures)
    _pub(path, state)
    # node tests (each file is a test)
    npass = nfail = 0
    for f in node_files:
        try:
            cmd = ["node", "--test", str(f)] if f.suffix == ".mjs" else ["node", str(f)]  # node:test files
            r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=120)
            ok = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok, r = False, None
        npass, nfail = npass + ok, nfail + (not ok)
        if not ok:
            msg = ((r.stderr or r.stdout) if r else "did not run").strip().splitlines()
            msg = next((x for x in msg if "Error" in x), msg[0] if msg else "failed")
            failures.append(f"{f.name} - {msg.strip()[:80]}")
        if echo:
            echo(f"node {f.name}: {'ok' if ok else 'FAILED'}")
        state.update(passed=passed + npass, failed=failed + nfail, failures=failures)
        _pub(path, state)
    if node_files:
        suites.append({"name": "node", "ok": nfail == 0, "passed": npass, "failed": nfail})
    failed_all = failed + nfail
    ok = failed_all == 0 and rc in (0, 5)
    secs = time.time() - started
    summary = f"{failed_all} failed, " if failed_all else ""
    summary += f"{passed + npass} passed" + (f", {skipped} skipped" if skipped else "") + f" in {secs:.1f}s"
    final = {"running": False, "started": started, "when": time.time(), "passed": passed + npass,
             "failed": failed_all, "skipped": skipped, "total": passed + npass + failed_all + skipped,
             "ok": ok, "suites": suites, "failures": failures[:12], "summary": summary, "repo": str(repo),
             "workspace": wid}
    _pub(path, final)
    if echo:
        echo(("PASS " if ok else "FAIL ") + summary)
    return 0 if ok else 1
