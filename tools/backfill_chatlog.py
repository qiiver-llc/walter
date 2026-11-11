#!/usr/bin/env python3
"""One-time backfill of the app's chat history (walter/chatlog.py) from Walter's own Claude sessions.

Reads the session transcripts under ~/.claude/projects/-home-walter--walter-home/*.jsonl from the
last N days (default 7), keeps only Walter's own brain sessions (worker transcripts start with
"You are a background worker" and are skipped), and imports the spoken conversation:

  person  turns headed "[heard through the microphone ...]", "[heard through the companion app's
          microphone ...]", "[typed in the companion app ...]", "[typed on your keyboard ...]",
          "[heard on face NAME ...]": just the words after the header
  walter  the reply text of that turn, tags and code blocks stripped; "[silent]" and empty skipped

Everything else (heartbeats, routines, reminders, worker reports, presence, walterctl messages,
/model commands, tool calls) is ignored. Text goes through chatlog.clean (tags out, secrets
redacted) and pasted credentials are dropped.

Idempotent: imported records carry "imported": true; if any exist the script does nothing.
If the log already has live records, the import is merged in time order and ids are renumbered
(only messages older than the first live record are imported). Writes only chat-history.jsonl.

  python3 tools/backfill_chatlog.py [--days 7] [--dry-run] [--dir DIR] [--log FILE]
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from walter import chatlog as C  # noqa: E402

TRANSCRIPTS = Path.home() / ".claude" / "projects" / "-home-walter--walter-home"
WORKER_START = "You are a background worker"
# The frame header for a person's turn (walter/conductor.py _frame / _face_frame), at a line start.
FRAME_RE = re.compile(r"(?m)^\[(heard through the microphone|heard through the companion app's microphone"
                      r"|typed in the companion app|typed on your keyboard|heard on face ([^\]\n(;]+?)\s+(?:\(|at ))")
WORKSPACE_RE = re.compile(r"^\[workspace: ([^\]\n]+)\]", re.M)
CODE_RE = re.compile(r"```.*?(```|$)", re.S)


def _ts(s) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _texts(content) -> str | None:
    """A user entry's own text (None for tool results)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parts) if parts else None
    return None


def person_turn(text: str):
    """(words, source, workspace) for a person's turn, else None."""
    if not text or text.lstrip().startswith(WORKER_START):
        return None
    m = None
    for m in FRAME_RE.finditer(text):
        pass
    if m is None:
        return None
    end = text.find("]\n", m.start())
    if end < 0:
        end = text.find("]", m.start())
        if end < 0:
            return None
    words = text[end + 1:].strip()
    kind = m.group(1)
    if kind.startswith("heard on face"):
        source = "face:" + m.group(2).strip()[:30]
    elif "companion app" in kind:
        source = "app"
    else:
        source = "wall"
    ws = WORKSPACE_RE.search(text[: m.start()])
    return words, source, (re.sub(r"[^a-z0-9]+", "-", ws.group(1).lower()).strip("-") if ws else "")


def parse_transcript(path, since: float = 0.0) -> list[dict]:
    """Conversation messages of one session file, in order: {ts, role, text, source, workspace}."""
    out: list[dict] = []
    cur = None            # the open person turn: {"source", "workspace", "reply": [], "ts"}
    first_user = True

    def close():
        nonlocal cur
        if cur and cur["reply"]:
            reply = CODE_RE.sub(" ", " ".join(cur["reply"]))
            out.append({"ts": cur["reply_ts"], "role": "walter", "text": reply, "source": cur["source"],
                        "workspace": cur["workspace"]})
        cur = None

    with open(path, errors="replace") as fh:
        for line in fh:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if not isinstance(r, dict) or r.get("isSidechain"):
                continue
            t = r.get("type")
            if t not in ("user", "assistant"):
                continue
            msg = r.get("message") or {}
            ts = _ts(r.get("timestamp"))
            if t == "user":
                text = _texts(msg.get("content"))
                if text is None:
                    continue          # a tool result inside the current turn
                if first_user:
                    first_user = False
                    if text.lstrip().startswith(WORKER_START):
                        return []     # a background worker's session, not a conversation
                close()
                if ts is None or ts < since:
                    continue
                pt = person_turn(text)
                if pt is None:
                    continue          # heartbeat, routine, worker report, reminder, presence...
                words, source, ws = pt
                out.append({"ts": ts, "role": "person", "text": words, "source": source, "workspace": ws})
                cur = {"source": source, "workspace": ws, "reply": [], "reply_ts": ts}
            elif cur is not None:
                content = msg.get("content")
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                            cur["reply"].append(b["text"].strip())
                            cur["reply_ts"] = ts or cur["reply_ts"]
    close()
    return out


def collect(tdir: Path, days: float) -> list[dict]:
    since = time.time() - days * 86400
    msgs: list[dict] = []
    for f in sorted(Path(tdir).glob("*.jsonl")):
        try:
            if f.stat().st_mtime < since:
                continue
        except OSError:
            continue
        msgs.extend(parse_transcript(f, since))
    msgs.sort(key=lambda m: m["ts"])
    return msgs


def prepare(msgs: list[dict]) -> list[dict]:
    """Cleaned, credential-free, consecutive duplicates dropped (same rules as ChatLog.append)."""
    out: list[dict] = []
    for m in msgs:
        if C.looks_like_credential(C.strip_tags(m["text"])):
            continue
        text = C.clean(m["text"])
        if not text:
            continue
        if out and out[-1]["role"] == m["role"] and C._norm(out[-1]["text"]) == C._norm(text) \
                and m["ts"] - out[-1]["ts"] < C.DEDUPE_S:
            continue
        out.append({**m, "text": text})
    return out


def backfill(log: C.ChatLog, msgs: list[dict], dry_run: bool = False) -> dict:
    log.path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(log.path) + ".lock", "a") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        existing = list(reversed(list(log.iter_newest())))     # oldest first, both files
        if any(r.get("imported") for r in existing):
            return {"imported": 0, "skipped": "already backfilled"}
        first_live = min((r["ts"] for r in existing), default=None)
        new = prepare([m for m in msgs if first_live is None or m["ts"] < first_live])
        if dry_run or not new:
            return {"imported": len(new), "dry_run": dry_run}
        rows = [{**m, "imported": True} for m in new] + existing
        rows.sort(key=lambda r: r["ts"])
        tmp = log.path.with_name(log.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            for i, r in enumerate(rows, 1):
                rec = {"id": i, "ts": round(float(r["ts"]), 3), "role": r["role"], "text": r["text"],
                       "source": r.get("source") or "wall", "workspace": r.get("workspace") or ""}
                if r.get("imported"):
                    rec["imported"] = True
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.chmod(tmp, 0o600)
        if log.old_path.exists():
            os.remove(log.old_path)       # its records are in the rewritten file now
        os.replace(tmp, log.path)
        return {"imported": len(new), "total": len(rows)}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Backfill the app chat history from Walter's sessions.")
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--dir", default=str(TRANSCRIPTS))
    ap.add_argument("--log", default=None, help="chat log file (default ~/.walter/chat-history.jsonl)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    msgs = collect(Path(a.dir), a.days)
    res = backfill(C.ChatLog(a.log), msgs, dry_run=a.dry_run)
    people = sum(1 for m in prepare(msgs) if m["role"] == "person")
    print(json.dumps({**res, "found": len(msgs), "person_turns": people}))


if __name__ == "__main__":
    main()
