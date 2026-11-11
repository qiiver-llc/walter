"""walter-face: the small agent for a face that should also listen and speak
(a laptop, a mini PC behind a kitchen tablet...). The SCREEN needs no Python:
it is the normal face page from the brain (/face/?face=<id>). This agent adds
the mic and the speaker (docs/FACES.md):

  walter-face pair http://BRAIN:8765 123456 [--name Kitchen] [--room kitchen] [--kind laptop]
  walter-face run [--brain URL] [--no-ears] [--no-speaker] [--screen]
  walter-face status | screen | forget

Ears: the same local speech stack as the wall (walter.ears.Ears: webrtcvad +
faster-whisper) when it is installed; finished turns go to the brain as text
with a mic score (faces.mic_score). Without whisper, a light energy detector
sends each utterance's audio to the brain, which transcribes it with its own
ears. Without a mic at all: speaker (and screen) only.
Speaker: the brain's replies arrive as events (SSE) with TTS audio, played with
pw-play / paplay / aplay / ffplay; the mic is deaf while it plays (no echo).
Media handoff ("play it in the garage") plays the stream with mpv.

Discovery: --brain URL, else the URL saved at pairing, else mDNS _walter._tcp
(python zeroconf, if installed).

The token lives in ~/.walter-face/face.json (mode 600) and is never printed or logged.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("walter.face_agent")
VERSION = "1"
HELLO_EVERY_S = 20
SR = 16000
URL_RE = re.compile(r"^https?://[^\s\"'<>]{3,2000}$")


def home() -> Path:
    return Path(os.environ.get("WALTER_FACE_HOME", Path.home() / ".walter-face"))


def state_path() -> Path:
    return home() / "face.json"


def load_state(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or state_path()).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(d: dict, path: Path | None = None):
    path = path or state_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, path)


def clean_brain(url: str) -> str:
    url = str(url or "").strip().rstrip("/")
    if not re.match(r"^https?://[A-Za-z0-9.\-\[\]:]+$", url):
        raise ValueError("the brain address looks like http://192.168.1.20:8765")
    return url


# ------------------------------------------------------------------ http
class Http:
    """Tiny JSON client for the brain's /api/face/* (stdlib only)."""

    def __init__(self, brain: str, token: str = "", timeout: float = 10):
        self.brain, self.token, self.timeout = brain.rstrip("/"), token, timeout

    def _req(self, method: str, path: str, body=None, ctype="application/json"):
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h = {"Content-Type": ctype} if data is not None else {}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return urllib.request.Request(self.brain + path, data=data, method=method, headers=h)

    def json(self, method: str, path: str, body=None, ctype="application/json"):
        try:
            with urllib.request.urlopen(self._req(method, path, body, ctype), timeout=self.timeout) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read() or b"{}").get("error") or e.reason
            except (ValueError, AttributeError):
                msg = e.reason
            raise RuntimeError(f"{e.code} {msg}") from None

    def raw(self, path: str) -> bytes:
        with urllib.request.urlopen(self._req("GET", path), timeout=self.timeout) as r:
            return r.read()

    def stream(self, path: str):
        """Lines of a server-sent event stream (blocking)."""
        return urllib.request.urlopen(self._req("GET", path), timeout=60)


def parse_sse(lines):
    """Yield (id, data dict) from an iterable of SSE byte/str lines; comments skipped."""
    eid, data = None, []
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if not line:
            if data:
                try:
                    msg = json.loads("\n".join(data))
                    if isinstance(msg, dict):
                        yield eid, msg
                except ValueError:
                    pass
            eid, data = None, []
        elif line.startswith(":"):
            continue
        elif line.startswith("id:"):
            eid = line[3:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())


# ------------------------------------------------------------------ discovery
def discover(timeout: float = 3.0) -> str | None:
    """http://host:port of a brain announcing _walter._tcp, or None (no zeroconf / none found)."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except Exception:
        log.info("mDNS: python zeroconf isn't installed here; use --brain http://<brain-ip>:8765")
        return None
    found: list[str] = []

    class L:
        def add_service(self, zc, typ, name):
            info = zc.get_service_info(typ, name, 2000)
            if info and info.parsed_addresses():
                found.append(f"http://{info.parsed_addresses()[0]}:{info.port}")

        def update_service(self, *a):
            pass

        def remove_service(self, *a):
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, "_walter._tcp.local.", L())
        t0 = time.time()
        while not found and time.time() - t0 < timeout:
            time.sleep(0.1)
    finally:
        zc.close()
    return found[0] if found else None


# ------------------------------------------------------------------ speaker
def find_player() -> list[str] | None:
    for cmd in (["pw-play"], ["paplay"], ["aplay", "-q"], ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]):
        if shutil.which(cmd[0]):
            return cmd
    return None


class Speaker:
    """Plays reply audio one sentence at a time; mutes the mic around it."""

    def __init__(self, on_busy=None, player: list[str] | None = None, run=subprocess.run):
        self.player = player if player is not None else find_player()
        self.on_busy = on_busy or (lambda busy: None)
        self.run = run
        self.gen = 0
        self.media: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def play_wav(self, wav: bytes, gen: int | None = None) -> bool:
        if not self.player or (gen is not None and gen != self.gen):
            return False
        with self._lock:
            if gen is not None and gen != self.gen:
                return False
            fd, path = tempfile.mkstemp(suffix=".wav", prefix="walter-face-")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(wav)
                self.on_busy(True)
                self.run([*self.player, path], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, timeout=120)
            except Exception as e:
                log.warning("couldn't play reply audio: %s", e)
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                time.sleep(0.3)   # the room's echo tail
                self.on_busy(False)
        return True

    def say_text(self, text: str):
        """No audio from the brain (it has no real voice): a local voice if one is installed."""
        for cmd in (["espeak-ng"], ["spd-say", "-w"], ["say"]):
            if shutil.which(cmd[0]):
                self.on_busy(True)
                try:
                    self.run([*cmd, text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
                except Exception:
                    pass
                finally:
                    self.on_busy(False)
                return
        print(f"Walter: {text}", flush=True)

    def stop(self):
        self.gen += 1
        self.stop_media()

    def play_media(self, url: str, start: float = 0.0, spawn=subprocess.Popen) -> bool:
        if not URL_RE.match(str(url or "")):
            return False
        mpv = shutil.which("mpv")
        if not mpv:
            log.warning("media handoff needs mpv on this face (sudo apt install mpv)")
            return False
        self.stop_media()
        args = [mpv, "--no-video", "--no-terminal", "--really-quiet", "--idle=no"]
        if start and start > 1:
            args.append(f"--start={float(start):.0f}")
        self.media = spawn([*args, "--", url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, start_new_session=True)
        return True

    def stop_media(self):
        if self.media is not None:
            try:
                self.media.terminate()
            except Exception:
                pass
            self.media = None


# ------------------------------------------------------------------ ears
class SimpleEars:
    """No whisper here: a light energy detector that sends each utterance's
    audio to the brain (which transcribes it with its own ears)."""

    def __init__(self, on_audio, device=None):
        self.on_audio, self.device = on_audio, device
        self.muted = threading.Event()
        self._running = False

    def start(self) -> bool:
        try:
            import numpy  # noqa: F401
            import sounddevice  # noqa: F401
        except Exception as e:
            log.info("no microphone support here (%s): speaker and screen only", e)
            return False
        self._running = True
        threading.Thread(target=self._run, name="face-ears", daemon=True).start()
        log.info("ears: simple mode (audio goes to the brain to transcribe)")
        return True

    def _run(self):
        import numpy as np
        import sounddevice as sd
        frame = int(SR * 0.03)
        floor, voiced, levels, silence = 300.0, [], [], 0
        with sd.RawInputStream(samplerate=SR, channels=1, dtype="int16", blocksize=frame, device=self.device) as st:
            while self._running:
                data, _ = st.read(frame)
                if self.muted.is_set():
                    voiced, levels, silence = [], [], 0
                    continue
                rms = float(np.sqrt(np.mean(np.frombuffer(data, np.int16).astype(np.float32) ** 2)) or 0)
                speech = rms > max(floor * 3, 400)
                if not voiced and not speech:
                    floor = 0.98 * floor + 0.02 * rms
                    continue
                voiced.append(bytes(data))
                if speech:
                    levels.append(min(1.0, rms / 4000))
                    silence = 0
                else:
                    silence += 1
                if silence > 27 or len(voiced) > 15 * 33:   # 0.8 s quiet, or 15 s
                    if len(levels) > 8:
                        self.on_audio(b"".join(voiced), list(levels), floor)
                    voiced, levels, silence = [], [], 0

    def stop(self):
        self._running = False


# ------------------------------------------------------------------ the agent
class Agent:
    def __init__(self, brain: str, token: str, ears: bool = True, speaker: bool = True,
                 http: Http | None = None, spk: Speaker | None = None):
        self.http = http or Http(brain, token)
        self.want_ears, self.want_speaker = ears, speaker
        self.spk = spk or Speaker(on_busy=self._busy)
        self.ears = None
        self.levels: list[float] = []
        self.scores: list[float] = []
        self.after = 0
        self.running = True
        self.loop: asyncio.AbstractEventLoop | None = None
        self.face: dict = {}

    # ---- capabilities
    def caps(self) -> dict:
        mq = round(sum(self.scores[-20:]) / len(self.scores[-20:]), 3) if self.scores else None
        d = {"ears": self.ears is not None, "speaker": bool(self.want_speaker and self.spk.player),
             "version": VERSION}
        if mq is not None:
            d["mic_quality"] = mq
        return d

    def hello(self) -> dict:
        r = self.http.json("POST", "/api/face/hello", self.caps())
        self.face = r.get("face") or self.face
        muted = bool((r.get("settings") or {}).get("muted"))
        um = getattr(self.ears, "user_muted", None)
        if um is not None:
            (um.set if muted else um.clear)()   # muted from the brain (walterctl / the face's settings)
        return r

    def _busy(self, on: bool):
        if self.ears is not None:
            (self.ears.muted.set if on else self.ears.muted.clear)()

    # ---- hearing
    def on_start(self):
        self.levels = []

    def on_level(self, v: float):
        if len(self.levels) < 2000:
            self.levels.append(v)

    def on_text(self, text: str):
        from .faces import mic_score
        score = mic_score(self.levels, float(getattr(self.ears, "_floor", 0.0) or 0.0))
        self.levels = []
        self.scores.append(score)
        threading.Thread(target=self._post_heard, args=(text, score), daemon=True).start()

    def _post_heard(self, text: str, score: float):
        try:
            r = self.http.json("POST", "/api/face/heard", {"text": text, "score": score})
            log.info("heard (%s, mic %.2f): %s", r.get("status"), score, text[:120])
        except Exception as e:
            log.warning("couldn't send what I heard: %s", e)

    def on_audio(self, pcm: bytes, levels: list[float], floor: float):
        from .faces import mic_score
        score = mic_score(levels, floor)
        self.scores.append(score)
        try:
            r = self.http.json("POST", f"/api/face/audio-turn?score={score}", pcm, "application/octet-stream")
            if r.get("text"):
                log.info("heard via the brain (%s, mic %.2f): %s", r.get("status"), score, r["text"][:120])
        except Exception as e:
            log.warning("couldn't send audio: %s", e)

    def start_ears(self, loop) -> bool:
        if not self.want_ears:
            return False
        try:
            from .config import load
            from .ears import Ears
            cfg = load(os.environ.get("WALTER_FACE_CONFIG") or (home() / "config.toml"))
            cfg.ears.live_partials = False   # light: finals only
            ears = Ears(cfg, self.on_text, self.on_level, self.on_start)
            if ears.start(loop):
                self.ears = ears
                log.info("ears: local whisper (%s)", cfg.ears.whisper_model)
                return True
        except Exception as e:
            log.info("local whisper ears unavailable (%s)", e)
        simple = SimpleEars(self.on_audio)
        if simple.start():
            simple.user_muted = threading.Event()
            self.ears = simple
            return True
        return False

    # ---- events
    def handle(self, msg: dict):
        t = msg.get("t")
        if t == "audio" and self.want_speaker:
            gen = self.spk.gen
            try:
                wav = self.http.raw(str(msg.get("url") or ""))
            except Exception as e:
                log.warning("couldn't fetch reply audio: %s", e)
                return
            self.spk.play_wav(wav, gen)
        elif t == "noaudio" and self.want_speaker:
            self.spk.say_text(str(msg.get("text") or ""))
        elif t == "say":
            print(f"Walter: {msg.get('text', '')}", flush=True)
        elif t == "media" and self.want_speaker:
            ok = self.spk.play_media(str(msg.get("url") or ""), float(msg.get("start") or 0))
            log.info("media handoff %s: %s", "playing" if ok else "refused", msg.get("title") or "")
        elif t == "stop":
            self.spk.stop()
        elif t == "card":
            print(f"[card] {msg.get('title', '')}: {msg.get('body', '')}", flush=True)
        elif t == "forgotten":
            log.warning("this face was forgotten on the brain: pair again (walter-face pair ...)")
            self.running = False

    def events_forever(self):
        while self.running:
            try:
                q = f"/api/face/events?after={self.after}" if self.after else "/api/face/events"
                with self.http.stream(q) as r:
                    for eid, msg in parse_sse(r):
                        if eid and eid.isdigit():
                            self.after = int(eid)
                        self.handle(msg)
                        if not self.running:
                            return
            except Exception as e:
                if not self.running:
                    return
                log.info("event stream dropped (%s); reconnecting", e)
                time.sleep(3)

    def hello_forever(self):
        while self.running:
            try:
                self.hello()
            except Exception as e:
                log.info("brain not answering (%s)", e)
            for _ in range(HELLO_EVERY_S * 10):
                if not self.running:
                    return
                time.sleep(0.1)

    async def run(self):
        self.loop = asyncio.get_running_loop()
        await self.loop.run_in_executor(None, self.start_ears, self.loop)
        if self.want_speaker and not self.spk.player:
            log.warning("no audio player found (pw-play, paplay, aplay or ffplay): replies print as text")
        try:
            r = await self.loop.run_in_executor(None, self.hello)
            log.info("joined %s as face %s", (r.get("brain") or {}).get("name", "Walter"), self.face.get("name"))
        except Exception as e:
            log.warning("first hello failed (%s); will keep trying", e)
        threading.Thread(target=self.hello_forever, daemon=True).start()
        await self.loop.run_in_executor(None, self.events_forever)


# ------------------------------------------------------------------ cli
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="walter-face", description="A Walter face's mic + speaker agent.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("pair", help="join a brain with the code on its screen")
    p.add_argument("brain")
    p.add_argument("code")
    p.add_argument("--name", default="")
    p.add_argument("--room", default="")
    p.add_argument("--kind", default="laptop", choices=["laptop", "tablet", "other"])
    r = sub.add_parser("run", help="listen and speak for this face")
    r.add_argument("--brain", default="")
    r.add_argument("--no-ears", action="store_true")
    r.add_argument("--no-speaker", action="store_true")
    r.add_argument("--screen", action="store_true", help="also open the face page in a browser")
    sub.add_parser("status")
    sub.add_parser("screen", help="open this face's page in the default browser")
    sub.add_parser("forget", help="delete this device's face token")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    st = load_state()

    if a.cmd == "pair":
        try:
            brain = clean_brain(a.brain)
            res = Http(brain).json("POST", "/api/face/pair", {"code": a.code, "name": a.name, "room": a.room,
                                                              "kind": a.kind})
        except (ValueError, RuntimeError, OSError) as e:
            print(f"walter-face: couldn't pair: {e}", file=sys.stderr)
            return 1
        save_state({"brain": brain, "token": res["token"], "face": res["face"]})
        print(f"paired as face {res['face']['name']} (id {res['face']['id']}). Start it: walter-face run")
        return 0
    if a.cmd == "status":
        if not st.get("token"):
            print("not paired (walter-face pair http://BRAIN:8765 CODE)")
            return 1
        try:
            r = Http(st["brain"], st["token"]).json("GET", "/api/face/settings")
            f = r["face"]
            print(f"face {f['name']} (room {f.get('room') or '-'}, {f['kind']}) on {st['brain']}: connected")
        except Exception as e:
            print(f"face {st.get('face', {}).get('name', '?')} on {st.get('brain')}: not reachable ({e})")
            return 1
        return 0
    if a.cmd == "forget":
        try:
            state_path().unlink()
            print("this device's face token is deleted (also run walterctl face forget NAME on the brain)")
        except OSError:
            print("nothing to forget")
        return 0
    if a.cmd in ("screen", "run") and not st.get("token"):
        print("walter-face: pair first: walter-face pair http://BRAIN:8765 CODE", file=sys.stderr)
        return 1
    brain = st.get("brain") or ""
    if a.cmd == "run" and a.brain:
        brain = clean_brain(a.brain)
    if not brain:
        brain = discover() or ""
    if not brain:
        print("walter-face: no brain address (use --brain http://<brain-ip>:8765)", file=sys.stderr)
        return 1
    if a.cmd == "screen" or (a.cmd == "run" and a.screen):
        import webbrowser
        # the token is in this one address only; the brain swaps it for a cookie and drops it
        webbrowser.open(f"{brain}/face/?token={urllib.parse.quote(st['token'])}")
        if a.cmd == "screen":
            return 0
    if a.cmd == "run":
        agent = Agent(brain, st["token"], ears=not a.no_ears, speaker=not a.no_speaker)
        try:
            asyncio.run(agent.run())
        except KeyboardInterrupt:
            pass
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
