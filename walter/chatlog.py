"""Full chat history for the companion app (the owner, 10 Nov 13:01: "the full chat history in the
PWA so I can go back and look; NOT on the wall").

Append-only JSONL at ~/.walter/chat-history.jsonl (mode 0600), one record per message:
  {"id": 12, "ts": 1762800000.5, "role": "person"|"walter", "text": "...",
   "source": "wall"|"app"|"face:<name>", "workspace": "acme"}
Written from ONE place per direction (walter/conductor.py: the person's words when the turn is
accepted, Walter's spoken reply when it is complete). Never stores system notes, heartbeats,
routines, worker reports or "[silent]" replies (the conductor only calls in for person turns).

- ids are monotonic across both files (read from the newest record under a file lock)
- consecutive identical records (same role + text within an hour) are dropped
- 20 MB cap: chat-history.jsonl -> chat-history.1.jsonl (the older .1 is replaced; 2 files kept)
- tokens / keys are redacted (walter.workerlive's provider-token patterns); a turn that looks
  like a credential being pasted is not stored at all
Readers: page() (newest first, before=<id>, substring search q over both files), used by
/api/app/history (walter/app_history.py) and `walterctl chat log|search`.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import time
from pathlib import Path

MAX_BYTES = 20 * 1024 * 1024
MAX_TEXT = 8000
DEDUPE_S = 3600
ROLES = ("person", "walter")
BLOCK = 64 * 1024

# Emotion / action / control tags: [happy] [action:scoot:left] [think:hard] [silent] [laugh]
TAG_RE = re.compile(r"\[[a-zA-Z][a-zA-Z_:\- ]{0,30}\]")
# Turn headers the conductor adds: "[typed in the companion app on Phone ...]\n", "[heard ...]\n"
HEADER_RE = re.compile(r"^\s*\[(?:typed|heard|message via)[^\]\n]*\]\s*\n?", re.I)

# Provider-shaped tokens and long opaque blobs (same patterns as walter.workerlive.redact), plus
# "password: x" / "token=x". Plain words after "key" stay ("the key is under the mat").
_TOKEN = re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,}|gh[pousr]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,}"
                    r"|xox[abpr]-[A-Za-z0-9\-]{8,}|AKIA[0-9A-Z]{12,}|AIza[0-9A-Za-z_\-]{20,}"
                    r"|ya29\.[0-9A-Za-z_\-]+|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-.]+)")
_BLOB = re.compile(r"\b[A-Za-z0-9+_\-/]{40,}={0,2}")
_KV = re.compile(r"(?i)\b(api[ _-]?key|token|secret|password|passwd|passcode|bearer|authorization)"
                 r"(\s*[:=]\s*)\S+")
_PASTE_HINT = re.compile(r"(?i)\b(my|the)\s+(password|passcode|pin|api key|secret key|private key)\s+(is|=|:)")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def default_path() -> Path:
    return Path(os.environ.get("WALTER_DATA", Path.home() / ".walter")) / "chat-history.jsonl"


def strip_tags(text: str) -> str:
    """Spoken text without face tags or turn headers; whitespace collapsed."""
    t = str(text or "")
    while True:
        new = HEADER_RE.sub("", t, count=1)
        if new == t:
            break
        t = new
    t = TAG_RE.sub(" ", t)
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r" *\n *", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def looks_like_credential(text: str) -> bool:
    """A pasted key / password / token: don't keep the message at all."""
    t = (text or "").strip()
    if not t:
        return False
    if _PRIVATE_KEY.search(t) or _PASTE_HINT.search(t):
        return True
    words = t.split()
    if len(words) <= 3 and _TOKEN.search(t):
        return True
    if len(words) == 1 and len(t) >= 16 and re.search(r"[A-Za-z]", t) and re.search(r"\d", t) \
            and not re.match(r"https?://", t):
        return True   # one opaque word like "x7Kq2...": a pasted secret, not chat
    return False


def redact(text: str) -> str:
    t = _TOKEN.sub("[redacted]", text)
    t = _KV.sub(lambda m: m.group(1) + m.group(2) + "[redacted]", t)
    return _BLOB.sub("[redacted]", t)


def clean(text: str) -> str:
    """Tags out, secrets redacted, capped. '' = nothing worth storing."""
    t = strip_tags(text)
    if not t or t.lower() in ("silent", "[silent]"):
        return ""
    t = redact(t)
    if len(t) > MAX_TEXT:
        t = t[: MAX_TEXT - 1].rstrip() + "…"
    return t


def _norm(s: str) -> str:
    return " ".join(str(s or "").lower().split())


def _tail_record(path: Path) -> dict | None:
    """The last valid JSON line of a file (reads only its end)."""
    for rec in _reverse_records(path):
        return rec
    return None


def _reverse_lines(path: Path):
    try:
        f = open(path, "rb")
    except OSError:
        return
    with f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        rest = b""
        while pos > 0:
            step = min(BLOCK, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + rest
            lines = buf.split(b"\n")
            rest = lines[0]
            for line in reversed(lines[1:]):
                if line.strip():
                    yield line
        if rest.strip():
            yield rest


def _reverse_records(path: Path):
    for line in _reverse_lines(path):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("id"), int):
            yield rec


class ChatLog:
    def __init__(self, path: Path | str | None = None, max_bytes: int = MAX_BYTES):
        self.path = Path(path) if path else default_path()
        self.max_bytes = max_bytes

    @property
    def old_path(self) -> Path:
        return self.path.with_name(self.path.name.replace(".jsonl", "") + ".1.jsonl")

    # ------------------------------------------------------------ writing
    def append(self, role: str, text: str, source: str = "wall", workspace: str = "",
               ts: float | None = None, raw: bool = False) -> dict | None:
        """Store one message. Returns the record, or None if skipped (empty, silent, a
        credential, or a repeat of the previous record)."""
        if role not in ROLES:
            raise ValueError("role must be person or walter")
        if not raw and looks_like_credential(strip_tags(text)):
            return None
        t = clean(text)
        if not t:
            return None
        ts = float(ts if ts is not None else time.time())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + ".lock", "a") as lk:
            os.chmod(lk.name, 0o600)
            fcntl.flock(lk, fcntl.LOCK_EX)
            last = _tail_record(self.path) or _tail_record(self.old_path)
            if last and last.get("role") == role and _norm(last.get("text")) == _norm(t) \
                    and abs(ts - float(last.get("ts") or 0)) < DEDUPE_S:
                return None
            rec = {"id": int(last["id"]) + 1 if last else 1, "ts": round(ts, 3), "role": role, "text": t,
                   "source": str(source or "wall")[:40], "workspace": str(workspace or "")[:40]}
            try:
                if self.path.stat().st_size >= self.max_bytes:
                    os.replace(self.path, self.old_path)
            except FileNotFoundError:
                pass
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (json.dumps(rec, ensure_ascii=False) + "\n").encode())
            finally:
                os.close(fd)
            os.chmod(self.path, 0o600)
            return rec

    # ------------------------------------------------------------ reading
    def iter_newest(self):
        """Every record, newest first, across both files."""
        yield from _reverse_records(self.path)
        yield from _reverse_records(self.old_path)

    def page(self, before: int | None = None, limit: int = 50, q: str | None = None) -> dict:
        """{"items": newest-first records (id < before, text containing q), "has_more": bool}."""
        limit = max(1, min(int(limit), 500))
        needle = _norm(q) if q else ""
        out: list[dict] = []
        has_more = False
        for rec in self.iter_newest():
            if before is not None and rec["id"] >= before:
                continue
            if needle and needle not in _norm(rec.get("text")):
                continue
            if len(out) >= limit:
                has_more = True
                break
            out.append(rec)
        return {"items": out, "has_more": has_more}


# ---------------------------------------------------------------- the conductor's hook
_default: ChatLog | None = None


def log_message(role: str, text: str, source: str = "wall", workspace: str = "") -> dict | None:
    """Best effort: never raises (a full disk must not break a conversation)."""
    global _default
    try:
        if _default is None or _default.path != default_path():
            _default = ChatLog()
        return _default.append(role, text, source, workspace)
    except Exception:
        import logging
        logging.getLogger("walter.chatlog").exception("chat log write failed")
        return None
