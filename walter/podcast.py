"""Podcasts: follow shows by public RSS feed and play them through mpv.

Apple doesn't expose a listener's library, so shows are resolved with the free
iTunes Search API and episodes come straight from each show's RSS feed.

State lives in ~/.walter/podcasts.json (no secrets):
  {"shows": [{"title", "feed", "author", "itunes_id"}],
   "episodes": {guid: {"show", "title", "pos", "dur", "played", "url"}},
   "now": {guid, show, title, url, started} | null,
   "volume": 100, "duck_volume": 25, "speed": 1.0}

Playback is a detached mpv (``--input-ipc-server``) so pause/seek/speed work
from any process, plus a small watcher process that saves the position every
few seconds and marks an episode played near the end.

The conductor calls :class:`Ducker` on every state change so the podcast
ducks while Walter listens/thinks and pauses while he talks (the echo
canceller is off, so otherwise the mic would hear the podcast).
"""
from __future__ import annotations
from .security import clean_title  # noqa: E402

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path

DATA_DIR = Path(os.environ.get("WALTER_DATA", Path.home() / ".walter"))
STATE = DATA_DIR / "podcasts.json"
CACHE_DIR = DATA_DIR / "podcast-cache"
SOCK = DATA_DIR / "podcast-mpv.sock"
# now-playing for the face (dashboard.js polls it): written ~1/s by the watcher while mpv runs
NOW_FILE = Path(__file__).resolve().parent.parent / "face" / "dashboard-podcast.json"
# up-next list for the face (sibling file, so the 1/s now-playing writes never clobber it);
# written by the dashboard timer (walter-dashboard refresh) and after play/stop
UPNEXT_FILE = NOW_FILE.with_name("dashboard-podcast-upnext.json")
# cover art: fetched once, squared to ART_PX JPEG in ART_DIR, copied beside the face so the wall
# serves it itself (never hotlinked); the face uses "podcast-art/<file>"
ART_SUB = "art"             # under CACHE_DIR
FACE_ART = "podcast-art"    # beside the up-next file (face/)
ART_PX = 300
ART_RETRY_S = 86400         # a failed fetch/convert is retried after a day
CACHE_TTL = 30 * 60
NEW_DAYS = 7
PLAYED_TAIL_S = 90          # within this many seconds of the end = played
PLAYED_FRAC = 0.97
UA = "Walter-podcasts/1.0"

ITUNES = "https://itunes.apple.com/search"
NS_ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


# ------------------------------------------------------------------ state
def load(path: Path | None = None) -> dict:
    path = path or STATE
    try:
        st = json.loads(path.read_text())
    except (OSError, ValueError):
        st = {}
    st.setdefault("shows", [])
    st.setdefault("episodes", {})
    st.setdefault("now", None)
    st.setdefault("volume", 100)
    st.setdefault("duck_volume", 25)
    st.setdefault("speed", 1.0)
    return st


def save(st: dict, path: Path | None = None):
    path = path or STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(path)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (s or "").lower()).strip()


def find_show(st: dict, words: str | None) -> dict | None:
    """Best followed show for spoken words ('morning news'); None = no match."""
    shows = st["shows"]
    if not shows:
        return None
    if not words:
        return shows[0] if len(shows) == 1 else None
    w = _norm(words)
    for s in shows:
        if _norm(s["title"]) == w:
            return s
    toks = [t for t in w.split() if t not in ("the", "podcast", "show", "with", "a")]
    best, score = None, 0
    for s in shows:
        title = _norm(s["title"])
        n = sum(1 for t in toks if t in title.split())
        if n > score:
            best, score = s, n
    return best


# ------------------------------------------------------------------ network
def _get(url: str, timeout=15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def search(term: str, fetch=_get) -> list[dict]:
    q = urllib.parse.urlencode({"term": term, "media": "podcast", "entity": "podcast", "limit": 10})
    data = json.loads(fetch(f"{ITUNES}?{q}"))
    out = []
    for r in data.get("results", []):
        if r.get("feedUrl"):
            out.append({"title": clean_title(r.get("collectionName") or r.get("trackName", "")),
                        "feed": r["feedUrl"], "author": r.get("artistName", ""),
                        "itunes_id": r.get("collectionId")})
    return out


def pick_result(term: str, results: list[dict]) -> dict | None:
    """Prefer an exact / containing title match over iTunes' own ranking."""
    if not results:
        return None
    t = _norm(term)
    for r in results:
        if _norm(r["title"]) == t:
            return r
    for r in results:
        if t in _norm(r["title"]) or _norm(r["title"]) in t:
            return r
    return results[0]


def _img(el) -> str:
    """itunes:image href (or RSS <image><url>) of a channel/item element, else ''."""
    if el is None:
        return ""
    i = el.find(f"{NS_ITUNES}image")
    if i is not None and (i.get("href") or "").strip():
        return i.get("href").strip()
    return (el.findtext("image/url") or "").strip()


def parse_feed(xml_bytes: bytes) -> list[dict]:
    """Episodes newest first: {guid, title, url, date, dur, img} (img: episode art, else the show's)."""
    root = ET.fromstring(xml_bytes)
    show_img = _img(root.find("channel"))
    eps = []
    for it in root.iter("item"):
        enc = it.find("enclosure")
        if enc is None or not enc.get("url"):
            continue
        title = clean_title(it.findtext("title") or "")   # feed text: one capped line, no tags
        guid = (it.findtext("guid") or enc.get("url")).strip()
        ts = 0.0
        with_date = it.findtext("pubDate")
        if with_date:
            try:
                ts = parsedate_to_datetime(with_date.strip()).timestamp()
            except (TypeError, ValueError):
                ts = 0.0
        eps.append({"guid": guid, "title": title, "url": enc.get("url"),
                    "date": ts, "dur": _dur(it.findtext(f"{NS_ITUNES}duration")),
                    "img": _img(it) or show_img})
    eps.sort(key=lambda e: e["date"], reverse=True)
    return eps


def _dur(s: str | None) -> float:
    if not s:
        return 0.0
    try:
        parts = [float(p) for p in s.strip().split(":")]
    except ValueError:
        return 0.0
    tot = 0.0
    for p in parts:
        tot = tot * 60 + p
    return tot


def episodes(show: dict, fetch=_get, cache_dir: Path | None = None, now=None,
             cache_only=False) -> list[dict]:
    """Newest first. cache_only: read the cached feed however old (no network; [] if none)."""
    cache_dir = cache_dir or CACHE_DIR
    now = time.time() if now is None else now
    key = re.sub(r"[^A-Za-z0-9]+", "_", show["feed"])[-120:]
    f = cache_dir / f"{key}.xml"
    data = None
    try:
        if cache_only or now - f.stat().st_mtime < CACHE_TTL:
            data = f.read_bytes()
    except OSError:
        if cache_only:
            return []
    if data is None:
        try:
            data = fetch(show["feed"])
            cache_dir.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
        except OSError:
            if not f.exists():
                raise
            data = f.read_bytes()   # offline: stale cache beats nothing
    return parse_feed(data)


def choose_episode(eps: list[dict], what: str | None, st: dict) -> dict | None:
    """'latest' / None -> newest unplayed (or in-progress); 'N' -> Nth newest; words -> title match."""
    if not eps:
        return None
    w = (what or "").strip().lower()
    if w in ("", "next", "unplayed"):
        for e in eps:   # something half-heard first, then the newest unplayed
            rec = st["episodes"].get(e["guid"])
            if rec and rec.get("pos", 0) > 30 and not rec.get("played"):
                return e
        for e in eps:
            if not st["episodes"].get(e["guid"], {}).get("played"):
                return e
        return eps[0]
    if w in ("latest", "newest", "new", "last"):
        return eps[0]
    if w.isdigit():
        n = int(w)
        return eps[n - 1] if 1 <= n <= len(eps) else None
    toks = [t for t in _norm(w).split() if len(t) > 1]
    best, score = None, 0
    for e in eps:
        title = _norm(e["title"])
        n = sum(1 for t in toks if t in title)
        if n > score:
            best, score = e, n
    return best


# ------------------------------------------------------------------ mpv IPC
def ipc(cmd: list, sock: Path | None = None, timeout=0.5):
    """Send one mpv JSON IPC command; returns 'data' or None if no player."""
    sock = sock or SOCK
    if not sock.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(sock))
            s.sendall((json.dumps({"command": cmd, "request_id": 1}) + "\n").encode())
            buf = b""
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    return None
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    msg = json.loads(line)
                    if msg.get("request_id") == 1:
                        return msg.get("data") if msg.get("error") == "success" else None
    except (OSError, ValueError):
        return None


def get(prop, sock=None):
    return ipc(["get_property", prop], sock)


def setp(prop, value, sock=None):
    return ipc(["set_property", prop, value], sock)


def player_running(sock=None) -> bool:
    return get("pid", sock) is not None or get("idle-active", sock) is not None


def find_player() -> str | None:
    return shutil.which("mpv")


# ------------------------------------------------------------------ actions
def _record_pos(st: dict, sock=None):
    now = st.get("now")
    if not now:
        return
    pos, dur = get("time-pos", sock), get("duration", sock)
    rec = st["episodes"].setdefault(now["guid"], {"show": now["show"], "title": now["title"]})
    if isinstance(pos, (int, float)):
        rec["pos"] = round(pos, 1)
        rec["at"] = round(time.time())
    if isinstance(dur, (int, float)) and dur > 0:
        rec["dur"] = round(dur, 1)
    d = rec.get("dur") or 0
    if d and rec.get("pos", 0) >= min(d - PLAYED_TAIL_S, d * PLAYED_FRAC):
        rec["played"] = True


def play(ep: dict, show: dict, st: dict, spawn=None, sock=None) -> str:
    sock = sock or SOCK
    stop(st, sock=sock, quiet=True)
    rec = st["episodes"].setdefault(ep["guid"], {})
    rec.update({"show": show["title"], "title": ep["title"], "url": ep["url"]})
    if ep.get("dur") and not rec.get("dur"):
        rec["dur"] = ep["dur"]
    start = rec.get("pos", 0) if not rec.get("played") and rec.get("pos", 0) > 10 else 0
    if rec.get("played"):
        rec["played"] = False
        rec["pos"] = 0
    args = ["--no-video", "--no-terminal", "--really-quiet", "--idle=no",
            f"--input-ipc-server={sock}", f"--volume={st['volume']}",
            f"--speed={st['speed']}", f"--force-media-title={ep['title']}",
            "--cache=yes", "--demuxer-max-bytes=50MiB"]
    if start:
        args.append(f"--start={max(0, start - 3):.0f}")   # a few seconds of context
    (spawn or _spawn)([find_player() or "mpv", *args, ep["url"]])
    st["now"] = {"guid": ep["guid"], "show": show["title"], "title": ep["title"],
                 "url": ep["url"], "started": time.time()}
    save(st)
    resumed = f" from {_clock(start)}" if start else ""
    return f"Playing {show['title']}: {ep['title']}{resumed}"


def _spawn(argv: list):
    try:
        SOCK.unlink()
    except OSError:
        pass
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):   # wait for the IPC socket
        if SOCK.exists():
            break
        time.sleep(0.1)
    subprocess.Popen([sys.executable, "-m", "walter.podcast", "_watch", str(p.pid)],
                     cwd=str(Path(__file__).resolve().parent.parent),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


def stop(st: dict, sock=None, quiet=False) -> str:
    sock = sock or SOCK
    if not st.get("now"):
        if player_running(sock):
            ipc(["quit"], sock)
        return "" if quiet else "Nothing was playing"
    _record_pos(st, sock)
    title = st["now"]["title"]
    ipc(["quit"], sock)
    try:
        sock.unlink()   # mpv leaves its socket behind
    except OSError:
        pass
    st["now"] = None
    save(st)
    publish(st, sock=sock, playing=False)
    return "" if quiet else f"Stopped {title}"


def _clock(s: float) -> str:
    s = int(s or 0)
    h, m, sec = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def status(st: dict, sock=None) -> str:
    now = st.get("now")
    if not now or not player_running(sock):
        return "Nothing playing"
    _record_pos(st, sock)
    save(st)
    rec = st["episodes"].get(now["guid"], {})
    paused = get("pause", sock)
    speed = get("speed", sock) or st["speed"]
    bits = [f"{'Paused' if paused else 'Playing'}: {now['show']}: {now['title']}",
            f"at {_clock(rec.get('pos', 0))}" + (f" of {_clock(rec['dur'])}" if rec.get("dur") else "")]
    if abs(float(speed) - 1.0) > 0.01:
        bits.append(f"speed {float(speed):g}x")
    return ", ".join(bits)


# ------------------------------------------------------------------ face widget
def resume_candidate(st: dict) -> dict | None:
    """The most recently listened, unfinished episode (for the face's 'continue?' chip)."""
    best = None
    for rec in st.get("episodes", {}).values():
        if rec.get("played") or (rec.get("pos") or 0) <= 10 or not rec.get("title"):
            continue
        if best is None or (rec.get("at") or 0) > (best.get("at") or 0):
            best = rec
    if not best:
        return None
    return {"show": best.get("show", ""), "title": best["title"],
            "pos": best.get("pos", 0), "dur": best.get("dur", 0), "at": best.get("at", 0)}


def now_playing(st: dict, sock=None) -> dict:
    """What the face shows: {"playing": True, show, title, pos, dur, paused, speed, ducked}
    while mpv runs, else {"playing": False, "resume": {...} | None}."""
    now = st.get("now")
    pos = get("time-pos", sock) if now else None
    if not now or pos is None:
        return {"playing": False, "resume": resume_candidate(st), "updated": round(time.time())}
    rec = st.get("episodes", {}).get(now["guid"], {})
    dur = get("duration", sock)
    vol, speed = get("volume", sock), get("speed", sock)
    full = float(st.get("volume", 100))
    return {"playing": True, "show": now.get("show", ""), "title": now.get("title", ""),
            "pos": round(float(pos), 1),
            "dur": round(float(dur), 1) if isinstance(dur, (int, float)) and dur > 0 else rec.get("dur", 0),
            "paused": bool(get("pause", sock)),
            "speed": round(float(speed), 2) if isinstance(speed, (int, float)) else st.get("speed", 1.0),
            "ducked": isinstance(vol, (int, float)) and vol < full - 1,
            "updated": round(time.time(), 1)}


def publish(st: dict, sock=None, playing=True, path: Path | None = None):
    """Write the now-playing file for the face (atomic; never raises)."""
    path = path or NOW_FILE
    try:
        data = now_playing(st, sock) if playing else \
            {"playing": False, "resume": resume_candidate(st), "updated": round(time.time())}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except Exception:
        pass


def upnext(st: dict, fetch=_get, cache_only=False, now=None, n=3) -> list[dict]:
    """The next episodes to listen to across followed shows: the in-progress one first
    (resume), then the newest unplayed. Skips what's playing now. Never raises per show."""
    now = time.time() if now is None else now
    cur = (st.get("now") or {}).get("guid")
    resume, fresh = [], []
    for show in st.get("shows", []):
        try:
            eps = episodes(show, fetch=fetch, cache_only=cache_only, now=now)
        except Exception:  # noqa: BLE001  (offline with no cache: just skip the show)
            continue
        for e in eps:
            rec = st.get("episodes", {}).get(e["guid"], {})
            if e["guid"] == cur or rec.get("played"):
                continue
            pos = rec.get("pos", 0) or 0
            item = {"source": "podcast", "show": show.get("title", ""), "title": e["title"], "guid": e["guid"],
                    "img": e.get("img", ""),
                    "date": round(e["date"]), "dur": round(e["dur"] or rec.get("dur", 0) or 0),
                    "new": bool(e["date"]) and now - e["date"] < NEW_DAYS * 86400,
                    "resume": pos > 30, "pos": round(pos) if pos > 30 else 0}
            (resume if item["resume"] else fresh).append((rec.get("at", 0) or 0, item))
    resume.sort(key=lambda x: x[0], reverse=True)
    fresh.sort(key=lambda x: x[1]["date"], reverse=True)
    out = [resume[0][1]] if resume else []
    return (out + [i for _, i in fresh])[:n]


def _ffmpeg_square(src: Path, dst: Path, px: int = ART_PX) -> bool:
    """Center-crop + resize to a px-square JPEG with ffmpeg (no Pillow on the wall box)."""
    try:
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-frames:v", "1",
                            "-vf", f"scale={px}:{px}:force_original_aspect_ratio=increase,crop={px}:{px}",
                            "-q:v", "4", str(dst)], timeout=30,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return r.returncode == 0 and dst.exists() and dst.stat().st_size > 0
    except (OSError, subprocess.SubprocessError):
        return False


def cache_art(url: str, fetch=_get, art_dir: Path | None = None, face_dir: Path | None = None,
              convert=None, now=None) -> str:
    """Local face path ("podcast-art/<sha>.jpg") for a remote image, fetching + resizing it
    once; "" if there is none (yet). Never raises. Works for any image URL (YouTube thumbs later)."""
    if not url or not url.startswith(("http://", "https://")):
        return ""
    art_dir, face_dir = art_dir or CACHE_DIR / ART_SUB, face_dir or UPNEXT_FILE.with_name(FACE_ART)
    convert = convert or _ffmpeg_square
    now = time.time() if now is None else now
    name = hashlib.sha1(url.encode()).hexdigest()[:16] + ".jpg"
    out, fail, served = art_dir / name, art_dir / (name + ".fail"), face_dir / name
    try:
        if not out.exists():
            if fail.exists() and now - fail.stat().st_mtime < ART_RETRY_S:
                return ""
            art_dir.mkdir(parents=True, exist_ok=True)
            raw = art_dir / (name + ".src")
            try:
                raw.write_bytes(fetch(url))
                ok = convert(raw, out)
            except Exception:  # noqa: BLE001
                ok = False
            finally:
                raw.unlink(missing_ok=True)
            if not ok:
                out.unlink(missing_ok=True)
                fail.touch()
                return ""
            fail.unlink(missing_ok=True)
        if not served.exists() or served.stat().st_size != out.stat().st_size:
            face_dir.mkdir(parents=True, exist_ok=True)
            tmp = served.with_name(name + ".part")
            shutil.copyfile(out, tmp)
            tmp.replace(served)
        return f"{face_dir.name}/{name}"
    except OSError:
        return ""


def _with_art(item: dict, fetch=_get, **kw) -> dict:
    item = dict(item)
    item["art"] = cache_art(item.pop("img", "") or "", fetch=fetch, **kw)
    return item


def _now_item(st: dict, fetch=_get, cache_only=True) -> dict | None:
    """The playing episode (st["now"]) as an up-next style item with its art, for the card."""
    cur = st.get("now") or {}
    if not cur.get("guid"):
        return None
    show = next((s for s in st.get("shows", []) if s.get("title") == cur.get("show")), None)
    img = ""
    if show:
        try:
            ep = next((e for e in episodes(show, fetch=fetch, cache_only=cache_only) if e["guid"] == cur["guid"]), None)
            img = (ep or {}).get("img", "")
        except Exception:  # noqa: BLE001
            img = ""
    return {"source": "podcast", "show": cur.get("show", ""), "title": cur.get("title", ""),
            "guid": cur["guid"], "img": img}


def publish_upnext(st: dict | None = None, fetch=_get, cache_only=False, path: Path | None = None,
                   n=4, art_fetch=None, yt_state: dict | None = None) -> list[dict]:
    """Write the up-next file for the face (atomic; never raises). One spare item so the
    face can drop the one that's playing right now and still show three. Each item carries
    "source" ("podcast" or "youtube") and "art" (a local face path or ""); "now" is the
    playing episode with its art (the now-playing file has none)."""
    path = path or UPNEXT_FILE
    art_fetch = art_fetch or fetch
    kw = {"face_dir": path.with_name(FACE_ART)}
    try:
        st = st if st is not None else load()
        items = merge_youtube(upnext(st, fetch=fetch, cache_only=cache_only, n=n),
                              yt_state, real=path == UPNEXT_FILE, cache_only=cache_only, n=n)
        items = [_with_art(i, fetch=art_fetch, **kw) for i in items]
        nw = _now_item(st, fetch=fetch)
        data = {"items": items, "updated": round(time.time())}
        if nw:
            data["now"] = _with_art(nw, fetch=art_fetch, **kw)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
        return items
    except Exception:  # noqa: BLE001
        return []


def merge_youtube(pods: list[dict], yt_state: dict | None = None, real=False, cache_only=False,
                  n=4, fetch=None) -> list[dict]:
    """Podcast up-next plus newest unwatched followed-YouTube videos (walter/youtube.py):
    an in-progress episode stays first, the rest newest first. yt_state None = the real
    ~/.walter/youtube.json, only for the real face file (tests pass their own). Never raises."""
    try:
        from . import youtube
        if yt_state is None:
            if not real:
                return pods
            yt_state = youtube.load()
        vids = youtube.upnext(yt_state, cache_only=cache_only, n=n,
                              **({"fetch": fetch} if fetch else {}))
    except Exception:  # noqa: BLE001
        return pods
    if not vids:
        return pods
    head = [p for p in pods[:1] if p.get("resume")]
    rest = sorted(pods[len(head):] + vids, key=lambda i: i.get("date", 0), reverse=True)
    return (head + rest)[:n]


def describe_upnext(items: list[dict]) -> str:
    if not items:
        return "Nothing new: all caught up"
    out = []
    for i, e in enumerate(items, 1):
        mark = (f" [resume at {_clock(e['pos'])}]" if e["resume"] else "") + (" [new]" if e["new"] else "")
        out.append(f"{i}. {e['show']}: {e['title']} ({_date(e['date'])}, {_clock(e['dur'])}){mark}")
    return "\n".join(out)


# ------------------------------------------------------------------ ducking
class Ducker:
    """Turn Walter's state changes into podcast ducking.

    listening / thinking / working -> volume to duck_volume percent
    speaking                       -> pause (and stay paused until he rests,
                                      so the follow-up window doesn't hear it)
    idle / sleeping / anything else-> restore volume, unpause if *we* paused it
    A podcast the person paused stays paused.
    """
    DUCK = ("listening", "thinking", "working")
    PAUSE = ("speaking",)

    def __init__(self, sock: Path | None = None, state_path: Path | None = None):
        self.sock = sock
        self.state_path = state_path
        self.mode = "full"
        self.we_paused = False

    def _vols(self):
        st = load(self.state_path)
        return st["volume"], st["volume"] * st["duck_volume"] / 100.0

    def on_state(self, state: str):
        sock = self.sock or SOCK
        if not sock.exists():
            self.mode, self.we_paused = "full", False
            return
        want = ("pause" if state in self.PAUSE
                else "duck" if state in self.DUCK else "full")
        if self.mode == "pause" and want == "duck":
            want = "pause"     # mid-turn: stay paused till Walter rests
        if want == self.mode:
            return
        full, ducked = self._vols()
        if want == "duck":
            setp("volume", ducked, sock)
        elif want == "pause":
            if get("pause", sock) is False:
                setp("pause", True, sock)
                self.we_paused = True
            setp("volume", ducked, sock)
        else:
            setp("volume", full, sock)
            if self.we_paused:
                setp("pause", False, sock)
            self.we_paused = False
        self.mode = want


# ------------------------------------------------------------------ CLI
def _watch(pid: int):
    """Save position every 5 s while mpv runs (and publish now-playing for the face
    every second); mark played near the end."""
    def alive():
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    last, tick = None, 0
    while alive():
        time.sleep(1)
        tick += 1
        st = load()
        if not (st.get("now") and alive()):   # (re-check: a 'stop' may have just saved)
            continue
        publish(st)
        if tick % 5 == 0:
            _record_pos(st)
            rec = st["episodes"].get(st["now"]["guid"], {})
            last = (st["now"]["guid"], rec.get("pos", 0), rec.get("dur", 0))
            save(st)
    st = load()
    if last and st.get("now") and st["now"]["guid"] == last[0]:
        rec = st["episodes"].setdefault(last[0], {})
        d = last[2] or rec.get("dur", 0)
        if d and last[1] >= d - PLAYED_TAIL_S - 10:   # ran out at the end
            rec["played"], rec["pos"] = True, d
        st["now"] = None
        save(st)
    publish(load(), playing=False)
    publish_upnext(cache_only=True)


def _num(s: str, default: float) -> float:
    try:
        return float(re.sub(r"[^0-9.]", "", s) or default)
    except ValueError:
        return default


USAGE = """walterctl podcast COMMAND
  follow "show name"      find it on Apple Podcasts (iTunes search) and follow its RSS feed
  unfollow "show"         stop following
  list                    followed shows
  latest [show] [N]       newest N episodes (default 5), feed cached 30 min
  play [show] [latest|next|N|title words]
                          play (default: in-progress or newest unplayed; resumes where left off)
  pause | resume | stop   (pause/resume also: toggle)
  skip [30] | back [15]   seek seconds
  speed 1.25              playback speed (remembered)
  volume N                podcast volume 0-100 (remembered)
  seekto SECONDS          jump to a position
  play-guid GUID          play one exact episode (the app's Up next list)
  status                  what's playing and where
  upnext [N]              next unplayed episodes across followed shows (in-progress first;
                          also refreshes the face's Up next card)
  duck | unduck           manual duck to duck_volume (default 25%) / restore"""


def cli(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]
    st = load()
    if cmd == "_watch":
        _watch(int(rest[0]))
        return 0
    if cmd == "follow":
        term = " ".join(rest).strip()
        if not term:
            print("follow what?")
            return 2
        if term.startswith("http"):
            root = ET.fromstring(_get(term)).find("channel")
            show = {"title": clean_title((root.findtext("title") if root is not None else term) or term),
                    "feed": term, "author": "", "itunes_id": None}
        else:
            show = pick_result(term, search(term))
            if not show:
                print(f"Couldn't find a podcast called {term}")
                return 1
        st["shows"] = [s for s in st["shows"] if s["feed"] != show["feed"]] + [show]
        save(st)
        print(f"Following {show['title']}" + (f" by {show['author']}" if show["author"] else "")
              + f"\nfeed: {show['feed']}")
        try:
            for i, e in enumerate(episodes(show)[:3], 1):
                print(f"  {i}. {e['title']} ({_date(e['date'])})")
        except Exception as e:  # noqa: BLE001
            print(f"  (couldn't read the feed yet: {e})")
        return 0
    if cmd == "list":
        if not st["shows"]:
            print("Not following any podcasts yet")
        for s in st["shows"]:
            print(f"{s['title']}" + (f" ({s['author']})" if s.get("author") else ""))
        return 0
    if cmd == "unfollow":
        show = find_show(st, " ".join(rest))
        if not show:
            print("No followed show matches that")
            return 1
        st["shows"] = [s for s in st["shows"] if s is not show]
        save(st)
        print(f"Unfollowed {show['title']}")
        return 0
    if cmd == "latest":
        n = 5
        if rest and rest[-1].isdigit():
            n = int(rest.pop())
        show = find_show(st, " ".join(rest))
        if not show:
            print("Which show? " + ", ".join(s["title"] for s in st["shows"]) if st["shows"]
                  else "Not following any podcasts yet")
            return 1
        print(show["title"])
        for i, e in enumerate(episodes(show)[:n], 1):
            rec = st["episodes"].get(e["guid"], {})
            mark = " [played]" if rec.get("played") else (
                f" [at {_clock(rec['pos'])}]" if rec.get("pos", 0) > 30 else "")
            print(f"  {i}. {e['title']} ({_date(e['date'])}, {_clock(e['dur'])}){mark}")
        return 0
    if cmd == "play":
        if not find_player():
            print("No player: install mpv (sudo apt-get install mpv)")
            return 1
        show, what = _split_show(st, rest)
        if not show:
            if not rest and st.get("now") and player_running():
                return cli(["resume"])
            print("Which show? Following: " + ", ".join(s["title"] for s in st["shows"])
                  if st["shows"] else "Not following any podcasts yet: walterctl podcast follow NAME")
            return 1
        ep = choose_episode(episodes(show), what, st)
        if not ep:
            print(f"No episode of {show['title']} matches '{what}'")
            return 1
        print(play(ep, show, st))
        publish_upnext(load(), cache_only=True)
        return 0
    if cmd in ("pause", "resume", "toggle"):
        if not player_running():
            if cmd == "resume" and st.get("now"):
                return cli(["play", st["now"]["show"], st["now"]["title"]])
            print("Nothing playing")
            return 1
        val = {"pause": True, "resume": False}.get(cmd)
        if val is None:
            val = not get("pause")
        setp("pause", val)
        if not val:
            setp("volume", st["volume"])
        _record_pos(st)
        save(st)
        print(("Paused " if val else "Resumed ") + st["now"]["title"] if st.get("now") else "ok")
        return 0
    if cmd == "stop":
        print(stop(st))
        publish_upnext(cache_only=True)
        return 0
    if cmd in ("skip", "forward", "ahead", "back", "rewind"):
        secs = _num(rest[0], 30 if cmd in ("skip", "forward", "ahead") else 15) if rest else (
            30 if cmd in ("skip", "forward", "ahead") else 15)
        if cmd in ("back", "rewind"):
            secs = -secs
        if ipc(["seek", secs, "relative"]) is None and not player_running():
            print("Nothing playing")
            return 1
        print(f"{'Skipped ahead' if secs > 0 else 'Went back'} {abs(secs):g} seconds")
        return 0
    if cmd == "seekto":   # absolute seconds (the app's progress bar)
        if not rest or not player_running():
            print("Nothing playing")
            return 1
        to = max(0.0, _num(rest[0], 0.0))
        ipc(["seek", to, "absolute"])
        print(f"Jumped to {_clock(to)}")
        return 0
    if cmd == "play-guid":   # an exact episode by guid (the app's Up next list)
        guid = " ".join(rest).strip()
        if not find_player():
            print("No player: install mpv (sudo apt-get install mpv)")
            return 1
        for show in st["shows"]:
            try:
                ep = next((e for e in episodes(show) if e["guid"] == guid), None)
            except Exception:  # noqa: BLE001
                ep = None
            if ep:
                print(play(ep, show, st))
                publish_upnext(load(), cache_only=True)
                return 0
        print("That episode isn't in a followed show any more")
        return 1
    if cmd == "speed":
        sp = max(0.5, min(3.0, _num(rest[0], 1.0))) if rest else 1.0
        st["speed"] = sp
        save(st)
        setp("speed", sp)
        print(f"Speed {sp:g}x")
        return 0
    if cmd == "volume":
        v = max(0, min(130, int(_num(rest[0], 100)))) if rest else 100
        st["volume"] = v
        save(st)
        setp("volume", v)
        print(f"Podcast volume {v}")
        return 0
    if cmd == "status":
        print(status(st))
        return 0
    if cmd in ("upnext", "up-next", "next"):
        n = int(rest[0]) if rest and rest[0].isdigit() else 3
        publish_upnext(st)
        print(describe_upnext(upnext(st, n=n)))
        return 0
    if cmd in ("duck", "unduck"):
        setp("volume", st["volume"] * (st["duck_volume"] / 100.0 if cmd == "duck" else 1))
        print(cmd + "ed")
        return 0
    print(f"unknown podcast command '{cmd}'\n{USAGE}")
    return 2


def _split_show(st, rest):
    """'morning news latest' -> (show, 'latest'); 'latest' -> (only show, 'latest')."""
    words = " ".join(rest).strip()
    if not words:
        return find_show(st, None), None
    toks = words.split()
    for cut in range(len(toks), 0, -1):
        head, tail = " ".join(toks[:cut]), " ".join(toks[cut:])
        show = find_show(st, head)
        if show and all(t in _norm(show["title"]).split() or t in ("the", "podcast")
                        for t in _norm(head).split()):
            return show, tail or None
    show = find_show(st, None)   # one show followed: all words pick the episode
    return show, words if show else None


def _date(ts: float) -> str:
    if not ts:
        return "undated"
    return time.strftime("%b %-d, %Y", time.localtime(ts))


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
