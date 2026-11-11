"""My autonomy loop's state (9 Nov 2026): what I'm working on, what's next, what's
blocked on my person, and today's wins.

  ~/.walter/loop.json        the full state (walterctl loop status --json)
  face/dashboard-loop.json   the small slice the face's loop chip polls

Each heartbeat (initiative.md) calls `walterctl loop tick` (stamps the run and
refreshes the worker counts) and `walterctl loop focus/next/...` when things change.
Stdlib only: used by the CLI; the server doesn't need to import it (no restart).
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import time
from pathlib import Path

from . import dashboard as D

MAX_TEXT = 160
MAX_BLOCKED = 20
MAX_WINS = 30
FACE_TEXT = 48   # the chip shows ~40 characters; the face trims the rest


def _workspace_name() -> str:
    """The wall face's current workspace name ("" when no workspaces are defined)."""
    try:
        from . import workspaces as WS
        st = WS.WorkspaceStore()
        return st.current(WS.current_face_id())["name"] if st.defined() else ""
    except Exception:
        return ""


def _clean(s) -> str:
    s = " ".join(str(s or "").split())
    return s[:MAX_TEXT]


class LoopStore:
    DEFAULT_PATH: Path | None = None
    DEFAULT_FACE: Path | None = None

    def __init__(self, path=None, face_out=None):
        self.path = Path(path) if path else (self.DEFAULT_PATH or D.data_dir() / "loop.json")
        self.face_out = Path(face_out) if face_out else (self.DEFAULT_FACE or D.FACE_DIR / "dashboard-loop.json")

    # ------------------------------------------------------------ storage
    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            st = self._read()
            yield st
            st["updated"] = time.time()
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(st, indent=1))
            os.replace(tmp, self.path)
            self.publish(st)

    def _read(self) -> dict:
        try:
            st = json.loads(self.path.read_text())
            if not isinstance(st, dict):
                st = {}
        except Exception:
            st = {}
        st.setdefault("focus", "")
        st.setdefault("focus_since", 0)
        st.setdefault("next", "")
        st.setdefault("blocked", [])
        st.setdefault("wins", [])
        st.setdefault("last_run", 0)
        st.setdefault("runs_today", 0)
        st.setdefault("workers", {"running": [], "queued": 0})
        st.setdefault("next_id", 1)
        # wins are per day: yesterday's drop off at the first write today
        today = dt.date.today().isoformat()
        st["wins"] = [w for w in st["wins"] if w.get("date") == today]
        return st

    def state(self) -> dict:
        return self._read()

    # ------------------------------------------------------------ changes
    def set_focus(self, text: str) -> dict:
        with self._locked() as st:
            text = _clean(text)
            if text != st["focus"]:
                st["focus_since"] = time.time() if text else 0
            st["focus"] = text
            st["focus_ws"] = _workspace_name() if text else ""   # the chip says "<workspace>: <focus>"
            return dict(st)

    def set_next(self, text: str) -> dict:
        with self._locked() as st:
            st["next"] = _clean(text)
            return dict(st)

    def block(self, text: str) -> dict:
        text = _clean(text)
        if not text:
            raise ValueError("empty")
        with self._locked() as st:
            for b in st["blocked"]:
                if b["text"].lower() == text.lower():
                    return b
            item = {"id": st["next_id"], "text": text, "since": time.time()}
            st["next_id"] += 1
            st["blocked"] = (st["blocked"] + [item])[-MAX_BLOCKED:]
            return item

    def unblock(self, which="all") -> int:
        """which: 'all', an id, or words matching an item's text."""
        with self._locked() as st:
            before = len(st["blocked"])
            w = str(which).strip()
            if w in ("", "all"):
                st["blocked"] = []
            elif w.lstrip("#").isdigit():
                st["blocked"] = [b for b in st["blocked"] if b["id"] != int(w.lstrip("#"))]
            else:
                st["blocked"] = [b for b in st["blocked"] if w.lower() not in b["text"].lower()]
            return before - len(st["blocked"])

    def win(self, text: str) -> dict:
        text = _clean(text)
        if not text:
            raise ValueError("empty")
        with self._locked() as st:
            item = {"text": text, "at": time.time(), "date": dt.date.today().isoformat()}
            st["wins"] = (st["wins"] + [item])[-MAX_WINS:]
            return item

    def tick(self, running=None, queued=None, now=None) -> dict:
        """A heartbeat ran: stamp it and refresh the worker snapshot."""
        now = now or time.time()
        with self._locked() as st:
            last = st.get("last_run") or 0
            same_day = last and dt.date.fromtimestamp(last) == dt.date.fromtimestamp(now)
            st["runs_today"] = (st.get("runs_today", 0) if same_day else 0) + 1
            st["last_run"] = now
            if running is not None:
                st["workers"] = {"running": [_clean(t)[:80] for t in running][:8],
                                 "queued": int(queued or 0)}
            return dict(st)

    def clear(self) -> None:
        with self._locked() as st:
            st.update(focus="", focus_ws="", focus_since=0, next="", blocked=[])

    # ------------------------------------------------------------ face
    def publish(self, st=None) -> dict:
        st = st if st is not None else self._read()
        ws = st.get("focus_ws") or ""
        focus = (f"{ws}: " if ws and st["focus"] else "") + st["focus"]
        face = {
            "updated": time.time(),
            "focus": focus[:FACE_TEXT],
            "workspace": ws,
            "next": st["next"][:FACE_TEXT],
            "blocked": len(st["blocked"]),
            "blocked_first": st["blocked"][0]["text"][:FACE_TEXT] if st["blocked"] else "",
            "wins": len(st["wins"]),
            "running": len(st["workers"].get("running") or []),
            "queued": st["workers"].get("queued", 0),
            "last_run": st["last_run"],
        }
        try:
            self.face_out.parent.mkdir(parents=True, exist_ok=True)
            D.write_face_json(self.face_out, face)
        except Exception:
            pass
        return face


def _ago(ts, now=None) -> str:
    if not ts:
        return "never"
    s = int((now or time.time()) - ts)
    if s < 90:
        return "just now"
    m = s // 60
    return f"{m} min ago" if m < 90 else f"{m // 60}h {m % 60:02d}m ago"


def format_status(st: dict, now=None) -> str:
    ws = st.get("focus_ws") or ""
    lines = [f"Focus: {(ws + ': ') if ws and st['focus'] else ''}{st['focus'] or '(none set)'}"]
    if st["next"]:
        lines.append(f"Next:  {st['next']}")
    lines.append(f"Last heartbeat: {_ago(st['last_run'], now)} ({st.get('runs_today', 0)} today)")
    w = st.get("workers") or {}
    run = w.get("running") or []
    lines.append(f"Workers: {len(run)} running, {w.get('queued', 0)} queued" +
                 (": " + "; ".join(run) if run else ""))
    if st["blocked"]:
        lines.append("Blocked on you:")
        lines += [f"  #{b['id']} {b['text']} (since {_ago(b['since'], now)})" for b in st["blocked"]]
    else:
        lines.append("Blocked on you: nothing")
    if st["wins"]:
        lines.append("Today's wins:")
        lines += [f"  - {w['text']}" for w in st["wins"]]
    return "\n".join(lines)
