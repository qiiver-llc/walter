"""First-run wizard: blank box to a talking Walter in under 20 minutes.

Nine steps, shown big on the wall with a QR code so a phone can drive them:

  1 welcome    name your Walter + household name (kept on the box only)
  2 network    internet check, time zone, location (Open-Meteo city search)
  3 audio      speaker test + volume, mic level test ("Hey Walter"), device choice
  4 claude     Claude's own sign-in window or the owner's own API key, then verify
               (reuses walter.setup: Walter never sees Claude credentials)
  5 privacy    camera, cloud speech, face/voice recognition, admin opt-in (all off
               by default) and the worker permission mode (acceptEdits recommended)
  6 extras     optional: Google calendar connect, podcasts, sports teams,
               Home Assistant address (token via the private phone page)
  7 cloudlink  optional "Reach Walter from anywhere": paste (or scan) a Cloud Link
               invite code; walter/cloud_link.py claims it at the Cloud Link
               service and starts the private tunnel (the installer's --invite code
               is used when the box is left empty)
  8 pair       pair the phone app (6-digit code)
  9 hello      say hello + a short tour; finishing writes the done marker

When does it run? Only on a fresh install: deploy/install.sh writes
/etc/walter/firstrun/pending when it creates a new Walter. The wizard is active
while that file exists and neither /etc/walter/firstrun-done nor
/etc/walter/firstrun/done does. Existing installs (no pending file) never see it.

Progress lives in /etc/walter/firstrun/state.json (walter-owned folder), so a
reboot resumes at the same step. Every step can be skipped and revisited later
(from the companion app's Settings, paired devices only). Time from wizard start
to the first spoken hello is logged locally in ~/.walter/firstrun-metrics.jsonl.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Callable

from aiohttp import web

from . import security

log = logging.getLogger("walter.firstrun")

ETC_DIR = Path(os.environ.get("WALTER_ETC", "/etc/walter"))
STEPS = ("welcome", "network", "audio", "claude", "privacy", "extras", "cloudlink", "pair", "hello")
TITLES = {
    "welcome": "Welcome", "network": "Network, time and place", "audio": "Mic and speaker",
    "claude": "Connect Walter's brain", "privacy": "Privacy choices", "extras": "Extras (optional)",
    "cloudlink": "Reach Walter from anywhere", "pair": "Pair your phone", "hello": "Say hello",
}
REQUIRED = ("claude",)   # can be skipped, but the wall keeps a "brain not connected" note
PRIVACY_DEFAULTS = {"camera": False, "cloud_speech": False, "recognise_faces": False,
                    "recognise_voices": False, "full_admin": False, "permission_mode": "acceptEdits"}
PERMISSION_MODES = ("acceptEdits", "default", "bypassPermissions")
NAME_RE = re.compile(r"[^\w .'\-]", re.UNICODE)
TOUR = ["Say “Hey Walter” and then what you want: “what's the weather?”",
        "“Add milk to my shopping list.”",
        "“Remind me in 20 minutes to take the bread out.”",
        "“Focus on homework for 25 minutes.”",
        "Press Space to mute me; Z puts me to sleep."]


def clean(s, limit: int = 40) -> str:
    return NAME_RE.sub("", str(s or "")).strip()[:limit]


# ------------------------------------------------------------ state machine
class Wizard:
    """The step state machine. Pure: only reads/writes its JSON file."""

    def __init__(self, path: Path, now: Callable[[], float] = time.time):
        self.path = Path(path)
        self.now = now
        self.s = self._load()

    def _blank(self) -> dict:
        return {"version": 1, "started_at": None, "first_talk_at": None, "finished_at": None,
                "current": STEPS[0], "steps": {k: "pending" for k in STEPS}, "data": {}}

    def _load(self) -> dict:
        s = self._blank()
        try:
            d = json.loads(self.path.read_text())
            if isinstance(d, dict):
                for k in ("started_at", "first_talk_at", "finished_at"):
                    s[k] = d.get(k)
                if d.get("current") in STEPS:
                    s["current"] = d["current"]
                for k, v in (d.get("steps") or {}).items():
                    if k in STEPS and v in ("pending", "done", "skipped"):
                        s["steps"][k] = v
                if isinstance(d.get("data"), dict):
                    s["data"] = {k: v for k, v in d["data"].items() if k in STEPS and isinstance(v, dict)}
        except (OSError, ValueError):
            pass
        return s

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.s, f, indent=1)
        os.replace(tmp, self.path)

    # -- queries
    @property
    def current(self) -> str:
        return self.s["current"]

    @property
    def finished(self) -> bool:
        return bool(self.s["finished_at"])

    def status(self, step: str) -> str:
        return self.s["steps"][step]

    def data(self, step: str) -> dict:
        return self.s["data"].get(step, {})

    def next_open(self, after: str | None = None) -> str | None:
        """First pending step after `after` (wrapping round), or None if all are settled."""
        order = list(STEPS)
        if after in order:
            i = order.index(after) + 1
            order = order[i:] + order[:i]
        return next((k for k in order if self.s["steps"][k] == "pending"), None)

    def view(self) -> dict:
        return {"current": self.current, "finished": self.finished,
                "steps": [{"id": k, "n": i + 1, "title": TITLES[k], "status": self.s["steps"][k]}
                          for i, k in enumerate(STEPS)],
                "data": self.s["data"], "started_at": self.s["started_at"],
                "first_talk_at": self.s["first_talk_at"]}

    # -- transitions
    def _check(self, step: str):
        if step not in STEPS:
            raise ValueError(f"unknown step: {step}")

    def start(self):
        if not self.s["started_at"]:
            self.s["started_at"] = self.now()
            self.save()

    def update(self, step: str, data: dict):
        """Merge data into a step without completing it."""
        self._check(step)
        self.s["data"].setdefault(step, {}).update(data or {})
        self.save()

    def _advance(self, step: str):
        nxt = self.next_open(step)
        self.s["current"] = nxt or "hello"

    def complete(self, step: str, data: dict | None = None):
        self._check(step)
        if data:
            self.s["data"].setdefault(step, {}).update(data)
        self.s["steps"][step] = "done"
        self._advance(step)
        self.save()

    def skip(self, step: str):
        self._check(step)
        if self.s["steps"][step] != "done":
            self.s["steps"][step] = "skipped"
        self._advance(step)
        self.save()

    def goto(self, step: str):
        """Revisit any step (done or skipped ones too)."""
        self._check(step)
        self.s["current"] = step
        self.save()

    def first_talk(self) -> float | None:
        """Mark the first spoken exchange; returns seconds since start (once)."""
        if self.s["first_talk_at"] or not self.s["started_at"]:
            return None
        self.s["first_talk_at"] = self.now()
        self.save()
        return self.s["first_talk_at"] - self.s["started_at"]

    def finish(self):
        self.s["steps"]["hello"] = "done"
        self.s["current"] = "hello"
        self.s["finished_at"] = self.now()
        self.save()


# ------------------------------------------------------------ the service
class FirstRun:
    def __init__(self, cfg, send: Callable[[dict], None] = lambda m: None, setup=None,
                 etc: Path | None = None, data_dir: Path | None = None,
                 apply_config: Callable[[str, object], None] | None = None,
                 run: Callable | None = None):
        from .config import DATA_DIR
        self.cfg = cfg
        self.send = send
        self.setup = setup                 # walter.setup.Setup (Claude sign-in, time zone)
        self.etc = Path(etc or ETC_DIR)
        self.dir = self.etc / "firstrun"
        self.data_dir = Path(data_dir or DATA_DIR)
        self.apply_config = apply_config or self._apply_config
        self.run = run or _run
        self.key = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(10))
        self.wizard = Wizard(self.dir / "state.json")
        self.pair_store = None             # devices.DeviceStore, set by the app server
        self.cloud_link = None             # () -> cloud_link.CloudLink (tests swap it)
        self.live = None                   # live.Live: config changes apply without a restart

    # -- is it on?
    def done_marked(self) -> bool:
        return (self.etc / "firstrun-done").exists() or (self.dir / "done").exists()

    @property
    def active(self) -> bool:
        return (self.dir / "pending").exists() and not self.done_marked()

    def phone_url(self) -> str:
        from .setup import lan_ip
        base = (getattr(getattr(self.cfg, "app", None), "public_url", "") or "").rstrip("/")
        if base.lower().endswith(".wallter.app"):
            base = ""      # a Cloud Link address carries only /app and /api/app/*, not /firstrun
        if not base:
            base = f"http://{lan_ip() or 'walter.local'}:{self.cfg.server.port}"
        return f"{base}/firstrun?k={self.key}"

    def announce(self, **extra):
        if not self.active:
            self.send({"t": "firstrun", "active": False})
            return
        v = self.wizard.view()
        v.update(t="firstrun", active=True, phone_url=self.phone_url(), tour=TOUR, **extra)
        self.send(v)

    def start(self) -> bool:
        if not self.active:
            return False
        self.wizard.start()
        self.announce()
        log.info("first-run wizard active at step %s; phone page %s", self.wizard.current, self.phone_url())
        return True

    # -- hooks
    def on_heard(self, text: str):
        """Called with any heard turn while the wizard runs: the first one is time-to-first-talk."""
        if not self.active:
            return
        secs = self.wizard.first_talk()
        if secs is not None:
            self.metric("first_talk", seconds=round(secs, 1))
        if self.wizard.current == "audio" and "walter" in (text or "").lower():
            self.wizard.update("audio", {"heard_wake": True})
            self.announce()

    def metric(self, event: str, **kw):
        rec = {"t": time.time(), "event": event, **kw}
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.data_dir / "firstrun-metrics.jsonl", "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError as e:
            log.warning("couldn't log first-run metric: %s", e)

    def _apply_config(self, key: str, value):
        from . import live
        live.set_in_file(self.data_dir / "config.toml", key, json.dumps(value) if not isinstance(value, str) else value)

    # -- actions
    async def handle(self, action: str, d: dict) -> dict:
        w = self.wizard
        step = d.get("step") or w.current
        data = d.get("data") if isinstance(d.get("data"), dict) else {}
        if action == "state":
            return {"ok": True, **w.view(), "tour": TOUR}
        if action == "skip":
            w.skip(step)
            self.metric("skip", step=step)
            return self._ok()
        if action == "goto":
            w.goto(step)
            return self._ok()
        fn = getattr(self, f"_do_{action}", None)
        if fn is None:
            raise web.HTTPNotFound(text="unknown first-run action")
        return await fn(data)

    def _ok(self, **extra) -> dict:
        self.announce()
        return {"ok": True, **self.wizard.view(), **extra}

    def _fail(self, message: str) -> dict:
        return {"ok": False, "message": message, **self.wizard.view()}

    # 1 welcome
    async def _do_welcome(self, d):
        name = clean(d.get("walter_name")) or "Walter"
        house = clean(d.get("household"), 60)
        self.wizard.complete("welcome", {"walter_name": name, "household": house})
        if name.lower() != "walter":
            words = list(dict.fromkeys([name.lower(), "walter"]))
            try:
                self.apply_config("ears.wake_words", words)
            except Exception as e:
                log.warning("couldn't set wake words: %s", e)
        return self._ok()

    # 2 network, time zone, location
    async def _do_netcheck(self, d):
        online = await self.run("curl", "-fs", "--max-time", "6", "-o", "/dev/null", "http://deb.debian.org/debian/")
        from .setup import lan_ip
        res = {"online": online == 0, "lan_ip": lan_ip(), "checked_at": time.time()}
        self.wizard.update("network", res)
        return self._ok(check=res)

    async def _do_geocode(self, d):
        q = str(d.get("q") or "").strip()[:80]
        if len(q) < 2:
            return self._fail("Type at least two letters of your town.")
        return {"ok": True, "results": await geocode(q)}

    async def _do_network(self, d):
        out = {}
        tz = str(d.get("tz") or "")
        if tz:
            ok = await self.setup.set_timezone(tz) if self.setup else False
            if not ok:
                return self._fail("That time zone wasn't recognised.")
            out["tz"] = tz
        place = d.get("place") if isinstance(d.get("place"), dict) else None
        if place:
            try:
                lat, lon = float(place["lat"]), float(place["lon"])
                assert -90 <= lat <= 90 and -180 <= lon <= 180
            except (KeyError, TypeError, ValueError, AssertionError):
                return self._fail("That place didn't look right; search again.")
            out["place"] = {"name": clean(place.get("name"), 80), "lat": round(lat, 3), "lon": round(lon, 3)}
            for k, v in (("dashboard.lat", out["place"]["lat"]), ("dashboard.lon", out["place"]["lon"])):
                try:
                    self.apply_config(k, v)
                except Exception as e:
                    log.warning("couldn't save %s: %s", k, e)
        self.wizard.complete("network", out)
        return self._ok()

    # 3 audio
    async def _do_devices(self, d):
        return {"ok": True, **await audio_devices(self.run)}

    async def _do_speaker(self, d):
        """Set the volume / output device and play a test chime."""
        sink = str(d.get("sink") or "")
        if sink and re.fullmatch(r"[\w.\-:]{1,200}", sink):
            await self.run("pactl", "set-default-sink", sink)
        vol = d.get("volume")
        if isinstance(vol, (int, float)) and 0 <= vol <= 150:
            await self.run("pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{int(vol)}%")
        chime = _chime_file()
        if chime:
            await self.run("paplay", str(chime))
        self.wizard.update("audio", {k: v for k, v in (("sink", sink), ("volume", vol)) if v not in ("", None)})
        return self._ok()

    async def _do_mic(self, d):
        """Pick the input device, then record ~3 s and report the loudest level (0-100)."""
        source = str(d.get("source") or "")
        if source and re.fullmatch(r"[\w.\-:]{1,200}", source):
            await self.run("pactl", "set-default-source", source)
        level = await mic_level(3.0)
        passed = level is not None and level >= 8
        self.wizard.update("audio", {"mic_level": level, "mic_ok": passed,
                                     **({"source": source} if source else {})})
        return self._ok(level=level, passed=passed,
                        message=("Heard you loud and clear." if passed else
                                 "I couldn't hear much. Move closer, check the mic isn't muted, or pick another mic."))

    async def _do_audio(self, d):
        a = self.wizard.data("audio")
        heard = bool(d.get("speaker_ok"))
        self.wizard.complete("audio", {"speaker_ok": heard, "passed": bool(heard and a.get("mic_ok"))})
        return self._ok()

    # 4 claude (credentials never pass through here)
    async def _do_signin(self, d):
        if not self.setup:
            return self._fail("Sign-in isn't available right now.")
        ok, msg = await self.setup.open_signin()
        return {"ok": ok, "message": msg, **self.wizard.view()}

    async def _do_apikey(self, d):
        if not self.setup:
            return self._fail("Sign-in isn't available right now.")
        ok, msg = await self.setup.save_api_key(str(d.get("key") or ""))
        return {"ok": ok, "message": msg, **self.wizard.view()}

    async def _do_claude(self, d):
        if not self.setup:
            return self._fail("Sign-in isn't available right now.")
        ok, msg = await self.setup.verify()
        if not ok:
            return self._fail(msg)
        self.wizard.complete("claude", {"verified": True})
        if self.setup.needed:
            asyncio.ensure_future(self.setup.finish())   # wakes the brain (no admin request here)
        return self._ok(message="Signed in.")

    # 5 privacy
    async def _do_privacy(self, d):
        c = {k: (d.get(k) is True if isinstance(v, bool) else v) for k, v in PRIVACY_DEFAULTS.items()}
        pm = d.get("permission_mode")
        c["permission_mode"] = pm if pm in PERMISSION_MODES else "acceptEdits"
        sets = [("senses.camera", c["camera"]), ("senses.recognise_faces", c["recognise_faces"]),
                ("senses.recognise_voices", c["recognise_voices"]), ("brain.permission_mode", c["permission_mode"]),
                # the deny list binds in every mode; sudo rules drop only with the admin opt-in
                ("brain.disallowed_tools", security.deny_list(admin=c["full_admin"]))]
        for k, v in sets:
            try:
                self.apply_config(k, v)
            except Exception as e:
                log.warning("couldn't save %s: %s", k, e)
        mode = "openai" if c["cloud_speech"] else "local"
        _write_speech_choice(self.data_dir, mode)
        if self.setup:
            # Only now, during first run, and only with an explicit tick. An empty
            # request still closes the door, as in the old setup page.
            self.setup.request_admin_password("", full_admin=c["full_admin"])
        self.wizard.complete("privacy", c)
        return self._ok()

    # 6 extras
    async def _do_extras(self, d):
        out = {}
        if d.get("google"):
            await self.run(str(_repo() / "bin" / "walterctl"), "google", "connect")
            out["google"] = "started"
        pods = [clean(p, 80) for p in (d.get("podcasts") or []) if clean(p, 80)][:5]
        for p in pods:
            await self.run(str(_repo() / "bin" / "walterctl"), "podcast", "follow", p)
        if pods:
            out["podcasts"] = pods
        teams = [clean(t, 60) for t in (d.get("teams") or []) if clean(t, 60)][:8]
        if teams:
            out["teams"] = teams
        if d.get("verse") in (True, False):
            out["verse"] = bool(d["verse"])
        ha = str(d.get("homeassistant_url") or "").strip().rstrip("/")[:200]
        if re.fullmatch(r"https?://[\w.\-]+(?::\d{1,5})?", ha):
            # optional: only the address here; the token comes via the private phone page
            self.apply_config("homeassistant.url", ha)
            await self.run(str(_repo() / "bin" / "walterctl"), "ha", "connect")
            out["homeassistant"] = ha
        self.wizard.complete("extras", out)
        return self._ok()

    # 7 reach Walter from anywhere (Cloud Link, walter/cloud_link.py)
    def _cloud_link(self):
        if self.cloud_link is not None:
            return self.cloud_link()
        from . import cloud_link as CL
        def set_url(url):
            if self.live is not None:
                self.live.set("app.public_url", url)
            else:
                self.apply_config("app.public_url", url)
        return CL.CloudLink.from_config(self.cfg, get_public_url=lambda: self.cfg.app.public_url or "",
                                        set_public_url=set_url)

    async def _do_cloudlink(self, d):
        from . import cloud_link as CL
        if d.get("want") is False:
            self.wizard.skip("cloudlink")
            return self._ok()
        code = CL.clean_code(d.get("code"))
        if not code:
            try:
                code = CL.clean_code((self.etc / "invite").read_text())
            except OSError:
                code = ""
        if not code:
            return self._fail("Type or scan your invite code first (or tap Skip).")
        try:
            r = await self._cloud_link().enable(code, replace=d.get("replace") is True)
        except CL.CloudLinkError as e:
            return self._fail(str(e))
        self.wizard.complete("cloudlink", {"enabled": True, "host": r["host"]})
        self.metric("cloudlink", healthy=bool(r.get("healthy")))
        msg = f"Done! Walter's app now works from anywhere at {r['url']}/app"
        if not r.get("healthy"):
            msg += " (the first connection can take a minute or two)"
        return self._ok(message=msg + ".", url=r["url"])

    # 8 pair
    async def _do_paircode(self, d):
        if self.pair_store is None:
            return self._fail("The phone app isn't enabled on this Walter.")
        p = self.pair_store.start_pairing()
        # the app itself goes through any public address (Cloud Link carries /app)
        base = ((getattr(getattr(self.cfg, "app", None), "public_url", "") or "").rstrip("/")
                or self.phone_url().split("/firstrun")[0])
        link = f"{base}/app/#pair={p['code']}"
        self.wizard.goto("pair")
        self.announce(pair={"code": p["code"], "link": link})   # big code + QR on the wall
        return {"ok": True, "code": p["code"], "expires": p["expires"], "link": link, **self.wizard.view()}

    async def _do_pair(self, d):
        self.wizard.complete("pair", {"paired": bool(d.get("paired", True))})
        return self._ok()

    # 9 hello
    async def _do_hello(self, d):
        w = self.wizard
        w.finish()
        total = (w.s["finished_at"] - w.s["started_at"]) if w.s["started_at"] else None
        ttft = (w.s["first_talk_at"] - w.s["started_at"]) if w.s["first_talk_at"] and w.s["started_at"] else None
        self.metric("finished", total_s=total and round(total, 1), first_talk_s=ttft and round(ttft, 1),
                    skipped=[k for k in STEPS if w.status(k) == "skipped"])
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / "done").write_text(time.strftime("%Y-%m-%dT%H:%M:%S\n"))
        except OSError as e:
            log.warning("couldn't write the first-run done marker: %s", e)
        self.send({"t": "emotion", "name": "excited", "hold": 6})
        self.send({"t": "action", "name": "bounce"})
        return self._ok(total_s=total, first_talk_s=ttft)


# ------------------------------------------------------------ helpers
def _repo() -> Path:
    from .config import REPO_DIR
    return REPO_DIR


def _chime_file() -> Path | None:
    for p in (_repo() / "face" / "sounds" / "chime.wav",
              Path("/usr/share/sounds/freedesktop/stereo/complete.oga"),
              Path("/usr/share/sounds/alsa/Front_Center.wav")):
        if p.exists():
            return p
    return None


def _write_speech_choice(data_dir: Path, mode: str):
    """Same file walterctl voice/ears use (~/.walter/speech.json)."""
    p = Path(data_dir) / "speech.json"
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError):
        d = {}
    d["tts"] = d["stt"] = mode
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d))


async def _run(*argv, timeout: float = 30) -> int:
    if not shutil.which(argv[0]) and not Path(argv[0]).exists():
        return 127
    try:
        p = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.DEVNULL,
                                                 stdout=asyncio.subprocess.DEVNULL,
                                                 stderr=asyncio.subprocess.DEVNULL)
        return await asyncio.wait_for(p.wait(), timeout)
    except Exception as e:
        log.warning("%s failed: %s", argv[0], type(e).__name__)
        return 1


async def _out(*argv, timeout: float = 10) -> str:
    if not shutil.which(argv[0]):
        return ""
    try:
        p = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                 stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(p.communicate(), timeout)
        return out.decode(errors="replace")
    except Exception:
        return ""


async def audio_devices(run=None) -> dict:
    """Speakers and mics from PipeWire/Pulse (`pactl -f json`)."""
    out = {"sinks": [], "sources": []}
    for kind in ("sinks", "sources"):
        try:
            items = json.loads(await _out("pactl", "-f", "json", "list", kind) or "[]")
        except ValueError:
            items = []
        for it in items:
            name = it.get("name", "")
            if kind == "sources" and name.endswith(".monitor"):
                continue
            out[kind].append({"name": name, "label": it.get("description") or name})
    return out


async def mic_level(seconds: float = 3.0) -> int | None:
    """Loudest short-window level (0-100) over a few seconds from the default mic."""
    if not shutil.which("parec"):
        return None
    try:
        p = await asyncio.create_subprocess_exec(
            "parec", "--raw", "--format=s16le", "--rate=16000", "--channels=1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        buf = b""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                buf += await asyncio.wait_for(p.stdout.read(3200), max(0.05, end - time.monotonic()))
            except asyncio.TimeoutError:
                break
        p.kill()
        await p.wait()
    except Exception:
        return None
    return level_of(buf)


def level_of(pcm: bytes) -> int:
    """0-100 from s16le PCM: loudest 100 ms RMS on a dBFS scale (-60 dB = 0)."""
    import array
    import math
    a = array.array("h")
    a.frombytes(pcm[: len(pcm) // 2 * 2])
    best = 0.0
    for i in range(0, len(a), 1600):
        w = a[i:i + 1600]
        if w:
            best = max(best, math.sqrt(sum(x * x for x in w) / len(w)))
    if best <= 0:
        return 0
    db = 20 * math.log10(best / 32768)
    return max(0, min(100, int(round((db + 60) / 60 * 100))))


async def geocode(q: str) -> list[dict]:
    """City search via Open-Meteo's free geocoding (nothing about the household is sent)."""
    import aiohttp
    url = "https://geocoding-api.open-meteo.com/v1/search"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.get(url, params={"name": q, "count": 6, "language": "en", "format": "json"}) as r:
                d = await r.json()
    except Exception as e:
        log.warning("geocode failed: %s", type(e).__name__)
        return []
    out = []
    for x in d.get("results") or []:
        label = ", ".join(v for v in (x.get("name"), x.get("admin1"), x.get("country")) if v)
        out.append({"name": label, "lat": x.get("latitude"), "lon": x.get("longitude"),
                    "tz": x.get("timezone")})
    return out


# ------------------------------------------------------------ routes
def add_routes(app: web.Application, fr: FirstRun, apphub=None):
    """/firstrun (phone page) and /api/firstrun/*.

    Auth: while the wizard is active, the key shown in the wall's QR code (or the
    wall itself, from loopback); at any time, a paired companion-app device (so
    every step can be revisited from the app's Settings)."""
    from . import devices as D
    from .config import REPO_DIR
    routes = web.RouteTableDef()
    if apphub is not None:
        fr.pair_store = apphub.store

    def allowed(request, key: str | None) -> bool:
        if fr.active and (secrets.compare_digest(key or "", fr.key) or
                          (D.is_kiosk(request.remote, request.headers) and not request.headers.get("Authorization"))):
            return True
        if apphub is not None and request.headers.get("Authorization"):
            apphub.auth(request)   # raises 401/429
            return True
        return False

    @routes.get("/firstrun")
    async def page(request):
        # ?app=1: opened from the paired app's Settings. The page holds no secret;
        # its API calls carry the app's own device token instead of the key.
        from_app = request.query.get("app") == "1" and apphub is not None
        if not from_app and (not fr.active or not secrets.compare_digest(request.query.get("k", ""), fr.key)):
            return web.Response(status=403, text="Scan the QR code on Walter's screen.")
        html = (REPO_DIR / "face" / "firstrun-phone.html").read_text()
        return web.Response(text=html.replace("{{KEY}}", "" if from_app else fr.key), content_type="text/html",
                            headers={"Cache-Control": "no-store"})

    @routes.get("/api/firstrun/qr.svg")
    async def qr(request):
        from .setup import qr_svg
        if not fr.active or not D.is_kiosk(request.remote, request.headers):
            raise web.HTTPForbidden()
        which = request.query.get("which")
        data = fr.phone_url() if which != "pair" else request.query.get("link", "")
        if which == "pair" and "/app/#pair=" not in data:
            raise web.HTTPBadRequest()
        return web.Response(text=qr_svg(data), content_type="image/svg+xml")

    @routes.post("/api/firstrun/{action}")
    async def api(request):
        raw = await request.content.read(16_385)
        if len(raw) > 16_384:
            raise web.HTTPRequestEntityTooLarge(max_size=16_384, actual_size=len(raw))
        try:
            d = json.loads(raw or b"{}")
        except ValueError:
            d = {}
        if not isinstance(d, dict):
            d = {}
        if not allowed(request, d.get("k")):
            raise web.HTTPForbidden(text="first run is over; use the paired app")
        try:
            return web.json_response(await fr.handle(request.match_info["action"], d))
        except ValueError as e:
            return web.json_response({"ok": False, "message": str(e)}, status=400)

    app.add_routes(routes)
