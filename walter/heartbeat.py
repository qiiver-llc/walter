"""The heartbeat: the wake-up line that starts each idle beat, and the checklist it points at.

The owner asked (11 Nov 10:58) to edit "what the heartbeat says" from the companion app:
  * `[life] heartbeat_prompt` (config, live): the one-line wrapper that wakes the brain on each beat.
    Empty = DEFAULT_PROMPT. Placeholders {time} and {home} are filled in each beat. A custom line that
    never mentions [silent] gets SILENT_RULE appended, so a beat with nothing to do stays quiet.
  * <home>/initiative.md: the checklist the brain follows each beat. The app edits it through
    InitiativeFile: size/encoding checks, a stale-edit guard (base_mtime -> 409), atomic writes and
    up to KEEP timestamped backups under <data>/backups/initiative/ for undo.
"""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

PROMPT_MAX = 1500
INITIATIVE_MAX = 60 * 1024        # bytes, UTF-8
KEEP = 10                         # backups kept

DEFAULT_PROMPT = ("[heartbeat {time}] Nobody has talked to you for a while. "
                  "Follow your initiative checklist in {home}/initiative.md. "
                  "If there's something genuinely useful or kind to say or do "
                  "(a reminder, a follow-up, something you've been working on), do it. "
                  "Otherwise reply only [silent].")
_FOLLOW = "Follow your initiative checklist in {home}/initiative.md. "
SILENT_RULE = "If there's nothing genuinely useful or kind to say or do, reply only [silent]."

_CTRL_LINE = re.compile(r"[\x00-\x1f\x7f]")          # the wake-up line is one line
_CTRL_FILE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")  # the checklist keeps newlines and tabs
BACKUP_RE = re.compile(r"initiative-\d{8}-\d{6}(?:-\d{1,3})?")


class HeartbeatError(ValueError):
    def __init__(self, msg: str, status: int = 400, code: str = "bad_request"):
        super().__init__(msg)
        self.status, self.code = status, code


# ------------------------------------------------------------------ the wake-up line
def clean_prompt(v) -> str:
    """Validate a custom wake-up line: one line (newlines folded to spaces), PROMPT_MAX chars, no control chars.
    "" means: use the built-in line."""
    if not isinstance(v, str):
        raise HeartbeatError("text must be text")
    s = re.sub(r"\s*[\r\n]+\s*", " ", v).replace("\t", " ").strip()
    if len(s) > PROMPT_MAX:
        raise HeartbeatError(f"the wake-up line is {PROMPT_MAX} characters at most")
    if _CTRL_LINE.search(s):
        raise HeartbeatError("the wake-up line can't have control characters")
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        raise HeartbeatError("the wake-up line isn't valid text") from None
    return s


def render(custom: str | None, now: str, home) -> str:
    """The text of one heartbeat turn. Re-built every beat, so a config change applies at the next one."""
    home = Path(home)
    tmpl = (custom or "").strip()
    if not tmpl:
        tmpl = DEFAULT_PROMPT
        if not (home / "initiative.md").exists():
            tmpl = tmpl.replace(_FOLLOW, "")
    elif not tmpl.startswith("[heartbeat"):
        tmpl = "[heartbeat {time}] " + tmpl          # the brain and logs recognise beats by this stamp
    out = tmpl.replace("{time}", now).replace("{home}", str(home))
    if "[silent]" not in out:
        out = out.rstrip() + " " + SILENT_RULE
    return out


# ------------------------------------------------------------------ the checklist file
def clean_initiative(v, allow_empty: bool = False) -> str:
    if not isinstance(v, str):
        raise HeartbeatError("text must be text")
    s = v.replace("\r\n", "\n").replace("\r", "\n")
    try:
        size = len(s.encode("utf-8"))
    except UnicodeEncodeError:
        raise HeartbeatError("that isn't valid text (bad characters)") from None
    if size > INITIATIVE_MAX:
        raise HeartbeatError(f"the checklist is {INITIATIVE_MAX // 1024} KB at most ({size // 1024} KB sent)", 413, "too_big")
    if _CTRL_FILE.search(s):
        raise HeartbeatError("the checklist can't have control characters")
    if not s.strip() and not allow_empty:
        raise HeartbeatError("That would empty the checklist. Send allow_empty to really do it.", 409, "empty")
    if s and not s.endswith("\n"):
        s += "\n"
    return s


class InitiativeFile:
    """<home>/initiative.md with backups. Only ever touches that one file and its backup folder."""

    def __init__(self, home, backup_dir, now=time.time):
        self.path = Path(home) / "initiative.md"
        self.bdir = Path(backup_dir)
        self.now = now
        self._lock = threading.Lock()

    def mtime(self) -> float | None:
        try:
            return self.path.stat().st_mtime
        except FileNotFoundError:
            return None

    def read(self) -> tuple[str, float | None, bool]:
        """(text, mtime, truncated): a file over the limit is shown cut short and can't be saved from the app."""
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return "", None, False
        return raw[:INITIATIVE_MAX].decode("utf-8", errors="replace"), self.mtime(), len(raw) > INITIATIVE_MAX

    def backups(self) -> list[dict]:
        """Newest first: {id, time, size}."""
        out = []
        try:
            for p in self.bdir.iterdir():
                if p.suffix == ".md" and BACKUP_RE.fullmatch(p.stem) and p.is_file() and not p.is_symlink():
                    st = p.stat()
                    out.append({"id": p.stem, "time": st.st_mtime, "size": st.st_size})
        except FileNotFoundError:
            return []
        out.sort(key=lambda b: (b["id"], b["time"]), reverse=True)
        return out

    def _backup(self) -> str | None:
        """Copy the current file into the backup folder (skipped if it matches the newest backup); keep KEEP."""
        try:
            cur = self.path.read_bytes()
        except FileNotFoundError:
            return None
        self.bdir.mkdir(parents=True, exist_ok=True)
        bs = self.backups()
        if bs and (self.bdir / f"{bs[0]['id']}.md").read_bytes() == cur:
            return bs[0]["id"]
        stem = "initiative-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(self.now()))
        bid, n = stem, 1
        while (self.bdir / f"{bid}.md").exists():
            bid, n = f"{stem}-{n}", n + 1
        tmp = self.bdir / f".{bid}.tmp"
        tmp.write_bytes(cur)
        os.replace(tmp, self.bdir / f"{bid}.md")
        for old in self.backups()[KEEP:]:
            (self.bdir / f"{old['id']}.md").unlink(missing_ok=True)
        return bid

    def _write(self, text: str) -> None:
        tmp = self.path.with_name(".initiative.md.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    @staticmethod
    def _same(a, b) -> bool:
        if a is None or b is None:
            return a is None and b is None
        return abs(float(a) - float(b)) < 0.001

    def save(self, text: str, base_mtime) -> str | None:
        """Write the checklist if nobody changed it since base_mtime (else 409). Returns the backup id."""
        if base_mtime is not None and (isinstance(base_mtime, bool) or not isinstance(base_mtime, (int, float))):
            raise HeartbeatError("base_mtime must be the number from the last read")
        with self._lock:
            if not self._same(self.mtime(), base_mtime):
                raise HeartbeatError("The checklist changed since you opened it (Walter may have edited it). "
                                     "Reload, then make your change again.", 409, "conflict")
            bid = self._backup()
            self._write(text)
            return bid

    def restore(self, bid) -> str | None:
        if not isinstance(bid, str) or not BACKUP_RE.fullmatch(bid):
            raise HeartbeatError("pick one of the listed versions")
        with self._lock:
            src = self.bdir / f"{bid}.md"
            if src.is_symlink() or not src.is_file():
                raise HeartbeatError("that version is gone", 404, "unknown")
            raw = src.read_bytes()
            if len(raw) > INITIATIVE_MAX:
                raise HeartbeatError("that version is too big", 413, "too_big")
            text = raw.decode("utf-8", errors="replace")
            saved = self._backup()        # restoring is undoable too
            self._write(text)
            return saved
