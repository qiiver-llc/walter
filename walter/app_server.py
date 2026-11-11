"""Companion app backend: the PWA at /app, /api/app/* and the app websocket /app/ws.

Security (devices.py): every /api/app/* call and the websocket need a paired
device's token (the kiosk on loopback is exempt; a request through a reverse
proxy never counts as the kiosk). Wrong tokens and codes are rate-limited.

Chat and talk: messages enter the conductor as source "app" with a per-turn
reply route, so the answer goes back to the app (text, mood, and the TTS audio
Walter synthesises anyway) instead of the wall speaker, unless they ask for it
"on the wall".

Wire format, server -> app: JSON text frames, plus binary audio frames
b"WA" + 4-byte big-endian header length + JSON header + WAV bytes.
App -> server: JSON text frames, plus binary b"\\x01" + int16 LE mono PCM at
16 kHz while push-to-talk is held.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import io
import json
import logging
import os
import re
import struct
import time
import wave
from pathlib import Path

from aiohttp import WSMsgType, web

from . import dashboard as dash
from . import devices as D
from .devices import bump_version, read_version  # noqa: F401  (walterctl uses them too)
from .config import REPO_DIR, Config
from .tags import TAG_RE

log = logging.getLogger("walter.app")

APP_DIR = D.APP_DIR
KIOSK = {"id": "kiosk", "name": "this computer", "owner": ""}
FORWARD = {"state", "mute", "jobs", "cloud", "emotion", "sleeping"}
REPLAY = ("state", "mute", "jobs", "cloud")
MAX_CHAT = 4000
MAX_BODY = 8192
PCM_SR = 16000
# "say it on the wall", "tell them on the wall speaker", "out loud at home"
WALL_RE = re.compile(r"\b(?:on|through|over|from|via)\s+the\s+wall(?:\s+speaker)?\b|\bout\s+loud\s+at\s+home\b"
                     r"|\bsay\s+it\s+(?:out\s+loud|at\s+home)\b", re.I)
SHELL = ("./", "index.html", "app.js", "app.css", "face-mini.js", "pcm-worklet.js", "remote.js", "player.js",
         "modes.js", "schedule.js",
         "manifest.webmanifest", "icons/icon-192.png", "icons/icon-512.png", "icons/icon.svg")


# ------------------------------------------------------------------ helpers
def app_build(app_dir: Path | None = None) -> str:
    """Short hash of the app's files: changes whenever Walter edits any of them,
    so installed copies update even if he forgets to bump VERSION."""
    root = app_dir or APP_DIR
    h = hashlib.sha1()
    try:
        files = sorted(p for p in root.rglob("*") if p.is_file() and p.name != "README.md")
    except OSError:
        files = []
    for p in files:
        try:
            st = p.stat()
            h.update(f"{p.relative_to(root).as_posix()}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
        except OSError:
            continue
    return h.hexdigest()[:10]


def wav_bytes(audio, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(audio.astype("<i2").tobytes())
    return buf.getvalue()


def pack(header: dict, payload: bytes) -> bytes:
    h = json.dumps(header).encode()
    return b"WA" + struct.pack(">I", len(h)) + h + payload


def parse_backlog(text: str, limit: int = 100) -> list[dict]:
    """'- [status] **Title**: details' lines of backlog.md."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^\s*[-*]\s*\[([^\]]+)\]\s*(?:\*\*(.+?)\*\*)?\s*:?\s*(.*)$", line)
        if not m:
            continue
        out.append({"status": m[1].strip(), "title": (m[2] or "").strip(), "details": m[3].strip()[:400]})
        if len(out) >= limit:
            break
    return out


def bearer(request) -> str:
    auth = request.headers.get("Authorization", "")
    return auth[7:].strip() if auth[:7].lower() == "bearer " else ""


# ------------------------------------------------------------------ clients
class AppClient:
    def __init__(self, ws: web.WebSocketResponse, device: dict, ip: str):
        self.ws, self.device, self.ip = ws, device, ip
        self.talk: bytearray | None = None
        self.talk_t0 = 0.0

    def send(self, msg: dict):
        if not self.ws.closed:
            asyncio.ensure_future(self._send(self.ws.send_str, json.dumps(msg)))

    def send_bytes(self, data: bytes):
        if not self.ws.closed:
            asyncio.ensure_future(self._send(self.ws.send_bytes, data))

    @staticmethod
    async def _send(fn, data):
        try:
            await fn(data)
        except Exception:
            pass


class AppReply:
    """One brain turn answered to the app: streamed sentences, a mood, optional
    audio (sentence by sentence, in order) and the final text with formatting."""

    def __init__(self, hub: "AppHub", meta: dict):
        self.hub = hub
        self.device_id = meta.get("device_id", "")
        self.turn = meta.get("app_turn", 0)
        self.route = meta.get("route", "app")
        self.audio = bool(meta.get("audio"))
        self.cancelled = False
        self.raw_text = ""
        self.seq = 0
        self._q: asyncio.Queue | None = None
        self._worker: asyncio.Task | None = None
        self.send({"t": "typing", "turn": self.turn, "on": True})

    def send(self, msg: dict):
        self.hub.to_device(self.device_id, msg)

    def say(self, text: str):
        if self.cancelled:
            return
        self.seq += 1
        self.send({"t": "say", "turn": self.turn, "seq": self.seq, "text": text})
        if self.audio:
            if self._q is None:
                self._q = asyncio.Queue()
                self._worker = asyncio.ensure_future(self._audio_worker())
            self._q.put_nowait((self.seq, text))

    def mood(self, name: str):
        self.send({"t": "mood", "turn": self.turn, "name": name})

    def state(self, st: str):
        self.send({"t": "state", "state": st, "turn": self.turn})

    def raw(self, chunk: str):
        self.raw_text += chunk

    def reset(self):
        self.raw_text = ""

    def finish(self, silent: bool = False):
        text = re.sub(r"[ \t]+\n", "\n", TAG_RE.sub("", self.raw_text)).strip()
        text = re.sub(r"[ \t]{2,}", " ", text)
        self.send({"t": "reply", "turn": self.turn, "text": "" if (silent or self.cancelled) else text,
                   "silent": silent, "cancelled": self.cancelled})
        self.send({"t": "typing", "turn": self.turn, "on": False})
        if text and not (silent or self.cancelled) and self.route != "both":
            # app-only reply: into the wall's chat thread too (face/chat.js); "both" is spoken
            # on the wall, so its captions already fill the thread
            self.hub.to_face({"t": "chat", "who": "walter", "text": text[:4000], "from": "phone"})
        if self._q is not None:
            self._q.put_nowait(None)
        if self.hub.current is self:
            self.hub.current = None

    async def _audio_worker(self):
        while True:
            item = await self._q.get()
            if item is None or self.cancelled:
                break
            seq, text = item
            try:
                got = await self.hub.synth(text)
            except Exception as e:
                log.warning("app tts failed: %s", e)
                got = None
            if self.cancelled:
                break
            if got is None:
                self.send({"t": "noaudio", "turn": self.turn, "seq": seq, "text": text})
                continue
            wav, cloud = got
            hdr = {"t": "audio", "turn": self.turn, "seq": seq, "text": text, "cloud": cloud}
            for c in self.hub.device_clients(self.device_id):
                c.send_bytes(pack(hdr, wav))


class AppHub:
    def __init__(self, cfg: Config, conductor, store: D.DeviceStore | None = None,
                 app_dir: Path | None = None):
        self.cfg = cfg
        self.conductor = conductor
        self.store = store or D.DeviceStore()
        self.app_dir = app_dir or APP_DIR
        self.clients: set[AppClient] = set()
        self.limiter = D.RateLimiter(max_fails=8, window_s=600, block_s=900, global_max=60)
        self.pair_limiter = D.RateLimiter(max_fails=5, window_s=600, block_s=900, global_max=20)
        self.version = read_version(self.app_dir)
        self.build = app_build(self.app_dir)
        self.last: dict[str, dict] = {}
        self.turns = 0
        self.started = round(time.time(), 3)   # this process's start: the app spots a restart by it
        self.current: AppReply | None = None
        # Web Push (webpush.py): reaches paired phones with the app closed; https only.
        from .webpush import WebPush
        pub = (cfg.app.public_url or "").strip().rstrip("/")
        self.push = WebPush(subject=pub if pub.startswith("https://") else "")
        self.away = dash.AwayFeed()
        # Chat images (app_uploads.py): ~/.walter/app-uploads, mode 600, 7 days.
        from .app_uploads import UploadStore
        self.uploads = UploadStore(D.data_dir() / "app-uploads")
        self._loop = None
        dash.on_away(self.on_away)

    # ------------------------------------------------------------ sending
    def device_clients(self, device_id: str) -> list[AppClient]:
        return [c for c in self.clients if c.device["id"] == device_id]

    def to_device(self, device_id: str, msg: dict):
        for c in self.device_clients(device_id):
            c.send(msg)

    def broadcast(self, msg: dict):
        for c in list(self.clients):
            c.send(msg)

    def to_face(self, msg: dict):
        """A message for the wall face (the conductor's face broadcast), e.g. app turns for the
        conversation chat thread (face/chat.js). Never fails a turn."""
        send = getattr(self.conductor, "send", None)
        if not callable(send):
            return
        try:
            send(msg)
        except Exception:
            log.exception("app: face message failed")

    def from_face(self, msg: dict):
        """Hub.send hook: pass Walter's state, mute, workers and cloud mark on."""
        t = msg.get("t")
        if t not in FORWARD:
            return
        if t in REPLAY:
            self.last[t] = msg
        if self.clients:
            self.broadcast(msg)

    def notify(self, kind: str, text: str, title: str = "", push: bool = False):
        """Toast in every open app (reminders, finished tasks); push=True also sends a
        Web Push notification to paired phones (works with the app closed)."""
        self.broadcast({"t": "notify", "kind": kind, "title": title, "text": str(text)[:600], "time": time.time()})
        if push:
            try:
                self.push.send(title or ("Reminder" if kind == "reminder" else "Walter"), str(text), tag=kind)
            except Exception:
                log.exception("web push failed")

    def on_away(self, item: dict):
        """dash.add_away hook: update the apps' badge; push it to phones if asked."""
        def go():
            self.broadcast({"t": "away", "unseen": len(self.away.unseen()),
                            "item": {k: item.get(k) for k in ("id", "t", "title", "body", "kind")}})
        try:
            loop = self._loop or asyncio.get_running_loop()
            self._loop = loop
            loop.call_soon_threadsafe(go)
        except RuntimeError:
            pass   # no loop (a test or the CLI): nothing open to tell
        if item.get("push"):
            try:
                self.push.send(item.get("title") or "Walter", item.get("body") or "", tag="away")
            except Exception:
                log.exception("web push failed")

    def away_changed(self):
        self.broadcast({"t": "away", "unseen": len(self.away.unseen())})

    def check_version(self, force: bool = False) -> bool:
        v, b = read_version(self.app_dir), app_build(self.app_dir)
        if force or v != self.version or b != self.build:
            self.version, self.build = v, b
            log.info("companion app changed: version %s (build %s)", v, b)
            self.broadcast({"t": "app_version", "version": v, "build": b})
            return True
        return False

    def kick_revoked(self):
        ids = self.store.ids()
        for c in list(self.clients):
            if c.device["id"] != "kiosk" and c.device["id"] not in ids:
                log.info("app: closing revoked device %s", c.device["name"])
                c.send({"t": "revoked"})
                asyncio.ensure_future(c.ws.close(code=4401, message=b"revoked"))
                self.clients.discard(c)
        try:
            self.push.prune(ids)
        except Exception:
            log.exception("web push prune failed")

    # ------------------------------------------------------------ turns
    def reply(self, meta: dict) -> AppReply:
        """Called by the conductor when an app-routed turn starts."""
        r = AppReply(self, meta)
        self.current = r
        return r

    def submit(self, client: AppClient, text: str, *, wall: bool = False, audio: bool = False,
               voice: bool = False, cid=None, images: list[str] | None = None) -> int:
        self.turns += 1
        dev = client.device
        wall = wall or bool(WALL_RE.search(text))
        meta = {"route": "both" if wall else "app", "device": dev["name"], "device_id": dev["id"],
                "owner": dev.get("owner", ""), "audio": bool(audio), "app_turn": self.turns,
                "app_voice": bool(voice)}
        if voice:
            meta["speaker"] = f"speaker: {dev.get('owner') or dev['name']} (app)"
        if images:
            meta["images"] = list(images)
        client.send({"t": "queued", "turn": self.turns, "id": cid, "wall": wall})
        # the same conversation thread on the wall (face/chat.js), marked "from phone"
        self.to_face({"t": "chat", "who": "you", "text": str(text)[:2000], "from": "phone"})
        modes = getattr(self, "modes", None)   # typed "dev mode" / "exit dev mode" (T47)
        if modes is not None and not images:
            modes.typed(text)
        self.conductor.submit("app", text, meta)
        return self.turns

    def stop(self, device_id: str):
        r = self.current
        if r is not None and r.device_id == device_id and not r.cancelled:
            r.cancelled = True
            try:
                asyncio.ensure_future(self.conductor.brain.interrupt())
            except Exception:
                pass

    async def synth(self, text: str):
        """(wav bytes, cloud?) of one sentence in Walter's current voice, or None."""
        voice = getattr(self.conductor, "voice", None)
        if voice is None:
            return None
        loop = asyncio.get_running_loop()
        audio, sr, _words, _s, cloud = await loop.run_in_executor(voice._synth_pool, voice._timed_synth, text)
        if voice.synth.kind == "mime" and not cloud:
            return None   # no real voice installed: the app uses the browser's
        return wav_bytes(audio, sr), bool(cloud)

    async def transcribe(self, pcm: bytes) -> tuple[str, str]:
        """(text, "openai"|"local"|"none") using the same ears setting as the wall."""
        loop = asyncio.get_running_loop()
        sp = getattr(self.conductor, "speech", None)
        if sp is not None and sp.stt == "openai" and sp.has_key():
            from .ears import hint_prompt
            text = await loop.run_in_executor(None, sp.transcribe, pcm, hint_prompt(self.cfg.ears.hotwords))
            if text is not None:
                return text.strip(), "openai"
        ears = getattr(self.conductor, "ears", None)
        if ears is None or not hasattr(ears, "transcribe_pcm"):
            return "", "none"
        text = await loop.run_in_executor(None, ears.transcribe_pcm, pcm)
        return (text or "").strip(), "local"

    # ------------------------------------------------------------ auth
    def auth(self, request) -> dict:
        """The device making this request, or raise 401/429."""
        if D.is_kiosk(request.remote, request.headers) and not bearer(request):
            return KIOSK
        ip = D.client_ip(request.remote, request.headers)
        wait = self.limiter.retry_after(ip)
        if wait:
            raise web.HTTPTooManyRequests(text="too many wrong tries; wait a bit",
                                          headers={"Retry-After": str(int(wait) + 1)})
        dev = self.store.check(bearer(request))
        if dev is None:
            self.limiter.fail(ip)
            raise web.HTTPUnauthorized(text="pair this device first",
                                       headers={"WWW-Authenticate": "Bearer"})
        self.limiter.ok(ip)
        self.store.touch(dev["id"], ip)
        return dev

    def status(self) -> dict:
        c = self.conductor
        sp = getattr(c, "speech", None)
        return {"state": getattr(c, "state", "idle"), "sleeping": bool(getattr(c, "sleeping", False)),
                "muted": bool(getattr(c, "user_muted", False)), "busy": bool(getattr(c, "busy", False)),
                "version": self.version, "build": self.build,
                "cloud": sp.indicator() if sp is not None else None,
                "speech": {"tts": sp.tts, "stt": sp.stt} if sp is not None else None,
                "away_unseen": len(self.away.unseen()),
                "features": ["images"]}   # the app enables its attach button by this

    def image_paths(self, ids) -> list[str]:
        """Stored paths for the ids a chat message names (unknown/bad ids dropped)."""
        from .app_uploads import MAX_PER_MESSAGE
        out = []
        for i in (ids if isinstance(ids, list) else [])[:MAX_PER_MESSAGE]:
            p = self.uploads.path(i) if isinstance(i, str) else None
            if p is not None and str(p) not in out:
                out.append(str(p))
        return out


# ------------------------------------------------------------------ routes
def app_paths(path: str) -> bool:
    return path == "/app" or path.startswith("/app/") or path.startswith("/api/app/")


def add_routes(app: web.Application, cfg: Config, hub, apphub: AppHub, conductor, reminders, tasks):
    routes = web.RouteTableDef()
    app_dir = apphub.app_dir

    async def json_body(request, limit: int = MAX_BODY) -> dict:
        if (request.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            raise web.HTTPUnsupportedMediaType(text="JSON only")
        raw = b""
        while len(raw) <= limit:           # read() returns one chunk at a time: big bodies arrive in pieces
            part = await request.content.read(limit + 1 - len(raw))
            if not part:
                break
            raw += part
        if len(raw) > limit:
            raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(raw))
        try:
            d = json.loads(raw or b"{}")
        except ValueError:
            raise web.HTTPBadRequest(text="send a JSON object")
        if not isinstance(d, dict):
            raise web.HTTPBadRequest(text="send a JSON object")
        return d

    @routes.get("/app")
    async def app_root(_):
        raise web.HTTPFound("/app/")

    @routes.get("/app/")
    async def app_index(_):
        try:
            html = (app_dir / "index.html").read_text()
        except OSError:
            raise web.HTTPNotFound(text="the companion app isn't installed")
        return web.Response(text=html.replace("__VERSION__", apphub.version), content_type="text/html")

    @routes.get("/app/sw.js")
    async def app_sw(_):
        try:
            js = (app_dir / "sw.js").read_text()
        except OSError:
            raise web.HTTPNotFound()
        js = js.replace("__BUILD__", f"{apphub.version}-{apphub.build}").replace(
            "__SHELL__", json.dumps(list(SHELL)))
        return web.Response(text=js, content_type="application/javascript",
                            headers={"Service-Worker-Allowed": "/app/"})

    @routes.get("/api/app/version")
    async def app_version(_):
        return web.json_response({"version": apphub.version, "build": apphub.build,
                                  "started": apphub.started})   # changes on every restart

    # ---- pairing
    @routes.post("/api/app/pair")
    async def app_pair(request):
        ip = D.client_ip(request.remote, request.headers)
        wait = apphub.pair_limiter.retry_after(ip)
        if wait:
            return web.json_response({"error": "too many tries; wait a few minutes"}, status=429,
                                     headers={"Retry-After": str(int(wait) + 1)})
        d = await json_body(request, 1024)
        try:
            dev, token = apphub.store.pair(str(d.get("code", "")), str(d.get("name", "")),
                                           str(d.get("owner", "")))
        except PermissionError as e:
            apphub.pair_limiter.fail(ip)
            return web.json_response({"error": str(e)}, status=403)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        apphub.pair_limiter.ok(ip)
        log.info("app: paired a new device %r from %s", dev["name"], ip)
        hub.send({"t": "emotion", "name": "happy", "hold": 6})
        hub.send({"t": "action", "name": "bounce"})
        asyncio.ensure_future(_run(REPO_DIR / "bin" / "walter-show", "--clear"))
        return web.json_response({"token": token, "device": dev, "version": apphub.version})

    @routes.get("/api/app/me")
    async def app_me(request):
        dev = apphub.auth(request)
        return web.json_response({"device": dev, **apphub.status()})

    @routes.post("/api/app/unpair")
    async def app_unpair(request):
        dev = apphub.auth(request)
        if dev["id"] == "kiosk":
            return web.json_response({"ok": False, "error": "this computer isn't paired"})
        apphub.store.revoke(dev["id"])
        return web.json_response({"ok": True})

    @routes.get("/api/app/status")
    async def app_status(request):
        apphub.auth(request)
        return web.json_response(apphub.status())

    # ---- panels
    store = dash.ListStore(title=cfg.dashboard.list_title)

    def list_payload() -> dict:
        return {"title": store.title, "sections": list(dash.SECTIONS), "items": store.items()}

    @routes.get("/api/app/list")
    async def app_list(request):
        apphub.auth(request)
        return web.json_response(list_payload())

    @routes.post("/api/app/list")
    async def app_list_change(request):
        apphub.auth(request)
        from .server import dash_apply, dash_command
        d = await json_body(request, 4096)
        cmd = dash_command(conductor, d)
        if cmd:
            return web.json_response({"ok": True, "command": cmd, **list_payload()})
        try:
            item = dash_apply(store, d)
        except (ValueError, KeyError, TypeError) as e:
            return web.json_response({"error": str(e.args[0] if e.args else e)}, status=400)
        return web.json_response({"ok": True, "item": item, **list_payload()})

    @routes.get("/api/app/events")
    async def app_events(request):
        apphub.auth(request)
        try:
            d = json.loads((dash.FACE_DIR / "dashboard-events.json").read_text())
        except (OSError, ValueError):
            d = {}
        return web.json_response({"updated": d.get("updated"), "events": d.get("events") or []})

    @routes.get("/api/app/backlog")
    async def app_backlog(request):
        apphub.auth(request)
        try:
            text = (Path(cfg.brain.home) / "backlog.md").read_text()
        except OSError:
            text = ""
        return web.json_response({"items": parse_backlog(text)})

    @routes.get("/api/app/tasks")
    async def app_tasks(request):
        apphub.auth(request)
        items = list(getattr(tasks, "items", None) or [])[-30:]
        out = [{"id": i.get("id"), "title": i.get("title"), "status": i.get("status"),
                "started": i.get("started"), "finished": i.get("finished"),
                "result": (i.get("result") or "")[:800]} for i in reversed(items)]
        rem = [{"id": r.get("id"), "due": r.get("due"), "text": r.get("text"), "kind": r.get("kind"),
                "repeat": r.get("repeat")} for r in (getattr(reminders, "items", None) or [])][:50]
        return web.json_response({"tasks": out, "reminders": rem})

    @routes.post("/api/app/control")
    async def app_control(request):
        """{action: mute|unmute|sleep|wake|stop|volume, step?: up|down, set?: 0-100}"""
        apphub.auth(request)
        d = await json_body(request, 512)
        a = d.get("action")
        if a in ("mute", "unmute"):
            conductor.set_user_mute(a == "mute")
        elif a == "sleep":
            conductor.go_to_sleep()
        elif a == "wake":
            if conductor.user_muted:
                conductor.set_user_mute(False)   # unmuting wakes him too
            conductor.wake_up()
        elif a == "stop":
            conductor.stop()
        elif a == "volume":
            args = (["set", str(max(0, min(100, int(d["set"]))))] if isinstance(d.get("set"), int)
                    else [d["step"]] if d.get("step") in ("up", "down", "mute", "unmute") else ["get"])
            rc, out = await _run(REPO_DIR / "bin" / "walter-volume", *args)
            return web.json_response({"ok": rc == 0, "volume": out.strip(), **apphub.status()})
        else:
            return web.json_response({"error": "unknown action"}, status=400)
        if a in ("wake", "unmute") and hasattr(conductor, "welcome_back"):
            conductor.welcome_back("woken from the app")   # what they missed, as a card
        return web.json_response({"ok": True, **apphub.status()})

    @routes.post("/api/app/show")
    async def app_show(request):
        """Put a card on the wall: {title, body, seconds?}"""
        dev = apphub.auth(request)
        d = await json_body(request, 4096)
        title = dash.clean_text(d.get("title") or f"From {dev['name']}", 80)
        body = str(d.get("body") or "").replace("\r", "")[:1500]
        try:
            secs = max(5, min(3600, int(d.get("seconds") or 120)))
        except (TypeError, ValueError):
            secs = 120
        if not body.strip():
            return web.json_response({"error": "nothing to show"}, status=400)
        rc, _ = await _run(REPO_DIR / "bin" / "walter-show", title, body, str(secs))
        return web.json_response({"ok": rc == 0})

    # ---- workers at once ([brain] max_tasks, live via live.py; same path as
    # `walterctl config set brain.max_tasks N`, which keeps config.toml's comments)
    from .live import LIMITS
    w_min, w_max = LIMITS[("brain", "max_tasks")]

    def _workers_state() -> dict:
        queued = sum(1 for r in (getattr(reminders, "items", None) or [])
                     if str(r.get("text") or "").startswith("QUEUED ["))   # skills/queue-job.sh
        try:
            load = round(os.getloadavg()[0], 2)
        except (OSError, AttributeError):
            load = None
        return {"max_tasks": cfg.brain.max_tasks, "min": w_min, "max": w_max,
                "running": int(getattr(tasks, "running", 0) or 0), "queued": queued,
                "threads": os.cpu_count() or 1, "load": load}

    @routes.get("/api/app/workers")
    async def app_workers(request):
        apphub.auth(request)
        return web.json_response(_workers_state())

    # ---- live worker status (walter/workerlive.py: last step from each worker's transcript)
    from .workerlive import Snapshotter
    _live = Snapshotter(cfg.data_dir, cfg.brain.home)

    @routes.get("/api/app/workers/live")
    async def app_workers_live(request):
        apphub.auth(request)
        snap = await asyncio.get_running_loop().run_in_executor(None, _live.snapshot)
        return web.json_response(snap)

    @routes.post("/api/app/workers")
    async def app_workers_set(request):
        """{max_tasks: 1..8}: written to config.toml and applied live."""
        dev = apphub.auth(request)
        d = await json_body(request, 256)
        n = d.get("max_tasks")
        if isinstance(n, bool) or not isinstance(n, int) or not (w_min <= n <= w_max):
            return web.json_response({"error": f"max_tasks must be a whole number {w_min} to {w_max}",
                                      **_workers_state()}, status=400)
        lv = getattr(conductor, "live", None)
        if lv is None:
            return web.json_response({"error": "live config isn't ready", **_workers_state()}, status=503)
        old = cfg.brain.max_tasks
        try:
            r = await asyncio.get_running_loop().run_in_executor(None, lv.set, "brain.max_tasks", n)
        except (ValueError, OSError) as e:
            log.warning("app: couldn't set brain.max_tasks: %s", e)
            return web.json_response({"error": str(e), **_workers_state()}, status=400)
        if not r.get("ok", True):
            return web.json_response({"error": r.get("error") or "not applied", **_workers_state()},
                                     status=500)
        if n != old:
            log.info("app: %s set workers at once %s -> %s", dev.get("name"), old, n)
        return web.json_response({"ok": True, **_workers_state()})

    # ---- restart Walter (app_restart.py): compile check, worker warning, 30 s limit
    from .app_restart import Restarter
    restarter = apphub.restarter = Restarter(tasks, cfg.data_dir)

    @routes.get("/api/app/restart")
    async def app_restart_info(request):
        apphub.auth(request)
        return web.json_response(restarter.info())

    @routes.post("/api/app/restart")
    async def app_restart(request):
        """{force?: bool}: force=True means "Restart anyway" with workers running."""
        dev = apphub.auth(request)
        d = await json_body(request, 512)
        status, body = await restarter.request(dev, force=d.get("force") is True)
        if body.get("restarting"):
            hub.send({"t": "emotion", "name": "sleepy", "hold": 4})
        return web.json_response(body, status=status)

    # ---- wall display: text size (same code path as `walterctl display text-scale`)
    from . import display_settings as disp

    def _display_state() -> dict:
        return {"text_scale": disp.get_text_scale(), "default": disp.TEXT_SCALE_DEFAULT,
                "min": disp.TEXT_SCALE_MIN, "max": disp.TEXT_SCALE_MAX}

    @routes.get("/api/app/display")
    async def app_display(request):
        apphub.auth(request)
        return web.json_response(_display_state())

    @routes.post("/api/app/display")
    async def app_display_set(request):
        """{text_scale: 0.5..1.5}: written to face/display.json; the face applies it within ~2 s."""
        dev = apphub.auth(request)
        d = await json_body(request, 256)
        try:
            new, old = disp.set_text_scale(d.get("text_scale"))
        except ValueError as e:
            return web.json_response({"error": f"text_scale {e}", **_display_state()}, status=400)
        except OSError as e:
            log.warning("app: couldn't save the text scale: %s", e)
            return web.json_response({"error": "couldn't save it"}, status=500)
        if new != old:
            log.info("app: %s set the wall text scale %s -> %s", dev.get("name"), old, new)
        return web.json_response({"ok": True, **_display_state()})

    # ---- wall theme (face/themes.js; same code path as `walterctl display theme`)
    def _theme_state() -> dict:
        return {"theme": disp.get_theme(), "showing": disp.showing_theme(), "themes": list(disp.THEMES),
                "default": disp.THEME_DEFAULT}

    @routes.get("/api/app/theme")
    async def app_theme(request):
        apphub.auth(request)
        return web.json_response(_theme_state())

    @routes.post("/api/app/theme")
    async def app_theme_set(request):
        """{theme: NAME | "auto" | "next" | "random"}: written to face/display.json; live within ~2 s."""
        dev = apphub.auth(request)
        d = await json_body(request, 256)
        want = d.get("theme")
        try:
            if want in ("next", "random"):
                new, old = disp.step_theme(want)
            else:
                new, old = disp.set_theme(want)
        except ValueError as e:
            return web.json_response({"error": f"theme {e}", **_theme_state()}, status=400)
        except OSError as e:
            log.warning("app: couldn't save the theme: %s", e)
            return web.json_response({"error": "couldn't save it"}, status=500)
        if new != old:
            log.info("app: %s set the wall theme %s -> %s", dev.get("name"), old, new)
        return web.json_response({"ok": True, **_theme_state()})

    # ---- while you were away + Web Push
    @routes.get("/api/app/away")
    async def app_away(request):
        apphub.auth(request)
        items = apphub.away.items()
        return web.json_response({"items": items[::-1][:50], "unseen": sum(1 for i in items if not i.get("seen"))})

    @routes.post("/api/app/away")
    async def app_away_change(request):
        """{action: "seen", ids?: [..]} or {action: "clear"}"""
        apphub.auth(request)
        d = await json_body(request, 2048)
        if d.get("action") == "seen":
            ids = d.get("ids")
            if ids is not None and not (isinstance(ids, list) and all(isinstance(i, int) for i in ids)):
                return web.json_response({"error": "ids must be a list of numbers"}, status=400)
            apphub.away.mark_seen(ids)
        elif d.get("action") == "clear":
            apphub.away.clear()
        else:
            return web.json_response({"error": "action must be seen or clear"}, status=400)
        apphub.away_changed()
        return web.json_response({"ok": True, "unseen": len(apphub.away.unseen())})

    @routes.get("/api/app/push")
    async def app_push_info(request):
        dev = apphub.auth(request)
        from .webpush import available
        ok = available()
        return web.json_response({"available": ok, "key": apphub.push.public_key() if ok else "",
                                  "subscribed": apphub.push.count(dev["id"])})

    @routes.post("/api/app/push")
    async def app_push(request):
        """{action: "subscribe", subscription: {...}} | {action: "unsubscribe", endpoint?} | {action: "test"}"""
        dev = apphub.auth(request)
        if dev["id"] == "kiosk":
            return web.json_response({"error": "pair this device to get notifications"}, status=400)
        d = await json_body(request, 4096)
        a = d.get("action")
        if a == "subscribe":
            try:
                apphub.push.subscribe(dev["id"], d.get("subscription"))
            except ValueError as e:
                return web.json_response({"error": str(e)}, status=400)
            log.info("web push: %s subscribed", dev["name"])
        elif a == "unsubscribe":
            ep = d.get("endpoint")
            apphub.push.unsubscribe(dev["id"], ep if isinstance(ep, str) else None)
        elif a == "test":
            fut = apphub.push.send("Walter", "Notifications work. I'll send reminders here when you're away.",
                                   tag="test")
            n = await asyncio.wrap_future(fut) if fut is not None else 0
            return web.json_response({"ok": n > 0, "delivered": n})
        else:
            return web.json_response({"error": "unknown action"}, status=400)
        return web.json_response({"ok": True, "subscribed": apphub.push.count(dev["id"])})

    @routes.post("/api/app/bump")
    async def app_bump(request):
        """walterctl app-bump: tell connected apps about a new version (this computer only)."""
        if not D.is_kiosk(request.remote, request.headers):
            raise web.HTTPForbidden(text="local only")
        apphub.check_version(force=True)
        return web.json_response({"version": apphub.version, "build": apphub.build,
                                  "clients": len(apphub.clients)})

    @routes.post("/api/app/revoked")
    async def app_revoked(request):
        if not D.is_kiosk(request.remote, request.headers):
            raise web.HTTPForbidden(text="local only")
        apphub.kick_revoked()
        return web.json_response({"ok": True})

    # ---- the websocket
    @routes.get("/app/ws")
    async def app_ws(request):
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=512 * 1024)
        await ws.prepare(request)
        ip = D.client_ip(request.remote, request.headers)
        kiosk = D.is_kiosk(request.remote, request.headers)
        try:
            first = await ws.receive(timeout=10)
            d = json.loads(first.data) if first.type == WSMsgType.TEXT else {}
        except (asyncio.TimeoutError, ValueError, TypeError):
            d = {}
        if d.get("t") != "auth":
            await ws.close(code=4400, message=b"auth first")
            return ws
        token = d.get("token") or ""
        if kiosk and not token:
            dev = KIOSK
        else:
            wait = apphub.limiter.retry_after(ip)
            dev = None if wait else apphub.store.check(token)
            if dev is None:
                if not wait:
                    apphub.limiter.fail(ip)
                await ws.send_str(json.dumps({"t": "auth", "ok": False, "retry": int(wait),
                                              "error": "too many tries" if wait else "not paired"}))
                await ws.close(code=4401, message=b"unauthorized")
                return ws
            apphub.limiter.ok(ip)
            apphub.store.touch(dev["id"], ip)
        client = AppClient(ws, dev, ip)
        apphub.clients.add(client)
        log.info("app connected: %s (%s)", dev["name"], ip)
        await ws.send_str(json.dumps({"t": "auth", "ok": True, "device": dev, **apphub.status()}))
        for m in apphub.last.values():
            await ws.send_str(json.dumps(m))
        try:
            async for m in ws:
                if m.type == WSMsgType.BINARY:
                    _talk_data(client, m.data)
                elif m.type == WSMsgType.TEXT:
                    try:
                        msg = json.loads(m.data)
                    except ValueError:
                        continue
                    if isinstance(msg, dict):
                        await _handle(client, msg)
        finally:
            apphub.clients.discard(client)
            log.info("app disconnected: %s", dev["name"])
        return ws

    def _talk_data(client: AppClient, data: bytes):
        if client.talk is None or not data or data[0] != 1:
            return
        if len(client.talk) + len(data) - 1 > cfg.app.max_talk_s * PCM_SR * 2:
            return   # long enough: the rest is dropped
        client.talk.extend(data[1:])

    async def _handle(client: AppClient, msg: dict):
        t = msg.get("t")
        if t == "ping":
            client.send({"t": "pong", "time": time.time()})
        elif t == "chat":
            text = str(msg.get("text") or "").strip()[:MAX_CHAT]
            images = apphub.image_paths(msg.get("images"))
            if text or images:
                apphub.submit(client, text, wall=bool(msg.get("wall")), audio=bool(msg.get("audio")),
                              cid=msg.get("id"), images=images)
        elif t == "talk_start":
            client.talk, client.talk_t0 = bytearray(), time.time()
        elif t == "talk_cancel":
            client.talk = None
        elif t == "talk_end":
            pcm, client.talk = bytes(client.talk or b""), None
            secs = len(pcm) / (PCM_SR * 2)
            if secs < 0.3:
                client.send({"t": "heard", "text": "", "error": "That was too short. Hold the button while you talk."})
                return
            client.send({"t": "transcribing", "seconds": round(secs, 1)})
            try:
                text, how = await apphub.transcribe(pcm)
            except Exception as e:
                log.warning("app transcription failed: %s", e)
                text, how = "", "none"
            if not text:
                client.send({"t": "heard", "text": "", "provider": how,
                             "error": "I didn't catch that." if how != "none"
                             else "My ears aren't running right now; type instead."})
                return
            client.send({"t": "heard", "text": text, "provider": how, "id": msg.get("id")})
            apphub.submit(client, text, wall=bool(msg.get("wall")), audio=msg.get("audio", True) is not False,
                          voice=True, cid=msg.get("id"))
        elif t == "stop":
            apphub.stop(client.device["id"])

    # ---- chat images (app_uploads.py)
    @routes.post("/api/app/upload")
    async def app_upload(request):
        """multipart/form-data, one or more "image" parts (max 4, 8 MB each)."""
        from .app_uploads import MAX_PER_MESSAGE, UploadError
        apphub.auth(request)
        ctype = (request.headers.get("Content-Type") or "").lower()
        if not ctype.startswith("multipart/form-data"):
            raise web.HTTPUnsupportedMediaType(text="multipart/form-data only")
        store = apphub.uploads
        reader = await request.multipart()
        saved, errors, parts = [], [], 0
        while True:
            part = await reader.next()
            if part is None:
                break
            if getattr(part, "name", None) != "image":
                await part.release()
                continue
            parts += 1
            if parts > MAX_PER_MESSAGE:
                await part.release()
                errors.append(f"Only {MAX_PER_MESSAGE} images per message.")
                continue
            buf = bytearray()
            too_big = False
            while True:
                chunk = await part.read_chunk(64 * 1024)
                if not chunk:
                    break
                if len(buf) + len(chunk) > store.max_bytes:
                    too_big = True
                    await part.release()
                    break
                buf.extend(chunk)
            try:
                if too_big:
                    raise UploadError(f"That image is too big (max {store.max_bytes // (1024 * 1024)} MB).", 413)
                saved.append(await asyncio.get_running_loop().run_in_executor(None, store.save, bytes(buf)))
            except UploadError as e:
                errors.append(str(e))
                if e.status == 429:
                    break
        if not saved:
            return web.json_response({"ok": False, "error": errors[0] if errors else "No image in that."},
                                     status=400)
        return web.json_response({"ok": True, "images": saved, "errors": errors})

    @routes.get("/api/app/upload/{image_id}")
    async def app_upload_get(request):
        apphub.auth(request)
        p = apphub.uploads.path(request.match_info["image_id"])
        if p is None:
            raise web.HTTPNotFound(text="gone")
        return web.Response(body=p.read_bytes(), content_type=apphub.uploads.mime(p),
                            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                                     "Content-Disposition": "inline",
                                     "Content-Security-Policy": "default-src 'none'; sandbox"})

    # the Remote tab: drive the wall browser from the app (app_remote.py)
    from . import app_remote
    apphub.remote = app_remote.add_routes(routes, apphub, json_body, getattr(cfg.brain, "home", None))
    # the Player: podcast + YouTube controls (app_player.py)
    from . import app_player
    app_player.add_routes(routes, apphub, json_body)
    # beta feedback + consented diagnostics (feedback.py, diagnostics.py)
    from . import feedback
    feedback.add_routes(routes, apphub, json_body, cfg)
    # the floating mode switcher: Board / Face / Build / Conversation / Chat / Focus / Sleep (app_mode.py)
    from . import app_mode
    apphub.modes = app_mode.add_routes(routes, apphub, json_body, conductor, hub)
    # the Rotate button: portrait / landscape / flip, live with wlr-randr (app_rotate.py, screen_rotate.py)
    from . import app_rotate
    app_rotate.add_routes(routes, apphub, json_body)
    # Settings > Cloud Link: on with an invite code / off / rotate (app_cloud_link.py, cloud_link.py)
    from . import app_cloud_link
    apphub.cloud_link = app_cloud_link.add_routes(routes, apphub, json_body, cfg, conductor)
    # the Projects screen: list / switch / add / edit workspaces (app_workspaces.py, workspaces.py); no delete
    from . import app_workspaces
    app_workspaces.add_routes(routes, apphub, json_body)
    # the Schedule screen: reminders, routines, do-later jobs, loops; system timers read-only (app_schedule.py)
    from . import app_schedule
    app_schedule.add_routes(routes, apphub, json_body, reminders, cfg, conductor)
    # full chat history for the Chat tab: scroll back + search (app_history.py, chatlog.py); read-only
    from . import app_history
    app_history.add_routes(routes, apphub)
    # presentation mode: a 60 s scripted self-demo with demo data on the wall (present.py)
    from . import present
    apphub.present = present.add_routes(routes, apphub, json_body, conductor, getattr(cfg.brain, "home", None))

    app.add_routes(routes)
    if app_dir.exists():
        app.router.add_static("/app/", app_dir, show_index=False, append_version=False)


async def _run(path, *args) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(str(path), *args, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
        return proc.returncode or 0, out.decode(errors="replace")
    except Exception as e:
        log.warning("%s failed: %s", Path(path).name, e)
        return 1, ""


def csp_headers(request) -> dict:
    host = request.host
    return {
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            f"connect-src 'self' ws://{host} wss://{host}; img-src 'self' data: blob:; "
            "media-src 'self' blob:; worker-src 'self'; manifest-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
        "Permissions-Policy": "microphone=(self), camera=()",
    }


async def watch_app(apphub: AppHub):
    """New app files -> tell open apps; a revoked device -> close its socket;
    chat images older than 7 days -> deleted (hourly)."""
    dev_mtime = None
    next_clean = 0.0
    while True:
        await asyncio.sleep(3)
        if time.time() >= next_clean:
            next_clean = time.time() + 3600
            try:
                gone = apphub.uploads.cleanup()
                if gone:
                    log.info("deleted %d old app chat image(s)", gone)
            except Exception:
                log.exception("app upload cleanup failed")
        try:
            apphub.check_version()
            mt = apphub.store.path.stat().st_mtime_ns if apphub.store.path.exists() else 0
            if mt != dev_mtime:
                if dev_mtime is not None:
                    apphub.kick_revoked()
                dev_mtime = mt
        except Exception:
            log.exception("app watch failed")
