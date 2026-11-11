"""Live worker status: what each background worker is doing right now.

Workers run `claude -p --output-format json`, so their stdout only arrives at the
end. But Claude Code writes each session's transcript live, as JSON lines, under
~/.claude/projects/<slug of the worker's cwd>/<session>.jsonl. This module finds
each running worker's transcript (its first prompt contains "Task: <title>") and
turns the latest tool call into one short human line ("Editing face/walter.js",
"Running tests", "Searching the web"), plus a count of files edited.

Standalone on purpose (stdlib only, no walter imports at module level) so the
face part works from a tiny helper without restarting Walter:

    python3 -m walter.workerlive --out face/dashboard-workers.json --loop 3
    python3 -m walter.workerlive            # one JSON snapshot on stdout

Redaction: token-looking strings are replaced, paths under ~/.walter/secrets
are never shown, and lines are capped at ~70 characters.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

HOME = Path.home()
DATA = HOME / ".walter"
REPO = Path(__file__).resolve().parent.parent
MAX_LINE = 70
DONE_KEEP_S = 90          # finished workers stay in the JSON this long (the face fades them at ~60 s)
HEAD_BYTES = 64 * 1024

_TOKEN_RES = [
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,}|gh[pousr]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,}"
               r"|xox[abpr]-[A-Za-z0-9\-]{8,}|AKIA[0-9A-Z]{12,}|AIza[0-9A-Za-z_\-]{20,}"
               r"|ya29\.[0-9A-Za-z_\-]+|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-.]+)"),
    re.compile(r"(?i)\b(token|key|secret|password|passwd|bearer|authorization)\b(\s*[:=]\s*|\s+)\S+"),
    re.compile(r"\b[A-Za-z0-9+_\-]{40,}={0,2}"),   # long opaque blobs
]


def redact(s: str) -> str:
    s = str(s or "")
    if "secrets" in s and (".walter/secrets" in s or "/secrets/" in s):
        return "Handling a secret file"
    s = _TOKEN_RES[0].sub("[redacted]", s)
    s = _TOKEN_RES[1].sub(lambda m: m.group(1) + " [redacted]", s)
    s = _TOKEN_RES[2].sub("[redacted]", s)
    return s


def short(s: str, n: int = MAX_LINE) -> str:
    s = " ".join(redact(s).split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def nice_path(p: str) -> str:
    p = str(p or "")
    for base, rep in ((str(REPO) + "/", ""), (str(DATA / "home") + "/", ""), (str(HOME) + "/", "~/")):
        if p.startswith(base):
            return rep + p[len(base):]
    return p


def project_dir(cwd: str | Path) -> Path:
    """Claude Code's transcript folder for a working directory."""
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude") / "projects"
    return root / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


# ---- turning one tool call into a human line -------------------------------

_BASH_RULES = [
    (re.compile(r"\b(pytest|unittest|run_tests|npm (run )?test)\b"), "Running tests"),
    (re.compile(r"py_compile|compileall"), "Checking Python syntax"),
    (re.compile(r"\bnode\b.*(--check|-c\b)|acorn|esprima|parse.*\.js"), "Checking JavaScript syntax"),
    (re.compile(r"\bgit (commit|add)\b"), "Committing to git"),
    (re.compile(r"\bgit (log|status|diff|show)\b"), "Looking at git history"),
    (re.compile(r"\b(rg|grep|find)\b"), "Searching files"),
    (re.compile(r"\b(curl|wget)\b"), "Fetching from the web"),
    (re.compile(r"\bsystemctl\b|journalctl"), "Checking services"),
    (re.compile(r"\bwalterctl\b"), "Using walterctl"),
    (re.compile(r"\b(pip|apt|apt-get|npm) install\b"), "Installing a package"),
]


def describe_tool(name: str, inp: dict) -> str:
    inp = inp if isinstance(inp, dict) else {}
    n = name or ""
    fp = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or ""
    base = nice_path(fp)
    if n in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"Editing {base}" if base else "Editing a file"
    if n == "Write":
        return f"Writing {base}" if base else "Writing a file"
    if n == "Read":
        return f"Reading {base}" if base else "Reading a file"
    if n in ("Grep", "Glob"):
        pat = inp.get("pattern") or ""
        return f"Searching for {pat}" if pat and len(pat) < 30 else "Searching files"
    if n == "WebSearch":
        q = inp.get("query") or ""
        return f"Searching the web: {q}" if q else "Searching the web"
    if n == "WebFetch":
        return "Reading a web page"
    if n in ("Agent", "Task"):
        d = inp.get("description") or ""
        return f"Helper: {d}" if d else "Handing a part to a helper"
    if n == "TodoWrite":
        return "Planning the next steps"
    if n == "Skill":
        return f"Using the {inp.get('skill') or 'a'} skill"
    if n.startswith("mcp__"):
        low = n.lower()
        for k, v in (("gmail", "Checking Gmail"), ("calendar", "Checking the calendar"),
                     ("event", "Checking the calendar"), ("drive", "Checking Google Drive"),
                     ("browser", "Using the browser")):
            if k in low:
                return v
        return "Using " + n.split("__")[1].replace("_", " ").replace("-", " ")
    if n == "Bash":
        cmd = str(inp.get("command") or "")
        for rx, label in _BASH_RULES:
            if rx.search(cmd):
                return label
        d = inp.get("description") or ""
        if d:
            return d
        m = re.search(r"\b(cat|head|tail|sed -n \S+|less)\s+(\S+)", cmd)
        if m:
            return f"Reading {nice_path(m.group(2))}"
        return "Running a command"
    return f"Using {n}" if n else "Working"


def step_kind(name: str, line: str) -> str:
    """A colour class for the build cockpit's activity stream."""
    n, low = name or "", (line or "").lower()
    if n in ("Edit", "MultiEdit", "NotebookEdit", "Write"):
        return "edit"
    if n == "Read":
        return "read"
    if n in ("WebSearch", "WebFetch") or "web" in low:
        return "web"
    if n in ("Grep", "Glob") or low.startswith("searching"):
        return "search"
    if "test" in low or "syntax" in low:
        return "test"
    if "commit" in low or "git" in low:
        return "git"
    if n == "TodoWrite" or "plan" in low:
        return "plan"
    if n == "Bash":
        return "run"
    return "other"


STEP_KEEP = 40             # last N step lines per worker for the activity stream
_PW = {"t": 0.0, "words": []}


def private_words() -> list[str]:
    """~/.walter/private-words.txt (family names, town...), re-read at most once a minute."""
    if time.time() - _PW["t"] > 60:
        _PW["t"] = time.time()
        try:
            _PW["words"] = [w.strip() for w in (DATA / "private-words.txt").read_text().splitlines()
                            if w.strip() and not w.startswith("#")]
        except OSError:
            _PW["words"] = []
    return _PW["words"]


def scrub(s: str, n: int = MAX_LINE) -> str:
    s = short(s, 10_000)
    s = re.sub(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b", "…", s)
    for w in private_words():
        s = re.sub(r"(?i)(?<!\w)" + re.escape(w) + r"(?!\w)", "…", s)
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


# ---- reading a transcript --------------------------------------------------

def _lines(path: Path, start: int = 0):
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    end = start + len(data)
    if data and not data.endswith(b"\n"):          # half-written last line: leave it for next time
        cut = data.rfind(b"\n") + 1
        end = start + cut
        data = data[:cut]
    out = []
    for raw in data.splitlines():
        try:
            out.append(json.loads(raw))
        except (ValueError, UnicodeDecodeError):
            pass
    return out, end


def _ts(entry: dict) -> float | None:
    t = entry.get("timestamp")
    if not t:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(t).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Tracker:
    """Incremental state for one transcript: byte offset, last activity, files edited."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.activity = "Getting started"
        self.last_ts: float | None = None
        self.files: set[str] = set()
        self.tools = 0
        self.log: list[dict] = []   # last STEP_KEEP {t, k, s} for the activity stream

    def update(self) -> None:
        try:
            if self.path.stat().st_size <= self.offset:
                return
            entries, self.offset = _lines(self.path, self.offset)
        except OSError:
            return
        for e in entries:
            msg = e.get("message") if isinstance(e.get("message"), dict) else None
            if e.get("type") != "assistant" or not msg:
                continue
            ts = _ts(e)
            for c in msg.get("content") or []:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_use":
                    name, inp = c.get("name") or "", c.get("input") or {}
                    self.tools += 1
                    self.activity = describe_tool(name, inp)
                    self.log.append({"t": ts, "k": step_kind(name, self.activity), "s": scrub(self.activity)})
                    if len(self.log) > STEP_KEEP:
                        del self.log[: len(self.log) - STEP_KEEP]
                    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
                        fp = inp.get("file_path") or inp.get("notebook_path")
                        if fp:
                            self.files.add(str(fp))
                    if ts:
                        self.last_ts = ts
                elif c.get("type") == "text" and (c.get("text") or "").strip():
                    if msg.get("stop_reason") == "end_turn":
                        self.activity = "Writing the report"
                    if ts:
                        self.last_ts = ts


def _text_of(x) -> str:
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        return _text_of(x.get("content")) + _text_of(x.get("text"))
    if isinstance(x, list):
        return "".join(_text_of(i) for i in x)
    return ""


def find_transcript(title: str, started: float, cwd: str | Path, claimed: set | None = None) -> Path | None:
    """The transcript whose first prompt carries "Task: <title>", written since the task began."""
    d = project_dir(cwd)
    try:
        cands = [p for p in d.glob("*.jsonl") if p.stat().st_mtime >= (started or 0) - 120]
    except OSError:
        return None
    needle = "Task: " + str(title).strip() + "\n"
    for p in sorted(cands, key=lambda p: p.stat().st_mtime, reverse=True):
        if claimed and p in claimed:
            continue
        try:
            with open(p, "rb") as f:
                head = f.read(HEAD_BYTES)
        except OSError:
            continue
        for raw in head.splitlines()[:12]:
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if needle in _text_of(e.get("content")) or needle in _text_of(e.get("message")):
                return p
    return None


# ---- the snapshot ----------------------------------------------------------

def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def _cfg() -> dict:
    try:
        import tomllib
        with open(DATA / "config.toml", "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


class Snapshotter:
    def __init__(self, data_dir: Path | None = None, cwd: str | Path | None = None):
        self.data = Path(data_dir or DATA)
        cfg = _cfg()
        b = cfg.get("brain", {}) if isinstance(cfg.get("brain"), dict) else {}
        self.cwd = str(cwd or b.get("home") or (self.data / "home"))
        self.cwd = os.path.expanduser(self.cwd)
        self.max_tasks = int(b.get("max_tasks") or 3)
        self.trackers: dict[int, Tracker] = {}

    def snapshot(self, now: float | None = None) -> dict:
        now = now or time.time()
        items = _read_json(self.data / "tasks.json", [])
        items = items if isinstance(items, list) else []
        out = []
        claimed = {t.path for t in self.trackers.values()}
        try:  # each worker's workspace tag, so the cockpit's stream shows only the current workspace
            from walter import workspaces as WS
            wst = WS.WorkspaceStore()
            wst = wst if wst.defined() else None
        except Exception:
            wst = None
        live_ids = set()
        for it in items:
            if not isinstance(it, dict):
                continue
            tid, st = it.get("id"), it.get("status")
            fin = it.get("finished")
            if st != "running" and not (fin and now - fin < DONE_KEEP_S):
                continue
            tr = self.trackers.get(tid)
            if tr is None and st == "running":
                p = find_transcript(it.get("title") or "", it.get("started") or 0, self.cwd, claimed)
                if p:
                    tr = self.trackers[tid] = Tracker(p)
                    claimed.add(p)
            if tr:
                tr.update()
                live_ids.add(tid)
            row = {"id": tid, "title": short(it.get("title") or "", 80), "status": st,
                   "started": it.get("started"), "finished": fin,
                   "elapsed": int((fin or now) - (it.get("started") or now)),
                   "activity": short(tr.activity) if tr else ("Starting up" if st == "running" else ""),
                   "last_activity": tr.last_ts if tr else None,
                   "files": len(tr.files) if tr else 0, "steps": tr.tools if tr else 0,
                   "log": list(tr.log) if tr else [],
                   "workspace": wst.task_workspace(it) if wst else None}
            if st != "running":
                row["activity"] = {"done": "Finished", "failed": "Stopped with a problem",
                                   "cancelled": "Cancelled"}.get(st, st or "")
            out.append(row)
        for tid in list(self.trackers):
            if tid not in live_ids:
                self.trackers.pop(tid, None)
        rem = _read_json(self.data / "reminders.json", [])
        for r in rem if isinstance(rem, list) else []:
            m = re.match(r"QUEUED \[([^\]]+)\]", str((r or {}).get("text") or ""))
            if m:
                out.append({"id": None, "title": short(m.group(1), 80), "status": "queued",
                            "started": None, "finished": None, "elapsed": 0,
                            "activity": "Waiting for a free slot", "last_activity": None,
                            "files": 0, "steps": 0})
        order = {"running": 0, "queued": 1}
        out.sort(key=lambda r: (order.get(r["status"], 2), r.get("started") or 0))
        return {"updated": now, "max_tasks": self.max_tasks,
                "running": sum(1 for r in out if r["status"] == "running"), "workers": out}


def write_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, path)


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", help="write the snapshot here (atomically) instead of stdout")
    ap.add_argument("--loop", type=float, default=0, help="repeat every N seconds while workers run")
    ap.add_argument("--idle", type=float, default=15, help="seconds between checks when nothing runs")
    a = ap.parse_args(argv)
    s = Snapshotter()
    last = None
    while True:
        snap = s.snapshot()
        if a.out:
            body = json.dumps({k: v for k, v in snap.items() if k != "updated"}, sort_keys=True)
            if body != last or not snap["workers"] and last is None:
                write_atomic(Path(a.out), snap)
                last = body
        else:
            print(json.dumps(snap, indent=1))
        if not a.loop:
            return 0
        busy = any(w["status"] in ("running", "done", "failed", "cancelled") for w in snap["workers"])
        time.sleep(a.loop if busy else max(a.loop, a.idle))


if __name__ == "__main__":
    sys.exit(main())
