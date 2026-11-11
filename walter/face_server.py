"""Face hub: the brain's side of "one brain, many faces" (walter/faces.py, docs/FACES.md).

Routes (all LAN / tailnet only, never through the public tunnel; server.gate_ok):

  /face/pair            pairing page a tablet opens from the wall's QR (code + name)
  /face/ , /face/<file> the normal face page for a paired face, authenticated by an
                        HttpOnly cookie (set from ?token=... once, then the token is
                        dropped from the address). display.json / card.json are that
                        face's own files (face/display-<id>.json, face/card-<id>.json).
  /face/ws              the page's websocket: Walter's state, mood and captions (a
                        filtered copy of the wall's), plus this face's own turns.
  /api/face/*           the face agent (bin/walter-face), Bearer face token:
                        pair, hello (heartbeat + capabilities), settings (own only),
                        events (SSE: what to say/show/play), heard (finished turns +
                        mic score), audio-turn (raw audio when the face has no
                        whisper), audio/<id> (the brain's TTS for a reply).
  /api/faces/*          this computer only (walterctl): send (handoff), status, changed.

A face can never change brain config or run commands: only its own settings.
Heard text is untrusted exactly like the wall mic's (the conductor frames it).
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import re
import secrets
import time
from pathlib import Path

from aiohttp import WSMsgType, web

from . import dashboard as dash
from . import devices as D
from . import display_settings as disp
from . import faces as F
from .tags import TAG_RE

log = logging.getLogger("walter.faces")

COOKIE = "walter_face"
COOKIE_MAX_AGE = 365 * 86400
MAX_TEXT = 1000
MAX_BODY = 8192
PCM_SR = 16000
EVENTS_KEEP = 200
AUDIO_TTL_S = 300
STOP_RE = re.compile(r"^\W*(?:(?:hey\s+)?\w+\W*)?(stop|shush|hush|quiet|be quiet|never ?mind|cancel|enough)\W*$", re.I)
# Wall face messages a remote face page may see (no setup, network, camera or browser details).
PAGE_FORWARD = {"state", "emotion", "action", "level", "caption", "listen", "config", "jobs", "mute",
                "followup", "cloud", "look", "chat", "convo", "reload"}
# Files a face page may never read, whoever asks (the wall browser's remote-control token etc.).
PAGE_DENY = {"browser.json", "setup.json", "firstrun.json"}
FILE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-/]{0,200}$")


def face_paths(path: str) -> bool:
    return path in ("/face", "/api/face") or path.startswith(("/face/", "/api/face/"))


def clean_text(s, limit: int = MAX_TEXT) -> str:
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]+", " ", str(s or ""))
    return re.sub(r"\s+", " ", s).strip()[:limit]


def bearer(request) -> str:
    auth = request.headers.get("Authorization", "")
    return auth[7:].strip() if auth[:7].lower() == "bearer " else ""


# ------------------------------------------------------------------ replies
class FaceReply:
    """One brain turn answered on a face: same interface as app_server.AppReply.
    Sentences go to the face agent as events (+ TTS audio when it has a speaker)
    and to the face page as captions; the wall stays quiet."""

    def __init__(self, hub: "FaceHub", meta: dict):
        self.hub = hub
        self.face_id = meta.get("face_id", "")
        self.turn = meta.get("face_turn", 0)
        self.audio = bool(meta.get("audio"))
        self.cancelled = False
        self.raw_text = ""
        self.seq = 0
        self.spoken: list[str] = []
        self._q: asyncio.Queue | None = None
        self._worker = None

    def emit(self, msg: dict):
        self.hub.emit(self.face_id, msg)

    def say(self, text: str):
        if self.cancelled:
            return
        self.seq += 1
        self.spoken.append(text)
        self.emit({"t": "say", "turn": self.turn, "seq": self.seq, "text": text})
        self.hub.to_pages(self.face_id, {"t": "caption", "who": "walter", "text": text})
        if self.audio:
            if self._q is None:
                self._q = asyncio.Queue()
                self._worker = asyncio.ensure_future(self._audio_worker())
            self._q.put_nowait((self.seq, text))

    def mood(self, name: str):
        self.emit({"t": "mood", "turn": self.turn, "name": name})
        self.hub.to_pages(self.face_id, {"t": "emotion", "name": name, "hold": 8})

    def state(self, st: str):
        self.emit({"t": "state", "turn": self.turn, "state": st})
        self.hub.to_pages(self.face_id, {"t": "state", "state": st})

    def raw(self, chunk: str):
        self.raw_text += chunk

    def reset(self):
        self.raw_text = ""

    def finish(self, silent: bool = False):
        text = re.sub(r"[ \t]{2,}", " ", TAG_RE.sub("", self.raw_text)).strip()
        self.emit({"t": "reply", "turn": self.turn, "text": "" if (silent or self.cancelled) else text[:4000],
                   "silent": silent, "cancelled": self.cancelled})
        self.hub.to_pages(self.face_id, {"t": "state", "state": "idle"})
        if not silent and not self.cancelled:
            # they may answer without the name: keep listening on this face for a bit
            secs = len(" ".join(self.spoken).split()) / 2.7
            self.hub.followup[self.face_id] = time.time() + secs + self.hub.cfg.faces.followup_s
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
                wav = await self.hub.synth(text)
            except Exception as e:
                log.warning("face tts failed: %s", e)
                wav = None
            if self.cancelled:
                break
            if wav is None:
                self.emit({"t": "noaudio", "turn": self.turn, "seq": seq, "text": text})
                continue
            self.emit({"t": "audio", "turn": self.turn, "seq": seq, "url": self.hub.store_audio(self.face_id, wav)})


# ------------------------------------------------------------------ the hub
class FaceHub:
    def __init__(self, cfg, conductor, hub=None, store: F.FaceStore | None = None):
        self.cfg = cfg
        self.conductor = conductor
        self.hub = hub                      # server.Hub: the wall face (version, replays)
        self.store = store or F.FaceStore(wall_name=cfg.faces.wall_name)
        self.arbiter = F.Arbiter(cfg.faces.window_s, cfg.faces.match)
        self.limiter = D.RateLimiter(max_fails=8, window_s=600, block_s=900, global_max=60)
        self.pair_limiter = D.RateLimiter(max_fails=5, window_s=600, block_s=900, global_max=20)
        self.events: dict[str, collections.deque] = {}
        self._wake: dict[str, asyncio.Event] = {}
        self._seq = int(time.time() * 1000)  # monotonic across restarts: a reconnecting agent's ?after= stays valid
        self.online: dict[str, dict] = {}   # face id -> {"at", "ears", "speaker", "ip", "streams"}
        self.pages: dict = {}               # websocket -> face id
        self.audio: dict[str, tuple[str, bytes, float]] = {}
        self.followup: dict[str, float] = {}
        self.current: FaceReply | None = None
        self.turns = 0
        self.last_decision: dict | None = None
        self._timers: dict[int, asyncio.TimerHandle] = {}
        self.mdns = None
        self.reminders = None   # conductor.Reminders (set by build_app): "send the timer to the office"
        self.cluster = None     # cluster.ClusterNode (set by build_app): brain list for failover

    # ------------------------------------------------------------ cluster
    def brains(self) -> dict:
        """For the face agent: every brain it may use, the cluster lead first (docs/CLUSTER.md).
        {} on a single install, so faces see exactly what they saw before."""
        cl = getattr(self, "cluster", None)
        if cl is None or not cl.store.has_peers():
            return {}
        try:
            return {"brains": cl.brain_urls(), "lead": cl.lead_url(), "role": cl.role}
        except Exception:
            log.exception("faces: cluster brain list failed")
            return {}

    # ------------------------------------------------------------ names
    def wake_words(self) -> tuple[str, ...]:
        return tuple(w.lower() for w in (getattr(self.cfg.ears, "wake_words", None) or ["walter"]))

    def faces(self) -> list[dict]:
        return self.store.list()

    def named(self, text: str) -> dict | None:
        """The face this sentence calls by name, if any (the conductor's wall mic uses it too)."""
        return F.name_target(text, self.faces(), self.wake_words())

    # ------------------------------------------------------------ sending
    def emit(self, face_id: str, msg: dict) -> int:
        """Queue an event for a face agent (SSE /api/face/events)."""
        self._seq += 1
        q = self.events.setdefault(face_id, collections.deque(maxlen=EVENTS_KEEP))
        q.append((self._seq, dict(msg, time=round(time.time(), 3))))
        ev = self._wake.pop(face_id, None)
        if ev is not None:
            ev.set()
        return self._seq

    def pending(self, face_id: str, after: int) -> list[tuple[int, dict]]:
        return [(s, m) for s, m in self.events.get(face_id, ()) if s > after]

    async def wait(self, face_id: str, timeout: float) -> bool:
        ev = self._wake.setdefault(face_id, asyncio.Event())
        try:
            await asyncio.wait_for(ev.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def to_pages(self, face_id: str, msg: dict):
        data = json.dumps(msg)
        for ws, fid in list(self.pages.items()):
            if fid == face_id and not ws.closed:
                asyncio.ensure_future(_send(ws, data))

    def from_wall(self, msg: dict):
        """server.Hub.send hook: mirror Walter's state/mood/captions to the face pages."""
        if msg.get("t") not in PAGE_FORWARD or not self.pages:
            return
        data = json.dumps(msg)
        for ws in list(self.pages):
            if not ws.closed:
                asyncio.ensure_future(_send(ws, data))

    def store_audio(self, face_id: str, wav: bytes) -> str:
        now = time.time()
        for k in [k for k, v in self.audio.items() if v[2] < now]:
            self.audio.pop(k, None)
        aid = secrets.token_urlsafe(12)
        self.audio[aid] = (face_id, wav, now + AUDIO_TTL_S)
        return f"/api/face/audio/{aid}"

    async def synth(self, text: str) -> bytes | None:
        """WAV bytes of one sentence in Walter's current voice, or None (no real voice)."""
        voice = getattr(self.conductor, "voice", None)
        if voice is None or not hasattr(voice, "_timed_synth"):
            return None
        from .app_server import wav_bytes
        loop = asyncio.get_running_loop()
        audio, sr, _w, _s, cloud = await loop.run_in_executor(voice._synth_pool, voice._timed_synth, text)
        if voice.synth.kind == "mime" and not cloud:
            return None
        return wav_bytes(audio, sr)

    # ------------------------------------------------------------ auth
    def auth(self, request) -> dict:
        """The face making this request (Bearer token), or raise 401/429. No kiosk exemption."""
        ip = D.client_ip(request.remote, request.headers)
        wait = self.limiter.retry_after(ip)
        if wait:
            raise web.HTTPTooManyRequests(text="too many wrong tries; wait a bit",
                                          headers={"Retry-After": str(int(wait) + 1)})
        face = self.store.check(bearer(request))
        if face is None:
            self.limiter.fail(ip)
            raise web.HTTPUnauthorized(text="pair this face first (walterctl face pair)",
                                       headers={"WWW-Authenticate": "Bearer"})
        self.limiter.ok(ip)
        self.store.touch(face["id"], ip)
        return face

    def page_face(self, request) -> dict | None:
        """The face whose cookie this page request carries (rate-limited), or None."""
        tok = request.cookies.get(COOKIE, "")
        if not tok:
            return None
        ip = D.client_ip(request.remote, request.headers)
        if self.limiter.retry_after(ip):
            return None
        face = self.store.check(tok)
        if face is None:
            self.limiter.fail(ip)
        return face

    # ------------------------------------------------------------ presence
    def hello(self, face: dict, ip: str, caps: dict) -> dict:
        o = self.online.setdefault(face["id"], {"streams": 0})
        o.update(at=time.time(), ip=ip, ears=bool(caps.get("ears")), speaker=bool(caps.get("speaker")),
                 version=clean_text(caps.get("version"), 20))
        mq = caps.get("mic_quality")
        self.store.touch(face["id"], ip, F.parse_score(mq) if isinstance(mq, (int, float)) else None)
        return o

    def is_online(self, face_id: str, now: float | None = None) -> bool:
        o = self.online.get(face_id)
        now = time.time() if now is None else now
        return bool(o and (o.get("streams") or now - o.get("at", 0) < self.cfg.faces.ears_timeout_s))

    def has_speaker(self, face_id: str) -> bool:
        o = self.online.get(face_id) or {}
        return self.is_online(face_id) and bool(o.get("speaker"))

    def ears_active(self, now: float | None = None) -> bool:
        """Is any face with a microphone listening? (then the wall's own turns wait for the arbiter)"""
        now = time.time() if now is None else now
        ids = self.store.ids()
        return any(fid in ids and o.get("ears") and self.is_online(fid, now) and
                   not (self.store.get(fid) or {}).get("settings", {}).get("muted")
                   for fid, o in self.online.items())

    def status(self) -> dict:
        now = time.time()
        out = []
        for f in self.faces():
            o = self.online.get(f["id"], {})
            out.append({**{k: f.get(k) for k in ("id", "name", "room", "kind", "default_workspace",
                                                   "last_seen", "mic_quality")},
                        "online": f["id"] == F.WALL_ID or self.is_online(f["id"], now),
                        "ears": bool(o.get("ears")), "speaker": bool(o.get("speaker")),
                        "pages": sum(1 for v in self.pages.values() if v == f["id"])})
        return {"faces": out, "last_decision": self.last_decision, "window_s": self.arbiter.window_s}

    # ------------------------------------------------------------ hearing
    def addressed(self, face_id: str, text: str) -> bool:
        wake = getattr(self.conductor, "wake_re", None)
        if wake is not None and wake.search(text):
            return True
        if self.named(text):
            return True
        return time.time() < self.followup.get(face_id, 0)

    def heard(self, face: dict, text: str, score: float, meta: dict | None = None) -> str:
        """A finished utterance from a face's mic. Returns what happened:
        "queued" (in the arbiter), "ignored", "muted", "stopped" or "empty"."""
        text = clean_text(text)
        if not text:
            return "empty"
        fid = face["id"]
        if (face.get("settings") or {}).get("muted") or getattr(self.conductor, "user_muted", False):
            return "muted"
        cur = self.current
        if STOP_RE.match(text) and cur is not None and cur.face_id == fid and not cur.cancelled:
            self.stop(fid)
            return "stopped"
        if not self.addressed(fid, text):
            log.info("ignored (face %s, no wake word or name): %s", face["name"], text[:200])
            return "ignored"
        self.followup.pop(fid, None)
        self._offer(fid, text, score, meta or {})
        return "queued"

    def offer_wall(self, text: str, meta: dict | None, score: float):
        """The wall's own mic heard a turn meant for Walter while faces are listening:
        it joins the arbiter (submitted as a normal voice turn if the wall wins)."""
        self._offer(F.WALL_ID, text, score, {"wall_meta": meta})

    def _offer(self, fid: str, text: str, score: float, meta: dict):
        kind, gid = self.arbiter.offer(fid, text, score, meta)
        if kind == "new":
            try:
                loop = asyncio.get_running_loop()
                self._timers[gid] = loop.call_later(self.arbiter.window_s, self._close, gid)
            except RuntimeError:
                self._close(gid)

    def _close(self, gid: int):
        self._timers.pop(gid, None)
        g = self.arbiter.close(gid)
        if g is None:
            return
        try:
            self.deliver(F.decide(g, self.faces(), self.wake_words()))
        except Exception:
            log.exception("faces: delivering a turn failed")

    def deliver(self, d: dict):
        face = self.store.get(d["face"]) or self.store.get(d["entry"]["face"])
        names = {f["id"]: f["name"] for f in self.faces()}
        heard = [names.get(x, x) for x in d["heard_by"]]
        self.last_decision = {"face": face["name"] if face else d["face"], "named": d["named"],
                              "heard_by": heard, "score": d["score"], "at": round(time.time(), 1)}
        if len(d["heard_by"]) > 1 or d["named"]:
            log.info("faces: %s answers (%s; heard by %s)", self.last_decision["face"],
                     "called by name" if d["named"] else f"best mic {d['score']:.2f}", ", ".join(heard))
        if face is None or face["id"] == F.WALL_ID:
            wm = d["entry"]["meta"].get("wall_meta") if d["entry"]["face"] == F.WALL_ID else None
            self.conductor.submit("voice", d["text"], dict(wm) if wm else None)
            return
        self.turns += 1
        meta = {"route": "face", "face_id": face["id"], "face": face["name"], "room": face.get("room") or "",
                "score": d["score"], "named": d["named"], "heard_by": heard, "face_turn": self.turns,
                "audio": self.has_speaker(face["id"]),
                "workspace": face.get("default_workspace")}
        spk = d["entry"]["meta"].get("speaker")
        if spk:
            meta["speaker"] = spk
        self.to_pages(face["id"], {"t": "caption", "who": "you", "text": d["text"]})
        self.emit(face["id"], {"t": "heard", "text": d["text"], "turn": self.turns})
        self.conductor.submit("face", d["text"], meta)

    def reply(self, meta: dict) -> FaceReply:
        """Called by the conductor when a face turn starts."""
        r = FaceReply(self, meta)
        self.current = r
        return r

    def stop(self, face_id: str):
        r = self.current
        if r is not None and r.face_id == face_id and not r.cancelled:
            r.cancelled = True
            try:
                asyncio.ensure_future(self.conductor.brain.interrupt())
            except Exception:
                pass
        self.emit(face_id, {"t": "stop"})

    async def transcribe(self, pcm: bytes) -> str:
        """Audio from a face without its own whisper: same ears as the wall."""
        loop = asyncio.get_running_loop()
        sp = getattr(self.conductor, "speech", None)
        if sp is not None and getattr(sp, "stt", "") == "openai" and sp.has_key():
            from .ears import hint_prompt
            text = await loop.run_in_executor(None, sp.transcribe, pcm, hint_prompt(self.cfg.ears.hotwords))
            if text is not None:
                return text.strip()
        ears = getattr(self.conductor, "ears", None)
        if ears is None or not hasattr(ears, "transcribe_pcm"):
            return ""
        return ((await loop.run_in_executor(None, ears.transcribe_pcm, pcm)) or "").strip()

    # ------------------------------------------------------------ handoff
    async def send(self, ref: str, kind: str, d: dict) -> dict:
        """walterctl face send NAME text|card|media|stop: put something on another face."""
        face = self.store.get(ref)
        if face is None:
            raise KeyError(f"no face called {ref!r}")
        fid = face["id"]
        if kind == "card":
            title = dash.clean_text(d.get("title") or "From Walter", 80)
            body = str(d.get("body") or "").replace("\r", "")[:1200]
            try:
                secs = max(5, min(3600, int(d.get("seconds") or 120)))
            except (TypeError, ValueError):
                secs = 120
            if fid == F.WALL_ID:
                dash.show_card(title, body, secs)
            else:
                dash.show_card(title, body, secs, out=card_path(fid))
                self.emit(fid, {"t": "card", "title": title, "body": body, "seconds": secs})
            return {"ok": True, "face": face["name"], "kind": "card"}
        if kind == "text":
            text = clean_text(d.get("text"), 600)
            if not text:
                raise ValueError("nothing to say")
            if fid == F.WALL_ID:
                self.conductor.voice.say(text)
                return {"ok": True, "face": face["name"], "kind": "text", "spoken": True}
            plain = re.sub(r"\[[^\]]*\]\s*", "", text).strip()
            self.to_pages(fid, {"t": "caption", "who": "walter", "text": plain})
            if self.has_speaker(fid):
                self.emit(fid, {"t": "say", "text": plain, "turn": 0, "seq": 1})
                wav = await self.synth(plain)
                if wav is not None:
                    self.emit(fid, {"t": "audio", "turn": 0, "seq": 1, "url": self.store_audio(fid, wav)})
                return {"ok": True, "face": face["name"], "kind": "text", "spoken": True}
            dash.show_card("Walter", plain, 60, out=card_path(fid))
            return {"ok": True, "face": face["name"], "kind": "text", "spoken": False,
                    "note": "no speaker agent online there: shown as a card"}
        if kind == "media":
            url = str(d.get("url") or "")
            if not re.match(r"^https?://[^\s\"'<>]{3,2000}$", url):
                raise ValueError("media needs an http(s) url")
            if fid == F.WALL_ID:
                raise ValueError("media handoff goes to another face (the wall plays it itself)")
            if not self.has_speaker(fid):
                raise LookupError(f"{face['name']} has no speaker agent online (run walter-face there)")
            try:
                start = max(0.0, float(d.get("start") or 0))
            except (TypeError, ValueError):
                start = 0.0
            ev = {"t": "media", "url": url, "title": dash.clean_text(d.get("title") or "", 120),
                  "start": round(start, 1), "source": dash.clean_text(d.get("source") or "", 20)}
            self.emit(fid, ev)
            self.to_pages(fid, {"t": "caption", "who": "walter", "text": "Playing " + (ev["title"] or "it") + " here"})
            return {"ok": True, "face": face["name"], "kind": "media"}
        if kind == "stop":
            self.stop(fid)
            return {"ok": True, "face": face["name"], "kind": "stop"}
        if kind == "reminder":
            rem = self.reminders
            rid = d.get("id")
            if rem is None or isinstance(rid, bool) or not isinstance(rid, int):
                raise ValueError("a reminder number is needed (walterctl reminders)")
            item = next((i for i in rem.items if i.get("id") == rid), None)
            if item is None:
                raise KeyError(f"no reminder #{rid}")
            if item.get("kind") == "do":
                raise ValueError("routines stay with the brain; only reminders move")
            if fid == F.WALL_ID:
                item.pop("face", None)
            else:
                item["face"] = fid
            rem._save()
            return {"ok": True, "face": face["name"], "kind": "reminder", "id": rid}
        raise ValueError("kind must be text, card, media, reminder or stop")

    def deliver_reminder(self, face_id: str, text: str) -> bool:
        """A due reminder that was sent to another face ("send the timer to the office"):
        said there (speaker agent) and shown as a card. False = the face is gone: the
        wall says it as usual."""
        face = self.store.get(face_id)
        if face is None or face["id"] == F.WALL_ID:
            return False
        try:
            dash.show_card("Reminder", text, 600, out=card_path(face["id"]))
        except (OSError, ValueError):
            log.exception("faces: reminder card failed")
        if self.has_speaker(face["id"]):
            asyncio.ensure_future(self.send(face["id"], "text", {"text": "Reminder: " + text}))
        else:
            self.to_pages(face["id"], {"t": "caption", "who": "walter", "text": "Reminder: " + text})
        log.info("faces: reminder delivered on %s", face["name"])
        return True

    def kick_forgotten(self):
        ids = self.store.ids()
        for ws, fid in list(self.pages.items()):
            if fid not in ids:
                asyncio.ensure_future(ws.close(code=4401, message=b"forgotten"))
                self.pages.pop(ws, None)
        for fid in [f for f in self.online if f not in ids]:
            self.online.pop(fid, None)
            self.emit(fid, {"t": "forgotten"})


async def _send(ws, data: str):
    try:
        await ws.send_str(data)
    except Exception:
        pass


def card_path(face_id: str) -> Path:
    if not F.valid_id(face_id) or face_id == F.WALL_ID:
        raise ValueError("bad face id")
    return Path(dash.FACE_DIR) / f"card-{face_id}.json"


# ------------------------------------------------------------------ mDNS
def advertise(port: int, name: str = "Walter"):
    """Announce _walter._tcp on the home network so face agents find the brain.
    Needs python zeroconf; without it, faces take --brain URL (logged once)."""
    try:
        import socket

        from zeroconf import ServiceInfo, Zeroconf
    except Exception:
        log.info("mDNS: python zeroconf isn't installed, so faces won't find me by themselves "
                 "(walter-face --brain http://<this-ip>:%d works; pip install zeroconf to enable)", port)
        return None
    try:
        ip = dash.lan_ip() or ""
        if not ip:
            return None
        info = ServiceInfo("_walter._tcp.local.", f"{F.clean_name(name) or 'Walter'}._walter._tcp.local.",
                           addresses=[socket.inet_aton(ip)], port=int(port),
                           properties={"path": "/api/face", "v": "1"})
        zc = Zeroconf()
        zc.register_service(info)
        log.info("mDNS: advertising _walter._tcp on %s:%d", ip, port)
        return (zc, info)
    except Exception as e:
        log.warning("mDNS: couldn't advertise (%s)", e)
        return None


# ------------------------------------------------------------------ routes
PAIR_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Add a Walter face</title>
<style>body{font:18px system-ui,sans-serif;background:#0b1020;color:#e8eefc;display:flex;min-height:100vh;
align-items:center;justify-content:center;margin:0}form{max-width:340px;width:90%}input,select,button{width:100%;
box-sizing:border-box;font:inherit;padding:12px;margin:6px 0 14px;border-radius:10px;border:1px solid #33406a;
background:#141b33;color:inherit}button{background:#4d8dff;border:0;font-weight:600}#err{color:#ff9a9a;min-height:1em}</style>
</head><body><form id="f"><h2>Add this screen as a Walter face</h2>
<label>Code on Walter's screen<input id="code" inputmode="numeric" autocomplete="one-time-code" maxlength="7"></label>
<label>Name for this face<input id="name" maxlength="40" placeholder="Kitchen"></label>
<label>Room (optional)<input id="room" maxlength="40" placeholder="kitchen"></label>
<label>Kind<select id="kind"><option value="tablet">tablet</option><option value="laptop">laptop</option>
<option value="other">other</option></select></label>
<button>Join</button><div id="err"></div></form>
<script src="/face/pair.js"></script></body></html>"""

PAIR_JS = """(function(){var h=new URLSearchParams(location.hash.slice(1));
var c=document.getElementById('code'),n=document.getElementById('name');
if(h.get('code'))c.value=h.get('code');if(h.get('name'))n.value=h.get('name');
history.replaceState(null,'',location.pathname);
document.getElementById('f').onsubmit=function(e){e.preventDefault();
fetch('/api/face/pair',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',
body:JSON.stringify({code:c.value,name:n.value,room:document.getElementById('room').value,
kind:document.getElementById('kind').value,cookie:true})}).then(function(r){return r.json().then(function(j){
if(!r.ok)throw new Error(j.error||'failed');location.replace('/face/?face='+encodeURIComponent(j.face.id));});})
.catch(function(err){document.getElementById('err').textContent=err.message;});};})();"""


def add_routes(app: web.Application, cfg, hub, facehub: FaceHub, face_dir: Path):
    routes = web.RouteTableDef()
    face_dir = Path(face_dir)

    async def json_body(request, limit: int = MAX_BODY) -> dict:
        if (request.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            raise web.HTTPUnsupportedMediaType(text="JSON only")
        raw = await request.content.read(limit + 1)
        if len(raw) > limit:
            raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(raw))
        try:
            d = json.loads(raw or b"{}")
        except ValueError:
            raise web.HTTPBadRequest(text="send a JSON object")
        if not isinstance(d, dict):
            raise web.HTTPBadRequest(text="send a JSON object")
        return d

    def set_cookie(resp, token: str, request):
        resp.set_cookie(COOKIE, token, max_age=COOKIE_MAX_AGE, path="/face", httponly=True,
                        samesite="Strict", secure=request.secure)

    def kiosk_only(request):
        if not D.is_kiosk(request.remote, request.headers):
            raise web.HTTPForbidden(text="local only")

    # ---- pairing
    @routes.post("/api/face/pair")
    async def face_pair(request):
        ip = D.client_ip(request.remote, request.headers)
        wait = facehub.pair_limiter.retry_after(ip)
        if wait:
            return web.json_response({"error": "too many tries; wait a few minutes"}, status=429,
                                     headers={"Retry-After": str(int(wait) + 1)})
        d = await json_body(request, 1024)
        try:
            face, token = facehub.store.pair(str(d.get("code", "")), str(d.get("name") or ""),
                                             str(d.get("kind") or "other"), str(d.get("room") or ""))
        except PermissionError as e:
            facehub.pair_limiter.fail(ip)
            return web.json_response({"error": str(e)}, status=403)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        facehub.pair_limiter.ok(ip)
        try:
            disp.seed_face_display(face["id"])
        except (OSError, ValueError):
            log.exception("faces: couldn't create the display file")
        log.info("faces: paired a new face %r (%s) from %s", face["name"], face["kind"], ip)
        if hub is not None:
            hub.send({"t": "emotion", "name": "happy", "hold": 6})
            hub.send({"t": "action", "name": "bounce"})
        try:
            dash.show_card("New face", f"{face['name']} joined.", 6)
        except Exception:
            pass
        page = f"/face/?face={face['id']}"
        if d.get("cookie") is True:   # the browser pairing page: token only in an HttpOnly cookie
            resp = web.json_response({"face": face, "page": page})
            set_cookie(resp, token, request)
            return resp
        return web.json_response({"token": token, "face": face, "page": page,
                                  "brain": {"name": facehub.store.get(F.WALL_ID)["name"]}, **facehub.brains()})

    @routes.get("/face/pair")
    async def face_pair_page(_):
        return web.Response(text=PAIR_PAGE, content_type="text/html")

    @routes.get("/face/pair.js")
    async def face_pair_js(_):
        return web.Response(text=PAIR_JS, content_type="application/javascript")

    # ---- the agent API
    @routes.post("/api/face/hello")
    async def face_hello(request):
        """Heartbeat + capabilities {ears, speaker, version, mic_quality}. Returns my settings."""
        face = facehub.auth(request)
        d = await json_body(request, 1024)
        facehub.hello(face, D.client_ip(request.remote, request.headers), d)
        return web.json_response({**_settings(face), **facehub.brains()})

    def _settings(face: dict) -> dict:
        face = facehub.store.get(face["id"]) or face
        try:
            display = disp.read_display(disp.face_display_path(face["id"]))
        except ValueError:
            display = {}
        return {"face": face, "settings": face.get("settings") or {}, "display": display,
                "page": f"/face/?face={face['id']}", "window_s": facehub.arbiter.window_s,
                "brain": {"name": (facehub.store.get(F.WALL_ID) or {}).get("name", "Walter")}}

    @routes.get("/api/face/settings")
    async def face_settings(request):
        return web.json_response(_settings(facehub.auth(request)))

    @routes.post("/api/face/settings")
    async def face_settings_set(request):
        """Own settings only: volume, muted, captions, ears, and display text_scale/theme/calendar_style/layout."""
        face = facehub.auth(request)
        d = await json_body(request, 1024)
        mine = {k: d[k] for k in F.SETTING_KEYS if k in d}
        shown = {k: d[k] for k in ("text_scale", "theme", "calendar_style", "layout") if k in d}
        extra = set(d) - set(mine) - set(shown)
        if extra:
            return web.json_response({"error": f"not a face setting: {', '.join(sorted(extra))}"}, status=400)
        try:
            path = disp.face_display_path(face["id"])
            if "text_scale" in shown:
                disp.set_text_scale(shown["text_scale"], path)
            if "theme" in shown:
                disp.set_theme(shown["theme"], path)
            if "calendar_style" in shown:
                disp.set_calendar_style(shown["calendar_style"], path)
            if "layout" in shown:
                if shown["layout"] not in ("auto", "compact", "large"):
                    raise ValueError("layout must be auto, compact or large")
                disp.write_display(path, layout=shown["layout"])
            if mine:
                facehub.store.set_settings(face["id"], mine)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"ok": True, **_settings(face)})

    @routes.get("/api/face/events")
    async def face_events(request):
        """Server-sent events for the agent: say / audio / mood / state / reply / card / media / stop."""
        face = facehub.auth(request)
        fid = face["id"]
        try:
            after = int(request.query.get("after") or request.headers.get("Last-Event-ID") or 0)
        except ValueError:
            after = 0
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-store",
                                           "X-Accel-Buffering": "no"})
        await resp.prepare(request)
        o = facehub.online.setdefault(fid, {"streams": 0, "at": time.time()})
        o["streams"] = o.get("streams", 0) + 1
        try:
            if after == 0:
                after = facehub._seq   # a fresh agent: only what happens from now on
            await resp.write(b": hello\n\n")
            while fid in facehub.store.ids():
                for seq, msg in facehub.pending(fid, after):
                    await resp.write(f"id: {seq}\ndata: {json.dumps(msg)}\n\n".encode())
                    after = seq
                if not await facehub.wait(fid, 15):
                    await resp.write(b": ping\n\n")
        except (ConnectionResetError, asyncio.CancelledError, RuntimeError):
            pass
        finally:
            o["streams"] = max(0, o.get("streams", 1) - 1)
            o["at"] = time.time()
        return resp

    @routes.post("/api/face/heard")
    async def face_heard(request):
        """{text, score 0..1, speaker?}: a finished turn from this face's mic."""
        face = facehub.auth(request)
        d = await json_body(request, 4096)
        text = d.get("text")
        if not isinstance(text, str):
            return web.json_response({"error": "text required"}, status=400)
        meta = {}
        if isinstance(d.get("speaker"), str) and d["speaker"].strip():
            meta["speaker"] = "speaker: " + F.clean_name(d["speaker"]) + " (voice match on the face)"
        status = facehub.heard(face, text, F.parse_score(d.get("score")), meta)
        return web.json_response({"ok": True, "status": status})

    @routes.post("/api/face/audio-turn")
    async def face_audio_turn(request):
        """Raw 16 kHz int16 mono PCM (application/octet-stream), ?score=0..1: for faces
        without their own whisper. Transcribed with the wall's ears, then like /heard."""
        face = facehub.auth(request)
        if (request.headers.get("Content-Type") or "").split(";")[0].strip() != "application/octet-stream":
            raise web.HTTPUnsupportedMediaType(text="application/octet-stream only")
        limit = int(cfg.app.max_talk_s * PCM_SR * 2)
        pcm = await request.content.read(limit + 1)
        if len(pcm) > limit:
            raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(pcm))
        if len(pcm) < PCM_SR * 2 * 0.3 or len(pcm) % 2:
            return web.json_response({"ok": True, "status": "empty", "text": ""})
        try:
            score = F.parse_score(float(request.query.get("score", "0")))
        except ValueError:
            score = 0.0
        try:
            text = await facehub.transcribe(pcm)
        except Exception as e:
            log.warning("face audio transcription failed: %s", e)
            text = ""
        if not text:
            return web.json_response({"ok": True, "status": "empty", "text": ""})
        return web.json_response({"ok": True, "status": facehub.heard(face, text, score), "text": text})

    @routes.get("/api/face/audio/{aid}")
    async def face_audio(request):
        face = facehub.auth(request)
        got = facehub.audio.get(request.match_info["aid"])
        if got is None or got[0] != face["id"] or got[2] < time.time():
            raise web.HTTPNotFound(text="gone")
        return web.Response(body=got[1], content_type="audio/wav", headers={"Cache-Control": "no-store"})

    # ---- this computer only (walterctl)
    @routes.get("/api/faces/status")
    async def faces_status(request):
        kiosk_only(request)
        return web.json_response(facehub.status())

    @routes.post("/api/faces/send")
    async def faces_send(request):
        """{face, kind: text|card|media|stop, text?, title?, body?, seconds?, url?, start?, source?}"""
        kiosk_only(request)
        d = await json_body(request, 8192)
        try:
            r = await facehub.send(str(d.get("face") or ""), str(d.get("kind") or ""), d)
        except KeyError as e:
            return web.json_response({"error": str(e.args[0])}, status=404)
        except LookupError as e:
            return web.json_response({"error": str(e)}, status=409)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(r)

    @routes.post("/api/faces/changed")
    async def faces_changed(request):
        kiosk_only(request)
        facehub.kick_forgotten()
        return web.json_response({"ok": True})

    # ---- the face page for a paired face (cookie)
    @routes.get("/face")
    async def face_root(request):
        raise web.HTTPFound("/face/" + (("?" + request.query_string) if request.query_string else ""))

    @routes.get("/face/ws")
    async def face_ws(request):
        face = facehub.page_face(request)
        if face is None:
            raise web.HTTPUnauthorized(text="pair this face first")
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        facehub.pages[ws] = face["id"]
        facehub.store.touch(face["id"], D.client_ip(request.remote, request.headers))
        try:
            if hub is not None:
                await ws.send_str(json.dumps({"t": "version", "v": hub.face_version}))
                for m in list(hub.last.values()):
                    if m.get("t") in PAGE_FORWARD:
                        await ws.send_str(json.dumps(m))
            async for m in ws:
                if m.type != WSMsgType.TEXT:
                    continue   # the page is a viewer: typed text, mute and sleep stay on the wall
        finally:
            facehub.pages.pop(ws, None)
        return ws

    @routes.get("/face/{path:.*}")
    async def face_file(request):
        path = request.match_info["path"] or "index.html"
        tok = request.query.get("token")
        if tok and path == "index.html":
            face = facehub.store.check(tok)
            ip = D.client_ip(request.remote, request.headers)
            if face is None:
                facehub.limiter.fail(ip)
                raise web.HTTPUnauthorized(text="that face link isn't valid any more: pair again")
            resp = web.Response(status=302, headers={"Location": f"/face/?face={face['id']}"})
            set_cookie(resp, tok, request)   # the token leaves the address bar
            return resp
        face = facehub.page_face(request)
        if face is None:
            if path == "index.html":
                raise web.HTTPFound("/face/pair")
            raise web.HTTPUnauthorized(text="pair this face first")
        fid = face["id"]
        if path == "index.html" and request.query.get("face") != fid:
            raise web.HTTPFound(f"/face/?face={fid}")
        if not FILE_RE.match(path) or ".." in path.split("/") or "//" in path:
            raise web.HTTPNotFound()
        name = path.rsplit("/", 1)[-1]
        if name in ("display.json", f"display-{fid}.json"):
            target = disp.face_display_path(fid)
            if not target.exists():
                return web.json_response({})
            return web.FileResponse(target, headers={"Cache-Control": "no-store"})
        if name in ("card.json", f"card-{fid}.json"):
            target = card_path(fid)
            if not target.exists():
                return web.json_response({})
            return web.FileResponse(target, headers={"Cache-Control": "no-store"})
        if (name in PAGE_DENY or name.endswith((".tmp", ".lock")) or name.startswith(".")
                or re.match(r"^(display|card)-.*\.json$", name)):
            raise web.HTTPNotFound()
        target = (face_dir / path).resolve()
        try:
            target.relative_to(face_dir.resolve())
        except ValueError:
            raise web.HTTPNotFound()
        if not target.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(target, headers={"Cache-Control": "no-store"})

    app.add_routes(routes)

    async def on_start(_app):
        facehub.watcher = asyncio.ensure_future(watch_faces(facehub))

    async def on_stop(_app):
        w = getattr(facehub, "watcher", None)
        if w is not None:
            w.cancel()
        if facehub.mdns:
            try:
                zc, info = facehub.mdns
                zc.unregister_service(info)
                zc.close()
            except Exception:
                pass

    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)


async def start_mdns(facehub: FaceHub, cfg):
    """Called by __main__ once the server listens (never in tests)."""
    if cfg.faces.enabled and cfg.faces.mdns:
        facehub.mdns = await asyncio.get_running_loop().run_in_executor(
            None, advertise, cfg.server.port, (facehub.store.get(F.WALL_ID) or {}).get("name", "Walter"))


async def watch_faces(facehub: FaceHub):
    """A forgotten face (walterctl face forget) loses its page and event stream."""
    mtime = None
    while True:
        await asyncio.sleep(3)
        try:
            p = facehub.store.path
            mt = p.stat().st_mtime_ns if p.exists() else 0
            if mt != mtime:
                if mtime is not None:
                    facehub.kick_forgotten()
                mtime = mt
        except Exception:
            log.exception("faces watch failed")
