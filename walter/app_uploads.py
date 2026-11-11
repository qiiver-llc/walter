"""Images sent from the companion app's chat (so Walter can see them).

Stored under ~/.walter/app-uploads/YYYY-MM-DD/<random>.<ext>, mode 600, never
executable, deleted after 7 days. Only JPEG, PNG, WebP and GIF, checked by their
magic bytes (the browser's content type is not trusted). Served back only to
paired devices (app_server.py checks the token before calling `path`).
"""
from __future__ import annotations

import datetime as dt
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

MAX_BYTES = 8 * 1024 * 1024        # per image
MAX_PER_MESSAGE = 4
MAX_PER_DAY = 200                  # images
MAX_BYTES_PER_DAY = 300 * 1024 * 1024
KEEP_DAYS = 7

ID_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_([A-Za-z0-9_-]{16,64})\.(jpg|png|webp|gif)$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MIME = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}


class UploadError(ValueError):
    """Rejected upload; str(e) is safe to show the person."""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def sniff(data: bytes) -> str | None:
    """The file extension for these bytes if they are an allowed image, else None."""
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


class UploadStore:
    def __init__(self, root: Path, *, max_bytes: int = MAX_BYTES, per_day: int = MAX_PER_DAY,
                 bytes_per_day: int = MAX_BYTES_PER_DAY, keep_days: int = KEEP_DAYS):
        self.root = Path(root)
        self.max_bytes, self.per_day, self.bytes_per_day = max_bytes, per_day, bytes_per_day
        self.keep_days = keep_days
        self._lock = threading.Lock()

    # ------------------------------------------------------------ write
    def _day_dir(self, day: str) -> Path:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        d = self.root / day
        d.mkdir(exist_ok=True, mode=0o700)
        return d

    def usage(self, day: str | None = None) -> tuple[int, int]:
        """(images, bytes) stored today."""
        d = self.root / (day or dt.date.today().isoformat())
        if not d.is_dir():
            return 0, 0
        files = [p for p in d.iterdir() if p.is_file()]
        return len(files), sum(p.stat().st_size for p in files)

    def save(self, data: bytes) -> dict:
        """Validate and store one image -> {"id", "type", "bytes"}; raises UploadError."""
        if not data:
            raise UploadError("That image was empty.")
        if len(data) > self.max_bytes:
            raise UploadError(f"That image is too big (max {self.max_bytes // (1024 * 1024)} MB).", 413)
        ext = sniff(data)
        if ext is None:
            raise UploadError("Only JPEG, PNG, WebP or GIF images.", 415)
        day = dt.date.today().isoformat()
        with self._lock:
            n, size = self.usage(day)
            if n + 1 > self.per_day or size + len(data) > self.bytes_per_day:
                raise UploadError("That's enough images for today; try again tomorrow.", 429)
            d = self._day_dir(day)
            name = f"{secrets.token_urlsafe(18)}.{ext}"
            p = d / name
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.chmod(p, 0o600)
        return {"id": f"{day}_{name}", "type": MIME[ext], "bytes": len(data)}

    # ------------------------------------------------------------ read
    def path(self, image_id: str) -> Path | None:
        """The stored file for an id, or None (bad id, traversal, gone)."""
        m = ID_RE.match(str(image_id or ""))
        if not m:
            return None
        day, name, ext = m.groups()
        p = self.root / day / f"{name}.{ext}"
        try:
            if p.is_symlink() or not p.is_file():
                return None
            if p.resolve().parent != (self.root / day).resolve():
                return None
        except OSError:
            return None
        return p

    @staticmethod
    def mime(p: Path) -> str:
        return MIME.get(p.suffix.lstrip(".").lower(), "application/octet-stream")

    # ------------------------------------------------------------ cleanup
    def cleanup(self, now: float | None = None) -> int:
        """Delete day folders older than keep_days. Returns how many files went."""
        if not self.root.is_dir():
            return 0
        today = dt.date.fromtimestamp(now if now is not None else time.time())
        gone = 0
        for d in self.root.iterdir():
            if not d.is_dir() or d.is_symlink() or not DAY_RE.match(d.name):
                continue
            try:
                day = dt.date.fromisoformat(d.name)
            except ValueError:
                continue
            if (today - day).days >= self.keep_days:
                gone += sum(1 for p in d.iterdir() if p.is_file())
                shutil.rmtree(d, ignore_errors=True)
        return gone
