"""Media in the wall browser: YouTube (and a DRM check for Amazon Music).

Playback happens in the live wall browser (home/skills/walter-browse + browse_live.py):
`walter-browse show URL` opens the panel, and a small fixed set of player actions
(pause/play/seek/volume/rate/next/info, YouTube shortcut keys) goes over the same
private unix socket as {"cmd": "media", "args": {...}}. No arbitrary JS from here.

State in ~/.walter/media.json (no secrets):
  {"active": bool, "service": "youtube", "query", "title", "url", "volume": 60, "duck_volume": 25}

The conductor calls :class:`Ducker` on every state change (like the podcast one):
listening/thinking -> volume to duck_volume % of his volume, speaking -> pause,
idle -> restore and resume if *we* paused it. It works in a background thread so a
busy browser never stalls Walter's state changes.
"""
from __future__ import annotations
from .security import clean_title  # noqa: E402

import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

DATA_DIR = Path(os.environ.get("WALTER_DATA", Path.home() / ".walter"))
STATE = DATA_DIR / "media.json"
SOCK = DATA_DIR / "browser-live.sock"
BROWSE = Path(os.environ.get("WALTER_BROWSE", DATA_DIR / "home" / "skills" / "walter-browse"))
DEFAULTS = {"active": False, "service": "", "query": "", "title": "", "url": "",
            "volume": 60, "duck_volume": 25}

USAGE = """walterctl media ... : video/music in the wall browser
  youtube play "search words" [--live] [--shorts]   best match (skips ads/shorts/live)
  youtube url URL                                    play a YouTube link
  pause | resume | toggle
  skip [N] | back [N]          seconds (default 10)
  seek MM:SS                   jump to a position
  volume N | volume up|down    0-100 (the page player)
  speed X                      0.25-3
  next                         next video (autoplay queue)
  fullscreen | theater         fill the panel (portrait wall) / theatre mode
  status                       what's playing + position
  stop                         stop the sound and close the browser
  amazon check                 can Amazon Music (Widevine DRM) play here?
  amazon play "artist or playlist"   (only if the check passes)"""


# ------------------------------------------------------------------ state
def load(path: Path | None = None) -> dict:
    try:
        st = json.loads((path or STATE).read_text())
    except Exception:  # noqa: BLE001
        st = {}
    return {**DEFAULTS, **st}


def save(st: dict, path: Path | None = None) -> None:
    path = path or STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    os.replace(tmp, path)


# ------------------------------------------------------------------ transport
def send(args: dict, timeout: float = 10, sock: Path | None = None) -> dict:
    """One media action to the live browser daemon."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(sock or SOCK))
        s.sendall((json.dumps({"cmd": "media", "args": args}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return json.loads(buf.decode() or "{}")


def browse(*argv: str, timeout: float = 70) -> tuple[int, str]:
    """Run walter-browse (show/goto/hide/quit/status) and return (code, output)."""
    try:
        p = subprocess.run([str(BROWSE), *argv], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "walter-browse timed out"
    return p.returncode, (p.stdout + p.stderr).strip()


# ------------------------------------------------------------------ pure helpers
def search_url(query: str, live: bool = False) -> str:
    q = urllib.parse.quote_plus(query.strip())
    # sp=EgIQAQ%3D%3D = "Type: Video" (keeps channels/playlists/most shorts out)
    return f"https://www.youtube.com/results?search_query={q}" + ("" if live else "&sp=EgIQAQ%253D%253D")


YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def video_id(url: str) -> str:
    """The 11-char id from watch?v=, youtu.be/, /shorts/, /live/ or /embed/ links ('' if none)."""
    u = urllib.parse.urlsplit(url.strip() if "://" in url else "https://" + url.strip())
    host = (u.hostname or "").lower()
    if host.endswith("youtu.be"):
        cand = u.path.strip("/").split("/")[0]
    elif host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        cand = urllib.parse.parse_qs(u.query).get("v", [""])[0]
        if not cand:
            parts = u.path.strip("/").split("/")
            cand = parts[1] if len(parts) > 1 and parts[0] in ("shorts", "live", "embed", "v") else ""
    else:
        return ""
    return cand if YT_ID.match(cand or "") else ""


def watch_url(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


OFFICIAL = re.compile(r"(vevo|official|- topic$|records|music)", re.I)
JUNK = re.compile(r"\b(reaction|reacts?|cover|karaoke|nightcore|8d audio|slowed|sped up|1 hour|10 hours?)\b", re.I)


def pick(results: list[dict], query: str, allow_live: bool = False, allow_shorts: bool = False) -> dict | None:
    """Best search result: relevance order, minus ads/shorts/live, nudged toward
    official/verified channels and away from reactions/covers the person didn't ask for."""
    qwords = set(re.findall(r"\w+", query.lower()))
    best, best_score = None, None
    for i, r in enumerate(results):
        href = r.get("href") or ""
        if r.get("ad") or not video_id("https://www.youtube.com" + href if href.startswith("/") else href):
            continue
        if "/shorts/" in href and not allow_shorts:
            continue
        if r.get("live") and not allow_live:
            continue
        title, ch = r.get("title", ""), r.get("channel", "")
        score = -i * 1.0
        if r.get("verified"):
            score += 1.5
        if OFFICIAL.search(ch) or "official" in title.lower():
            score += 1.5
        twords = set(re.findall(r"\w+", (title + " " + ch).lower()))
        score += 2.0 * len(qwords & twords) / max(1, len(qwords))
        junk = JUNK.search(title)
        if junk and junk.group(0).lower() not in query.lower():
            score -= 4
        if best_score is None or score > best_score:
            best, best_score = r, score
    return best


def parse_time(s: str) -> float:
    """'90', '1:30', '1:02:03' -> seconds."""
    parts = [float(x) for x in s.strip().split(":")]
    out = 0.0
    for x in parts:
        out = out * 60 + x
    return out


def fmt_time(sec: float) -> str:
    sec = int(sec or 0)
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def describe(info: dict) -> str:
    if not info or not info.get("found"):
        return "Nothing playing in the wall browser."
    state = "paused" if info.get("paused") else "playing"
    if info.get("ad"):
        state += " (an ad is on)"
    ch = f" ({info['channel']})" if info.get("channel") else ""
    pos = fmt_time(info.get("time", 0)) + (f" of {fmt_time(info['duration'])}" if info.get("duration") else "")
    return f"{info.get('title') or 'Untitled'}{ch}: {state}, {pos}, volume {info.get('volume', '?')}%" + (
        f", speed {info['rate']}x" if info.get("rate") not in (None, 1, 1.0) else "")


# ------------------------------------------------------------------ ducking
class Ducker:
    """Walter's state changes -> browser media ducking (same rules as podcast.Ducker).

    listening / thinking / working -> page volume to duck_volume % of his volume
    speaking -> pause (stay paused until Walter rests); idle/other -> restore, and
    resume only if *we* paused it. Actions run on one background thread in order.
    """
    DUCK = ("listening", "thinking", "working")
    PAUSE = ("speaking",)

    def __init__(self, sock: Path | None = None, state_path: Path | None = None, sender=None,
                 threaded: bool = True):
        self.sock = sock
        self.state_path = state_path
        self.mode = "full"
        self.we_paused = False
        self._send = sender or (lambda args: send(args, timeout=4, sock=self.sock))
        self._q: queue.Queue | None = queue.Queue() if threaded else None
        self._thread = None

    def _active(self) -> bool:
        return (self.sock or SOCK).exists() and load(self.state_path).get("active", False)

    def on_state(self, state: str):
        if not self._active():
            self.mode, self.we_paused = "full", False
            return
        want = ("pause" if state in self.PAUSE
                else "duck" if state in self.DUCK else "full")
        if self.mode == "pause" and want == "duck":
            want = "pause"
        if want == self.mode:
            return
        self.mode = want
        if self._q is None:
            self._apply(want)
            return
        self._q.put(want)
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="media-duck", daemon=True)
            self._thread.start()

    def _run(self):
        while True:
            try:
                want = self._q.get(timeout=30)
            except queue.Empty:
                return
            while not self._q.empty():        # only the latest state matters
                want = self._q.get_nowait()
            try:
                self._apply(want)
            except Exception:  # noqa: BLE001
                pass

    def _apply(self, want: str):
        st = load(self.state_path)
        full = int(st.get("volume", 60))
        ducked = round(full * st.get("duck_volume", 25) / 100.0)
        if want == "duck":
            self._send({"action": "volume", "value": ducked})
        elif want == "pause":
            info = self._send({"action": "info"}).get("info") or {}
            if info.get("found") and not info.get("paused"):
                self._send({"action": "pause"})
                self.we_paused = True
            self._send({"action": "volume", "value": ducked})
        else:
            self._send({"action": "volume", "value": full})
            if self.we_paused:
                self._send({"action": "play"})
            self.we_paused = False


# ------------------------------------------------------------------ CLI
def _act(action: str, value=None) -> dict:
    if not SOCK.exists():
        return {"ok": False, "error": "the wall browser isn't open"}
    try:
        return send({"action": action, **({"value": value} if value is not None else {})})
    except OSError as e:
        return {"ok": False, "error": f"the wall browser isn't answering ({e.__class__.__name__})"}


def _wait_video(seconds: float = 12) -> dict:
    end, info = time.time() + seconds, {}
    while time.time() < end:
        res = _act("info")
        info = res.get("info") or {}
        if info.get("found") and (info.get("duration") or info.get("time")):
            return info
        time.sleep(1)
    return info


def _start(url: str, st: dict, service: str, query: str) -> int:
    code, out = browse("show", url)
    if code != 0:
        print(f"Couldn't open the wall browser: {out}")
        return 1
    st.update(active=True, service=service, query=query, url=url)
    save(st)
    return 0


def _play_video(vid: str, st: dict, query: str, fullscreen: bool = True) -> int:
    if _start(watch_url(vid), st, "youtube", query):
        return 1
    info = _wait_video()
    _act("play")
    _act("volume", st["volume"])
    if info.get("ad"):                      # skippable pre-roll: try for a few seconds
        for _ in range(8):
            if not (_act("skipad").get("info") or {}).get("ad"):
                break
            time.sleep(1)
    if fullscreen:
        send({"action": "key", "key": "f"})  # fill the panel (portrait wall): f toggles
    info = (_act("info").get("info") or info)
    st["title"] = clean_title(info.get("title", ""))   # page text: one capped line, no tags
    save(st)
    print("Playing on the wall: " + describe(info))
    return 0


def youtube(argv: list[str]) -> int:
    if not argv:
        print(USAGE)
        return 2
    sub, rest = argv[0], argv[1:]
    flags = {x for x in rest if x.startswith("--")}
    words = " ".join(x for x in rest if not x.startswith("--")).strip()
    st = load()
    if sub == "url":
        vid = video_id(words)
        if not vid:
            print("That doesn't look like a YouTube video link.")
            return 2
        return _play_video(vid, st, words, "--no-fullscreen" not in flags)
    if sub in ("play", "search"):
        if not words:
            print('play what? walterctl media youtube play "search words"')
            return 2
        if video_id(words):
            return _play_video(video_id(words), st, words, "--no-fullscreen" not in flags)
        if _start(search_url(words, live="--live" in flags), st, "youtube", words):
            return 1
        results = []
        for _ in range(6):                   # results render a moment after load
            results = (_act("results").get("info") or [])
            if results:
                break
            time.sleep(1)
        best = pick(results, words, allow_live="--live" in flags, allow_shorts="--shorts" in flags)
        if not best:
            print(f"Couldn't find a video for '{words}' (the results page is on the wall).")
            return 1
        href = best["href"]
        vid = video_id("https://www.youtube.com" + href if href.startswith("/") else href)
        return _play_video(vid, st, words, "--no-fullscreen" not in flags)
    print(USAGE)
    return 2


def amazon(argv: list[str]) -> int:
    sub = argv[0] if argv else "check"
    if sub == "check":
        if not SOCK.exists():
            code, out = browse("show", "https://music.amazon.com")
            if code:
                print(f"Couldn't open the wall browser: {out}")
                return 1
        res = send({"action": "drm"}, timeout=10)
        info = res.get("info") or {}
        ok = info.get("com.widevine.alpha") is True
        print("Amazon Music can play here (Widevine DRM available)." if ok else
              "Amazon Music can't play here: this Chromium has no Widevine DRM module "
              f"(EME: {info.get('eme')}, widevine: {info.get('com.widevine.alpha')}). "
              "Its songs are DRM-protected, so the web player won't start. "
              "YouTube works; adding Google's Widevine CDM would need the owner's okay.")
        return 0 if ok else 1
    if sub == "play":
        res = _act("drm") if SOCK.exists() else {}
        if (res.get("info") or {}).get("com.widevine.alpha") is not True:
            print("Amazon Music can't play in this browser (no Widevine DRM). Try YouTube instead.")
            return 1
        words = " ".join(argv[1:]).strip()
        url = "https://music.amazon.com/search/" + urllib.parse.quote(words) if words else "https://music.amazon.com"
        st = load()
        if _start(url, st, "amazon", words):
            return 1
        print("Amazon Music search is on the wall; pick with walter-browse click (the owner logs in himself "
              "via walter-browse typing-page).")
        return 0
    print(USAGE)
    return 2


def cli(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd in ("youtube", "yt"):
        return youtube(rest)
    if cmd == "amazon":
        return amazon(rest)
    st = load()
    if cmd == "stop":
        if SOCK.exists():
            _act("stop")
            browse("quit", timeout=20)
        st.update(active=False, title="")
        save(st)
        print("Stopped and closed the wall browser.")
        return 0
    simple = {"pause": "pause", "resume": "play", "play": "play", "toggle": "toggle", "next": "next",
              "status": "info", "theater": "theater", "theatre": "theater", "skip-ad": "skipad"}
    if cmd in simple:
        res = _act(simple[cmd])
    elif cmd in ("fullscreen", "full"):
        try:
            res = send({"action": "key", "key": "f"}) if SOCK.exists() else _act("info")
        except OSError:
            res = _act("info")
    elif cmd in ("skip", "forward", "back", "rewind"):
        n = float(rest[0]) if rest else 10.0
        res = _act("seek", -n if cmd in ("back", "rewind") else n)
    elif cmd == "seek" and rest:
        res = _act("seekto", parse_time(rest[0]))
    elif cmd == "volume" and rest:
        cur = int(st.get("volume", 60))
        v = {"up": cur + 10, "down": cur - 10}.get(rest[0])
        v = max(0, min(100, int(v if v is not None else float(rest[0]))))
        st["volume"] = v
        save(st)
        res = _act("volume", v)
    elif cmd == "speed" and rest:
        res = _act("rate", float(rest[0].rstrip("x")))
    else:
        print(USAGE)
        return 2
    if not res.get("ok"):
        print(res.get("error", "That didn't work."))
        return 1
    print(describe(res.get("info") or {}))
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
