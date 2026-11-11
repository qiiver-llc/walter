"""Beta feedback from the companion app (Settings > Feedback).

POST /api/app/feedback                  {type: bug|idea|praise, text (<= 2000), diagnostics?: bool,
                                         image?: upload id from /api/app/upload}  -> saved locally
GET  /api/app/feedback                  "Your feedback": newest first, with status
GET  /api/app/feedback/diagnostics      the exact diagnostics bundle that would be attached (preview)
GET  /api/app/feedback/{id}             one item, including its diagnostics bundle (what would be sent)
POST /api/app/feedback/{id}/send        "Send to the Walter team": POST to [feedback] endpoint;
                                         with no endpoint set -> 409 + offer the download instead
GET  /api/app/feedback/{id}/download    the same payload as a .json file for the owner to email

Stored as ~/.walter/feedback/YYYY-MM-DD-HHMMSS.json (mode 600, folder 700), photo next to it.
Nothing ever leaves the box unless the owner taps Send (no background sending, no retries).
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from aiohttp import web

log = logging.getLogger("walter.feedback")

TYPES = ("bug", "idea", "praise")
MAX_TEXT = 2000
MAX_PER_DAY = 30
MAX_PHOTO_SEND = 700 * 1024       # photos bigger than this stay local (download instead)
SEND_TIMEOUT = 15
ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{6}(?:-\d{1,2})?$")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class FeedbackError(ValueError):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def _write_600(path: Path, data: bytes):
    tmp = path.with_name("." + path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class FeedbackStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.Lock()

    def _dir(self) -> Path:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        return self.root

    def path(self, fid) -> Path | None:
        if not isinstance(fid, str) or not ID_RE.match(fid):
            return None
        p = self.root / f"{fid}.json"
        return p if p.is_file() else None

    def get(self, fid) -> dict | None:
        p = self.path(fid)
        if not p:
            return None
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return None

    def _save(self, rec: dict):
        _write_600(self._dir() / f"{rec['id']}.json", json.dumps(rec, indent=1).encode())

    def add(self, kind, text, *, device="", diagnostics: dict | None = None, photo: Path | None = None,
            now: float | None = None) -> dict:
        if kind not in TYPES:
            raise FeedbackError("Pick bug, idea or praise.")
        if not isinstance(text, str):
            raise FeedbackError("Write a few words first.")
        text = CTRL_RE.sub("", text).strip()
        if not text:
            raise FeedbackError("Write a few words first.")
        if len(text) > MAX_TEXT:
            raise FeedbackError(f"That's a bit long (max {MAX_TEXT} characters).", 413)
        now = time.time() if now is None else now
        with self._lock:
            d = self._dir()
            day = dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
            if len(list(d.glob(f"{day}-*.json"))) >= MAX_PER_DAY:
                raise FeedbackError("That's plenty of feedback for today, thank you! Try again tomorrow.", 429)
            base = dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d-%H%M%S")
            fid, n = base, 1
            while (d / f"{fid}.json").exists():
                fid, n = f"{base}-{n}", n + 1
            rec = {"id": fid, "type": kind, "text": text, "created": int(now), "device": str(device)[:60],
                   "status": "saved", "status_at": int(now), "photo": None, "diagnostics": diagnostics}
            if photo is not None:
                dest = d / f"{fid}{photo.suffix.lower()}"
                shutil.copyfile(photo, dest)
                os.chmod(dest, 0o600)
                rec["photo"] = dest.name
            self._save(rec)
        return rec

    def set_status(self, fid, status: str, error: str = "") -> dict | None:
        with self._lock:
            rec = self.get(fid)
            if rec is None:
                return None
            rec["status"], rec["status_at"] = status, int(time.time())
            rec["error"] = error[:200] if error else None
            self._save(rec)
        return rec

    def list(self, limit: int = 50) -> list[dict]:
        if not self.root.is_dir():
            return []
        out = []
        for p in sorted(self.root.glob("*.json"), reverse=True)[:limit]:
            try:
                r = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            out.append(public(r))
        return out

    def payload(self, fid) -> dict | None:
        """Exactly what Send posts (and Download saves)."""
        rec = self.get(fid)
        if rec is None:
            return None
        out = {"kind": "walter-feedback", "schema": 1, "id": rec["id"], "type": rec["type"], "text": rec["text"],
               "created": rec["created"], "diagnostics": rec.get("diagnostics"), "photo": None}
        ph = rec.get("photo")
        if ph and (self.root / ph).is_file():
            data = (self.root / ph).read_bytes()
            if len(data) <= MAX_PHOTO_SEND:
                out["photo"] = {"name": ph, "base64": base64.b64encode(data).decode()}
            else:
                out["photo"] = {"name": ph, "omitted": "too big to send; attach it by email if needed"}
        return out


def public(rec: dict) -> dict:
    """List view: no diagnostics body."""
    return {k: rec.get(k) for k in ("id", "type", "text", "created", "status", "status_at", "error", "photo")} | {
        "has_diagnostics": bool(rec.get("diagnostics"))}


def post_json(url: str, body: dict, timeout: float = SEND_TIMEOUT) -> tuple[bool, str]:
    if not re.match(r"^https://[^\s/]+(/\S*)?$", url or "") and not url.startswith("http://127.0.0.1"):
        return False, "the feedback address must be https"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Walter-feedback/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:   # noqa: S310 (owner-configured https URL)
            return 200 <= r.status < 300, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, str(getattr(e, "reason", e))[:120]


def add_routes(routes: web.RouteTableDef, apphub, json_body, cfg, store: FeedbackStore | None = None,
               build_diag=None, poster=None):
    from . import devices as D
    store = store or FeedbackStore(D.data_dir() / "feedback")
    apphub.feedback = store

    def _diag() -> dict:
        if build_diag:
            return build_diag()
        from . import diagnostics
        return diagnostics.build()

    def endpoint() -> str:
        return (getattr(getattr(cfg, "feedback", None), "endpoint", "") or "").strip()

    @routes.get("/api/app/feedback")
    async def fb_list(request):
        apphub.auth(request)
        items = await asyncio.get_running_loop().run_in_executor(None, store.list)
        return web.json_response({"items": items, "can_send": bool(endpoint()), "types": list(TYPES),
                                  "max_text": MAX_TEXT})

    @routes.get("/api/app/feedback/diagnostics")
    async def fb_diag_preview(request):
        apphub.auth(request)
        from . import diagnostics
        b = await asyncio.get_running_loop().run_in_executor(None, _diag)
        return web.json_response({"summary": diagnostics.summary(b), "bundle": b},
                                 headers={"Cache-Control": "no-store"})

    @routes.post("/api/app/feedback")
    async def fb_add(request):
        dev = apphub.auth(request)
        d = await json_body(request, 16384)
        photo = None
        if d.get("image"):
            ps = apphub.image_paths([d.get("image")]) if hasattr(apphub, "image_paths") else []
            if not ps:
                return web.json_response({"ok": False, "error": "That photo has gone; attach it again."},
                                         status=400)
            photo = Path(ps[0])
        loop = asyncio.get_running_loop()
        diag = await loop.run_in_executor(None, _diag) if d.get("diagnostics") is True else None
        try:
            rec = await loop.run_in_executor(None, lambda: store.add(
                d.get("type"), d.get("text"), device=dev.get("name", ""), diagnostics=diag, photo=photo))
        except FeedbackError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=e.status)
        log.info("feedback %s saved (%s, diagnostics=%s, photo=%s)", rec["id"], rec["type"],
                 bool(diag), bool(rec["photo"]))
        return web.json_response({"ok": True, "item": public(rec), "can_send": bool(endpoint())})

    @routes.get("/api/app/feedback/{fid}")
    async def fb_get(request):
        apphub.auth(request)
        p = store.payload(request.match_info["fid"])
        if p is None:
            raise web.HTTPNotFound(text="no such feedback")
        rec = store.get(request.match_info["fid"])
        return web.json_response({"item": public(rec), "payload": p}, headers={"Cache-Control": "no-store"})

    @routes.get("/api/app/feedback/{fid}/download")
    async def fb_download(request):
        apphub.auth(request)
        fid = request.match_info["fid"]
        p = store.payload(fid)
        if p is None:
            raise web.HTTPNotFound(text="no such feedback")
        return web.Response(body=json.dumps(p, indent=1).encode(), content_type="application/json", headers={
            "Content-Disposition": f'attachment; filename="walter-feedback-{fid}.json"',
            "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})

    @routes.post("/api/app/feedback/{fid}/send")
    async def fb_send(request):
        dev = apphub.auth(request)
        fid = request.match_info["fid"]
        p = store.payload(fid)
        if p is None:
            raise web.HTTPNotFound(text="no such feedback")
        url = endpoint()
        if not url:
            return web.json_response({"ok": False, "error": "No feedback address is set up, so download the file "
                                      "and email it instead.", "download": f"/api/app/feedback/{fid}/download"},
                                     status=409)
        ok, why = await asyncio.get_running_loop().run_in_executor(None, poster or post_json, url, p)
        rec = store.set_status(fid, "sent" if ok else "send failed", "" if ok else why)
        log.info("feedback %s send by %s: %s", fid, dev.get("name"), "ok" if ok else why)
        return web.json_response({"ok": ok, "item": public(rec), **({} if ok else {"error": f"Couldn't send ({why})."})},
                                 status=200 if ok else 502)

    return store
