"""Build view data for conversation mode (owner, 9 Nov 13:14: "more about development building").

Writes face/dashboard-build.json for face/buildview.js: what we shipped lately (the last few git
commits of ~/walter and ~/walter-site as short plain lines), the latest release (latest.json) and
the last full test run (~/.walter/last-test.json, if something wrote it). Running workers, the loop
and usage already have their own files (dashboard-workers.json, dashboard-loop.json, usage.json).

Standalone (stdlib + walter.workerlive's redaction), run by walter-build.timer every 30 s, so no
Walter restart is needed:

    python3 -m walter.buildview --out face/dashboard-build.json
    python3 -m walter.buildview            # JSON on stdout

Lines go through workerlive.redact (tokens) and ~/.walter/private-words.txt (family names, town,
addresses: replaced with "…"); absolute paths are cut to their last part.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

try:
    from walter.workerlive import redact, write_atomic
except Exception:  # pragma: no cover - run as a bare script
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from walter.workerlive import redact, write_atomic

HOME = Path.home()
DATA = HOME / ".walter"
REPO = Path(__file__).resolve().parent.parent
MAX_SUBJECT = 60
REPOS = [("Walter", REPO), ("Site", HOME / "walter-site")]
RELEASE_FILES = [DATA / "releases-mirror" / "latest.json", REPO / "dist" / "latest.json"]

_NOISE = [
    re.compile(r"\[skip ci\]|\[ci skip\]", re.I),
    re.compile(r"Co-Authored-By:.*$", re.I),
    re.compile(r"🤖.*$"),
]
_PATH = re.compile(r"(?:~|/home/[^/\s]+|/tmp|/etc|/usr|/var)(?:/[^\s,;:)]+)+")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def private_words(path: Path | None = None) -> list[str]:
    p = path or (DATA / "private-words.txt")
    try:
        return [w.strip() for w in p.read_text().splitlines() if w.strip() and not w.startswith("#")]
    except OSError:
        return []


def clean_subject(s: str, words: list[str] | None = None, n: int = MAX_SUBJECT) -> str:
    s = str(s or "").splitlines()[0] if s else ""
    for r in _NOISE:
        s = r.sub("", s)
    s = _PATH.sub(lambda m: m.group(0).rstrip("/").rsplit("/", 1)[-1], s)
    s = _EMAIL.sub("…", s)
    s = _IP.sub("…", s)
    for w in words or []:
        s = re.sub(r"(?i)(?<!\w)" + re.escape(w) + r"(?!\w)", "…", s)
    s = " ".join(redact(s).split()).strip(" -:;,")
    return s if len(s) <= n else s[: n - 1].rstrip(" ,;:-") + "…"


def ago(ts: float, now: float) -> str:
    d = max(0, int(now - ts))
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{d // 60} min ago"
    if d < 86400:
        h = d // 3600
        return f"{h} hour{'s' if h > 1 else ''} ago"
    days = d // 86400
    return "yesterday" if days == 1 else f"{days} days ago"


def git_commits(repo: Path, n: int = 6) -> list[dict]:
    if not (repo / ".git").exists():
        return []
    try:
        out = subprocess.run(["git", "-C", str(repo), "log", f"-{n}", "--no-merges", "--format=%ct%x09%s"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    rows = []
    for line in out.splitlines():
        ts, _, subj = line.partition("\t")
        try:
            rows.append({"ts": float(ts), "subject": subj})
        except ValueError:
            pass
    return rows


def release_info(paths=None) -> dict | None:
    for p in paths or RELEASE_FILES:
        try:
            d = json.loads(Path(p).read_text())
        except (OSError, ValueError):
            continue
        if d.get("version"):
            return {"version": str(d["version"])[:20], "date": str(d.get("date") or "")[:25]}
    return None


def test_info(path: Path | None = None) -> dict | None:
    """~/.walter/last-test.json: {"passed": N, "failed": N, "when": epoch|ISO, "ok": bool} (any writer)."""
    try:
        d = json.loads((path or DATA / "last-test.json").read_text())
    except (OSError, ValueError):
        return None
    out = {}
    for k in ("passed", "failed", "skipped"):
        if isinstance(d.get(k), (int, float)):
            out[k] = int(d[k])
    ok = d.get("ok")
    out["ok"] = bool(ok) if ok is not None else out.get("failed", 0) == 0
    w = d.get("when") or d.get("ts") or d.get("updated")
    if isinstance(w, (int, float)):
        out["when"] = float(w)
    return out


def snapshot(now: float | None = None, repos=None, words=None, n: int = 6) -> dict:
    now = time.time() if now is None else now
    words = private_words() if words is None else words
    if repos is None:
        repos = scoped_repos()
    commits = []
    for label, repo in (REPOS if repos is None else repos):
        for c in git_commits(Path(repo), n):
            line = clean_subject(c["subject"], words)
            if line:
                commits.append({"repo": label, "line": line, "ts": c["ts"], "ago": ago(c["ts"], now)})
    commits.sort(key=lambda c: -c["ts"])
    rel, tpath = release_info(), None
    if repos is not None:   # a workspace: its own test run; the Walter release only where the Walter repo is
        try:
            from walter.cockpit import scope, tests_file
            ws = scope()
        except Exception:
            ws = None
        if ws is not None:
            tpath = tests_file(ws) or Path("/nonexistent/last-test.json")
            if not any(str(Path(r).resolve()) == str(REPO.resolve()) for _, r in repos):
                rel = None
    t = test_info(tpath)
    if t and "when" in t:
        t["ago"] = ago(t["when"], now)
    out = {"updated": now, "shipped": commits[:n], "release": rel, "tests": t}
    try:
        out.update(ide(now, repos=repos, words=words))
    except Exception as e:  # pragma: no cover - the basic view must never break
        out["ide_error"] = type(e).__name__
    return out


# ---- the voice-IDE deep view (the owner, 9 Nov 13:16) -------------------------------------------------
# Changed files, a live diff of the newest change, a mini git graph and the workers' recent steps.
# Strict limits; same redaction as above; secret-looking files are never read or shown.

DIFF_MAX_LINES = 80        # the face shows a ~25-line window, scrolled to the end, "scroll up" goes back
DIFF_LINE_CHARS = 100
_SECRET_FILE = re.compile(r"(^|/)(secrets?|\.ssh|\.gnupg)(/|$)|\.(key|pem|p12|pfx|crt|keystore)$|(^|/)\.env(\.|$)"
                          r"|(^|/)(id_rsa|id_ed25519|credentials|.*token.*|private-words\.txt)$", re.I)


def is_secret_file(path: str) -> bool:
    return bool(_SECRET_FILE.search(str(path or "").strip().strip('"')))


def _git(repo: Path, *args, timeout: float = 5) -> str:
    try:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                              timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def clean_code(s: str, words: list[str] | None = None, n: int = DIFF_LINE_CHARS) -> str:
    """One diff line: tokens, emails, IPs and private words redacted; tabs expanded; capped."""
    s = str(s or "").replace("\t", "    ").rstrip()
    s = _EMAIL.sub("…", s)
    s = _IP.sub("…", s)
    for w in words or []:
        s = re.sub(r"(?i)(?<!\w)" + re.escape(w) + r"(?!\w)", "…", s)
    s = redact(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def changed_files(repo: Path) -> list[dict]:
    """git status --short, secrets dropped, newest-modified first: [{st, file, mtime}]."""
    rows = []
    for line in _git(repo, "status", "--short", "--untracked-files=normal").splitlines():
        if len(line) < 4:
            continue
        st, f = line[:2].strip() or "M", line[3:].strip()
        if " -> " in f:
            f = f.split(" -> ", 1)[1]
        f = f.strip('"')
        if is_secret_file(f) or f.endswith("/"):
            continue
        try:
            mt = (repo / f).stat().st_mtime
        except OSError:
            mt = 0.0
        rows.append({"st": st, "file": f, "mtime": mt})
    rows.sort(key=lambda r: -r["mtime"])
    return rows


def _shortstat(text: str) -> str:
    m = re.search(r"(\d+) files? changed(?:, (\d+) insertions?\(\+\))?(?:, (\d+) deletions?\(-\))?", text)
    if not m:
        return ""
    return f"{m.group(1)} file{'s' if m.group(1) != '1' else ''}, +{m.group(2) or 0} −{m.group(3) or 0}"


def parse_diff(text: str, words=None, max_lines: int = DIFF_MAX_LINES) -> list[dict]:
    """Unified diff -> the LAST hunks' lines (newest change at the end): [{k: + - space @, s}]."""
    out = []
    for raw in text.splitlines():
        if raw.startswith(("diff --git", "index ", "--- ", "+++ ", "new file", "deleted file", "similarity",
                           "rename ", "old mode", "new mode", "Binary files", "\\ No newline")):
            continue
        if raw.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@\s*(.*)", raw)
            out.append({"k": "@", "s": clean_code(f"line {m.group(1)}  {m.group(2)}" if m else raw, words, 70)})
            continue
        k = raw[:1] if raw[:1] in "+- " else " "
        out.append({"k": k, "s": clean_code(raw[1:], words)})
    # keep up to max_lines, ending just after the last real change (+2 lines of context)
    last = max((i for i, r in enumerate(out) if r["k"] in "+-"), default=len(out) - 1)
    end = min(len(out), last + 3)
    return out[max(0, end - max_lines): end]


def diff_of(repo: Path, changes: list[dict], words=None) -> dict | None:
    """Live diff of the most recently modified changed file; else the newest commit's last change."""
    for c in changes[:5]:
        f = c["file"]
        if c["st"] == "??":
            try:
                p = repo / f
                if p.stat().st_size > 400_000:
                    continue
                body = p.read_text(errors="replace").splitlines()[-DIFF_MAX_LINES:]
            except (OSError, ValueError):
                continue
            lines = [{"k": "+", "s": clean_code(x, words)} for x in body]
        else:
            lines = parse_diff(_git(repo, "diff", "-U3", "HEAD", "--", f), words)
        if lines:
            return {"file": clean_subject(f, words, 70), "lines": lines, "live": True, "mtime": c["mtime"]}
    show = _git(repo, "show", "-U3", "--format=", "HEAD")
    chunks = [c for c in re.split(r"(?m)^(?=diff --git )", show) if c.startswith("diff --git")]
    for chunk in reversed(chunks):
        m = re.match(r"diff --git a/\S+ b/(\S+)", chunk)
        if not m or is_secret_file(m.group(1)):
            continue
        lines = parse_diff(chunk, words)
        if lines:
            return {"file": clean_subject(m.group(1), words, 70), "lines": lines, "live": False}
    return None


def git_graph(repo: Path, words=None, now: float | None = None, n: int = 8) -> list[dict]:
    now = time.time() if now is None else now
    rows = []
    for line in _git(repo, "log", f"-{n}", "--format=%h%x09%p%x09%ct%x09%D%x09%s").splitlines():
        parts = line.split("\t", 4)
        if len(parts) < 5:
            continue
        h, parents, ts, refs, subj = parts
        ref = ""
        for r in [x.strip() for x in refs.split(",") if x.strip()]:
            r = r.replace("HEAD -> ", "")
            if r != "HEAD" and not r.startswith("tag:"):
                ref = r[:20]
                break
        try:
            t = float(ts)
        except ValueError:
            t = now
        rows.append({"h": h[:7], "merge": len(parents.split()) > 1, "ref": ref,
                     "s": clean_subject(subj, words, 54), "ago": ago(t, now), "ts": t})
    return rows


def _tail_entries(path: Path, nbytes: int = 300_000) -> list[dict]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - nbytes))
            data = f.read()
    except OSError:
        return []
    lines = data.splitlines()
    if size > nbytes:
        lines = lines[1:]  # first one is cut
    out = []
    for raw in lines:
        try:
            out.append(json.loads(raw))
        except (ValueError, UnicodeDecodeError):
            pass
    return out


def worker_logs(now: float | None = None, data_dir: Path | None = None, n: int = 6, words=None) -> dict:
    """Recent steps of each running worker (from its live transcript): {id: [{s, ago}]}."""
    from walter import workerlive as WL
    now = time.time() if now is None else now
    data = Path(data_dir or DATA)
    try:
        tasks = json.loads((data / "tasks.json").read_text())
    except (OSError, ValueError):
        return {}
    cfg = WL._cfg()
    b = cfg.get("brain", {}) if isinstance(cfg.get("brain"), dict) else {}
    cwd = os.path.expanduser(str(b.get("home") or (data / "home")))
    out, claimed = {}, set()
    running = [t for t in tasks if isinstance(t, dict) and t.get("status") == "running"]
    try:  # only the current workspace's workers (walter/workspaces.py task_visible)
        from walter.cockpit import scope
        ws = scope()
    except Exception:
        ws = None
    if ws is not None:
        running = [t for t in running if ws["store"].task_visible(t, ws["id"])]
    running = running[:4]
    for t in running:
        p = WL.find_transcript(t.get("title") or "", t.get("started") or 0, cwd, claimed)
        if not p:
            continue
        claimed.add(p)
        steps = []
        for e in _tail_entries(p):
            msg = e.get("message") if isinstance(e.get("message"), dict) else None
            if e.get("type") != "assistant" or not msg:
                continue
            ts = WL._ts(e) or now
            for c in msg.get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    line = WL.describe_tool(c.get("name") or "", c.get("input") or {})
                    steps.append({"s": clean_subject(line, words, 70), "ago": ago(ts, now)})
        out[str(t.get("id"))] = steps[-n:]
    return out


def scoped_repos(face_id: str | None = None):
    """The current workspace's repos (walter/cockpit.scope), so the diff, the shipped list and the git
    lanes follow the workspace; None when no workspaces are defined (then REPOS, as before)."""
    try:
        from walter.cockpit import scope
        ws = scope(face_id)
    except Exception:
        return None
    return None if ws is None else list(ws["repos"])


def focused_repo(repos=None) -> tuple[str, Path] | None:
    """The repo with the newest working change (else the newest commit)."""
    best, best_t = None, -1.0
    for label, repo in (REPOS if repos is None else repos):
        repo = Path(repo)
        if not (repo / ".git").exists():
            continue
        ch = changed_files(repo)
        t = ch[0]["mtime"] if ch and ch[0]["mtime"] else 0.0
        if not t:
            c = git_commits(repo, 1)
            t = c[0]["ts"] if c else 0.0
        if t > best_t:
            best, best_t = (label, repo), t
    return best


def ide(now: float | None = None, repos=None, words=None) -> dict:
    now = time.time() if now is None else now
    words = private_words() if words is None else words
    fr = focused_repo(repos)
    out: dict = {"repo": None, "changes": [], "changes_stat": "", "diff": None, "graph": []}
    if fr:
        label, repo = fr
        ch = changed_files(repo)
        out["repo"] = label
        out["changes"] = [{"st": c["st"], "file": clean_subject(c["file"], words, 60),
                           "ago": ago(c["mtime"], now) if c["mtime"] else ""} for c in ch[:10]]
        out["changes_more"] = max(0, len(ch) - 10)
        out["changes_stat"] = _shortstat(_git(repo, "diff", "--shortstat", "HEAD"))
        out["diff"] = diff_of(repo, ch, words)
        out["graph"] = git_graph(repo, words, now)
    out["worker_log"] = worker_logs(now, words=words)
    try:  # build mode v2 cockpit panels (walter/cockpit.py)
        from walter.cockpit import cockpit
        out["cockpit"] = cockpit(now, repos=repos, words=words)
    except Exception as e:  # pragma: no cover - the basic view must never break
        out["cockpit_error"] = type(e).__name__
    return out


# ---- last test result (optional) ----------------------------------------------------------------

_PYTEST = re.compile(r"(?:(\d+) failed)?[ ,]*(?:(\d+) passed)?[ ,]*(?:(\d+) skipped)?.*?\bin (\d+(?:\.\d+)?)s\b")


def parse_pytest(text: str) -> dict | None:
    """The newest pytest summary line ('3 failed, 120 passed, 2 skipped in 4.5s') -> counts."""
    for line in reversed(str(text or "").splitlines()):
        if not re.search(r"\b\d+ (passed|failed)\b", line) or not re.search(r"\bin \d+(\.\d+)?s\b", line):
            continue
        cnt = {k: int(v) for v, k in re.findall(r"(\d+) (passed|failed|skipped|errors?)\b", line)}
        failed = cnt.get("failed", 0) + cnt.get("error", 0) + cnt.get("errors", 0)
        return {"passed": cnt.get("passed", 0), "failed": failed, "skipped": cnt.get("skipped", 0),
                "ok": failed == 0, "summary": clean_subject(line.strip("= "), [], 80)}
    return None


def record_test(text: str, path: Path | None = None, now: float | None = None) -> dict | None:
    d = parse_pytest(text)
    if d:
        d["when"] = time.time() if now is None else now
        write_atomic(path or DATA / "last-test.json", d)
    return d


def scan_logs(log_dir: Path | None = None, path: Path | None = None) -> dict | None:
    """Optional: newest pytest summary in ~/.walter/logs/*.log modified after last-test.json."""
    log_dir, path = Path(log_dir or DATA / "logs"), Path(path or DATA / "last-test.json")
    try:
        since = path.stat().st_mtime
    except OSError:
        since = 0.0
    try:
        logs = sorted((p for p in log_dir.glob("*.log") if p.stat().st_mtime > since),
                      key=lambda p: -p.stat().st_mtime)[:5]
    except OSError:
        return None
    for p in logs:
        try:
            with open(p, "rb") as f:
                f.seek(max(0, p.stat().st_size - 32_000))
                tail = f.read().decode(errors="replace")
        except OSError:
            continue
        if parse_pytest(tail):
            return record_test(tail, path, p.stat().st_mtime)
    return None


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--test-summary" in argv:  # walterctl test-summary: pytest -q output piped in
        d = record_test(sys.stdin.read())
        print(("tests: " + d["summary"]) if d else "no pytest summary line found")
        return 0 if d else 1
    out = argv[argv.index("--out") + 1] if "--out" in argv else None
    scan_logs()
    snap = snapshot()
    if out:
        write_atomic(Path(out), snap)
    else:
        print(json.dumps(snap, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
