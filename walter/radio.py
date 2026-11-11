"""Internet radio: free, DRM-free live streams from the public radio-browser.info directory.

Stations are resolved by the community API (https://api.radio-browser.info): the mirror
list comes from a DNS lookup of all.api.radio-browser.info (each address reverse-resolved
to its https host name), with a small fallback list. Requests carry a generic User-Agent.
Everything the directory returns (station names, tags, stream titles) is UNTRUSTED text:
it is cleaned with security.clean_title and only ever shown or played, never obeyed.

Playback is a detached mpv on its own IPC socket (like walter/podcast.py), so the
conductor's :class:`Ducker` ducks it while Walter listens/thinks and pauses it while he
talks. A tiny watcher writes face/dashboard-podcast.json with ``"source": "radio"`` so
the face's now-playing card shows the station and a "live" tag.

State: ~/.walter/radio.json (no secrets)
  {"now": {name, url, uuid, codec, bitrate, started} | null,
   "favorites": [{name, url, uuid}], "recent": [...], "volume": 80}
Presets from config [radio] favorites ("Name" or "Name=https://url") are merged in.
"""
from __future__ import annotations

import json
import os
import random
import re
import socket
import subprocess
import sys
import time
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path

from . import podcast
from .security import clean_title

DATA_DIR = Path(os.environ.get("WALTER_DATA", Path.home() / ".walter"))
STATE = DATA_DIR / "radio.json"
SOCK = DATA_DIR / "radio-mpv.sock"
NOW_FILE = podcast.NOW_FILE          # the face's now-playing card reads this one file
UA = "Walter-radio/1.0"              # generic: never the owner's details
DNS_NAME = "all.api.radio-browser.info"
FALLBACK_HOSTS = ["de1.api.radio-browser.info", "fi1.api.radio-browser.info",
                  "de2.api.radio-browser.info", "nl1.api.radio-browser.info"]

# spoken genres -> radio-browser tags (anything else is tried as a tag too, after a name search)
GENRES = {
    "country": "country", "classic country": "classic country", "jazz": "jazz",
    "smooth jazz": "smooth jazz", "classical": "classical", "rock": "rock",
    "classic rock": "classic rock", "oldies": "oldies", "pop": "pop", "news": "news",
    "talk": "talk", "christian": "christian", "gospel": "gospel", "blues": "blues",
    "hip hop": "hip hop", "rap": "hip hop", "chill": "chillout", "chillout": "chillout",
    "lofi": "lofi", "lo-fi": "lofi", "ambient": "ambient", "folk": "folk",
    "bluegrass": "bluegrass", "80s": "80s", "90s": "90s", "70s": "70s", "60s": "60s",
    "sports": "sports", "dance": "dance", "electronic": "electronic", "reggae": "reggae",
    "soul": "soul", "r&b": "rnb", "metal": "metal", "alternative": "alternative",
    "indie": "indie", "christmas": "christmas", "kids": "kids", "public radio": "npr",
    "npr": "npr", "top 40": "top 40", "easy listening": "easy listening",
}


# ------------------------------------------------------------------ state
def load(path: Path | None = None) -> dict:
    try:
        st = json.loads((path or STATE).read_text())
    except (OSError, ValueError):
        st = {}
    st.setdefault("now", None)
    st.setdefault("favorites", [])
    st.setdefault("recent", [])
    st.setdefault("volume", None)
    return st


def save(st: dict, path: Path | None = None):
    path = path or STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(path)


def config(path: Path | None = None) -> dict:
    """[radio] from config.toml (read directly: cheap, and never fails)."""
    try:
        with open(path or DATA_DIR / "config.toml", "rb") as f:
            c = tomllib.load(f).get("radio", {})
    except (OSError, ValueError):
        c = {}
    favs = c.get("favorites") if isinstance(c.get("favorites"), list) else []
    return {"favorites": [str(x) for x in favs], "volume": int(c.get("volume", 80)),
            "duck_volume": int(c.get("duck_volume", 25))}


def volume(st: dict, cfg: dict | None = None) -> int:
    v = st.get("volume")
    return int(v if isinstance(v, (int, float)) else (cfg or config())["volume"])


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9& ]+", " ", (s or "").lower())).strip()


def safe_url(u: str) -> bool:
    try:
        p = urllib.parse.urlsplit(u or "")
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc) and not re.search(r"\s", u)


def favorites(st: dict, cfg: dict | None = None) -> list[dict]:
    """Saved favourites plus config presets: [{name, url?, uuid?, preset?}]."""
    out = [dict(f) for f in st.get("favorites", []) if f.get("name")]
    have = {_norm(f["name"]) for f in out}
    for raw in (cfg or config())["favorites"]:
        name, _, url = raw.partition("=")
        name, url = name.strip(), url.strip()
        if not name or _norm(name) in have:
            continue
        have.add(_norm(name))
        out.append({"name": name, "url": url if safe_url(url) else "", "preset": True})
    return out


def find_favorite(words: str, favs: list[dict]) -> dict | None:
    """A favourite the words really name (all spoken words in its name)."""
    said = [w for w in _norm(words).split() if w not in ("the", "radio", "station", "on", "a")]
    if not said:
        return None
    w = " ".join(said)
    for f in favs:
        if _norm(f["name"]) == w or _norm(f["name"]).replace(" radio", "") == w:
            return f
    for f in favs:
        name = _norm(f["name"]).split()
        if all(t in name for t in said):
            return f
    return None


# ------------------------------------------------------------------ directory
def mirrors(resolve=None, reverse=None) -> list[str]:
    """radio-browser.info API hosts, shuffled (DNS round robin, as the API asks)."""
    resolve = resolve or (lambda: {ai[4][0] for ai in socket.getaddrinfo(DNS_NAME, 443, proto=socket.IPPROTO_TCP)})
    reverse = reverse or (lambda ip: socket.gethostbyaddr(ip)[0])
    hosts = []
    try:
        for ip in resolve():
            try:
                h = reverse(ip)
            except OSError:
                continue
            if h.endswith(".api.radio-browser.info") and h not in hosts:
                hosts.append(h)
    except OSError:
        pass
    random.shuffle(hosts)
    return hosts or random.sample(FALLBACK_HOSTS, len(FALLBACK_HOSTS))


def _get(url: str, timeout=8) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(2_000_000)


def api(path: str, params: dict | None = None, fetch=_get, hosts=None):
    """GET /json/<path> from the first mirror that answers; raises OSError if none do."""
    q = ("?" + urllib.parse.urlencode(params)) if params else ""
    err = None
    for h in (hosts if hosts is not None else mirrors())[:4]:
        try:
            return json.loads(fetch(f"https://{h}/json/{path}{q}"))
        except (OSError, ValueError) as e:
            err = e
    raise OSError(f"radio directory unreachable ({err})")


def _station(r: dict) -> dict | None:
    url = (r.get("url_resolved") or r.get("url") or "").strip()
    if not safe_url(url) or str(r.get("lastcheckok", 1)) == "0":
        return None
    return {"name": clean_title(r.get("name") or "", 80) or "Radio", "url": url,
            "uuid": str(r.get("stationuuid") or "")[:40],
            "codec": clean_title(r.get("codec") or "", 12), "bitrate": int(r.get("bitrate") or 0),
            "country": clean_title(r.get("countrycode") or "", 4),
            "state": clean_title(r.get("state") or "", 40),
            "tags": clean_title(r.get("tags") or "", 80), "votes": int(r.get("votes") or 0),
            "clicks": int(r.get("clickcount") or 0), "hls": bool(r.get("hls"))}


def search(words: str, fetch=_get, hosts=None, limit=10) -> list[dict]:
    """Stations for spoken words: a genre ('country') searches by tag, anything else by
    name first ('WNYC', 'Jazz FM') and then by tag. Most-listened first, working ones only."""
    w = _norm(words)
    w = re.sub(r"\b(radio|station|music|fm|am)$", "", w).strip() or w
    base = {"hidebroken": "true", "order": "clickcount", "reverse": "true", "limit": str(limit)}

    def run(**kw):
        rows = api("stations/search", {**base, **kw}, fetch=fetch, hosts=hosts)
        return [s for s in (_station(r) for r in rows if isinstance(r, dict)) if s]

    tag = GENRES.get(w)
    if tag:
        out = run(tag=tag, tagExact="true", countrycode="US") or run(tag=tag, tagExact="true")
        return out
    out = run(name=words.strip())
    if out:
        exact = [s for s in out if _norm(s["name"]).startswith(w)]
        return exact + [s for s in out if s not in exact]
    return run(tag=w)


def count_click(st_uuid: str, fetch=_get, hosts=None):
    """Tell the directory a station was played (its etiquette; best effort)."""
    if st_uuid and re.fullmatch(r"[0-9a-f-]{8,40}", st_uuid):
        try:
            api(f"url/{st_uuid}", fetch=fetch, hosts=hosts)
        except OSError:
            pass


def resolve(words: str, st: dict, cfg: dict | None = None, fetch=_get, hosts=None) -> dict | None:
    """Favourite or directory station for these words, else None."""
    fav = find_favorite(words, favorites(st, cfg))
    if fav:
        if fav.get("url"):
            return {"name": fav["name"], "url": fav["url"], "uuid": fav.get("uuid", "")}
        found = search(fav["name"], fetch=fetch, hosts=hosts)
        if found:
            return {**found[0], "name": fav["name"]}
    found = search(words, fetch=fetch, hosts=hosts)
    return found[0] if found else None


# ------------------------------------------------------------------ mpv
def ipc(cmd, sock=None):
    return podcast.ipc(cmd, sock or SOCK)


def get(prop, sock=None):
    return ipc(["get_property", prop], sock)


def setp(prop, value, sock=None):
    return ipc(["set_property", prop, value], sock)


def player_running(sock=None) -> bool:
    return get("pid", sock) is not None or get("idle-active", sock) is not None


def mpv_args(station: dict, vol: int, sock: Path) -> list[str]:
    # --ytdl=no: a station URL is never handed to yt-dlp; playlists stay mpv's safe default
    return [podcast.find_player() or "mpv", "--no-video", "--no-terminal", "--really-quiet",
            "--idle=no", "--ytdl=no", f"--input-ipc-server={sock}", f"--volume={vol}",
            f"--force-media-title={station['name']}", "--cache=yes", "--cache-secs=10",
            "--", station["url"]]


def _spawn(argv: list, sock: Path):
    try:
        sock.unlink()
    except OSError:
        pass
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(50):
        if sock.exists():
            break
        time.sleep(0.1)
    subprocess.Popen([sys.executable, "-m", "walter.radio", "_watch", str(p.pid)],
                     cwd=str(Path(__file__).resolve().parent.parent), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def play(station: dict, st: dict, spawn=None, sock=None, stop_others=None) -> str:
    sock = sock or SOCK
    (stop_others or _stop_others)()
    stop(st, sock=sock, quiet=True)
    (spawn or _spawn)(mpv_args(station, volume(st), sock), sock)
    st["now"] = {k: station.get(k) for k in ("name", "url", "uuid", "codec", "bitrate")}
    st["now"]["started"] = time.time()
    st["recent"] = ([{"name": station["name"], "url": station["url"], "uuid": station.get("uuid", "")}]
                    + [r for r in st["recent"] if r.get("url") != station["url"]])[:8]
    save(st)
    return f"Playing {station['name']} (live radio)"


def _stop_others():
    """One thing at a time: a podcast stops when the radio starts."""
    try:
        pst = podcast.load()
        if pst.get("now") or podcast.player_running():
            podcast.stop(pst, quiet=True)
    except Exception:  # noqa: BLE001
        pass


def stop(st: dict, sock=None, quiet=False) -> str:
    sock = sock or SOCK
    running = player_running(sock)
    if running:
        ipc(["quit"], sock)
    try:
        sock.unlink()
    except OSError:
        pass
    name = (st.get("now") or {}).get("name")
    if st.get("now"):
        st["now"] = None
        save(st)
    if name or running:
        publish(st, sock=sock, playing=False)
    if quiet:
        return ""
    return f"Stopped {name}" if name else ("Stopped the radio" if running else "The radio isn't on")


def stream_title(sock=None) -> str:
    md = get("metadata", sock)
    if isinstance(md, dict):
        for k in ("icy-title", "StreamTitle", "title", "TITLE"):
            if isinstance(md.get(k), str) and md[k].strip():
                return clean_title(md[k], 100)
    return ""


def now_playing(st: dict, sock=None, cfg: dict | None = None) -> dict:
    now = st.get("now")
    if not now or not player_running(sock):
        return {"playing": False, "source": "radio", "updated": round(time.time())}
    vol = get("volume", sock)
    return {"playing": True, "source": "radio", "live": True, "show": now.get("name", ""),
            "title": stream_title(sock) or "Live radio", "pos": 0, "dur": 0,
            "paused": bool(get("pause", sock)), "speed": 1.0,
            "ducked": isinstance(vol, (int, float)) and vol < volume(st, cfg) - 1,
            "updated": round(time.time(), 1)}


def publish(st: dict, sock=None, playing=True, path: Path | None = None):
    """Write the face's now-playing file (atomic; never raises). When the radio stops the
    podcast's own 'continue?' chip comes back."""
    path = path or NOW_FILE
    try:
        if playing:
            data = now_playing(st, sock)
        else:
            data = {"playing": False, "resume": podcast.resume_candidate(podcast.load()),
                    "updated": round(time.time())}
        tmp = path.with_name(path.name + ".radio.tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except Exception:  # noqa: BLE001
        pass


def status(st: dict, sock=None) -> str:
    now = st.get("now")
    if not now or not player_running(sock):
        return "The radio isn't on"
    t = stream_title(sock)
    paused = get("pause", sock)
    return f"{'Paused' if paused else 'Playing'}: {now['name']} (live radio)" + (f", now: {t}" if t else "")


# ------------------------------------------------------------------ ducking
class Ducker:
    """Same rules as podcast.Ducker: duck while listening/thinking, pause while speaking
    (stays paused till Walter rests), restore after; a radio the person paused stays paused."""
    DUCK = podcast.Ducker.DUCK
    PAUSE = podcast.Ducker.PAUSE

    def __init__(self, sock: Path | None = None, state_path: Path | None = None, cfg: dict | None = None):
        self.sock, self.state_path, self.cfg = sock, state_path, cfg
        self.mode, self.we_paused = "full", False

    def on_state(self, state: str):
        sock = self.sock or SOCK
        if not sock.exists():
            self.mode, self.we_paused = "full", False
            return
        want = "pause" if state in self.PAUSE else "duck" if state in self.DUCK else "full"
        if self.mode == "pause" and want == "duck":
            want = "pause"
        if want == self.mode:
            return
        cfg = self.cfg or config()
        full = volume(load(self.state_path), cfg)
        ducked = full * cfg["duck_volume"] / 100.0
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
    def alive():
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    while alive():
        time.sleep(2)
        st = load()
        if st.get("now") and alive():
            publish(st)
    st = load()
    if st.get("now") and not player_running():
        st["now"] = None
        save(st)
    publish(st, playing=False)


USAGE = """walterctl radio COMMAND   (live internet radio from radio-browser.info; free, no DRM)
  play "country" | "WNYC" | "station name"   genre, call sign, station or favourite
  play                    the last station again
  pause | resume | stop   (toggle = pause/resume)
  volume N | up | down    radio volume 0-100 (remembered)
  status                  what's on
  list [words]            search the directory (no words: favourites and recent)
  favorites [list]        favourites (yours + config [radio] favorites presets)
  favorites add "name"    save a station (searched) or add "name=https://stream" directly
  favorites remove "name"
  duck | unduck           manual duck / restore"""


def _fmt(s: dict) -> str:
    bits = [b for b in (s.get("codec"), f"{s['bitrate']}k" if s.get("bitrate") else "",
                        s.get("state") or s.get("country")) if b]
    return s["name"] + (f" ({', '.join(bits)})" if bits else "")


def cli(argv: list[str], fetch=_get, hosts=None, spawn=None) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]
    words = " ".join(rest).strip()
    st, cfg = load(), config()
    if cmd == "_watch":
        _watch(int(rest[0]))
        return 0
    if cmd == "play":
        if not podcast.find_player():
            print("No player: install mpv (sudo apt-get install mpv)")
            return 1
        if not words:
            if not st["recent"]:
                print("Play what? Try: walterctl radio play country")
                return 2
            station = st["recent"][0]
        else:
            try:
                station = resolve(words, st, cfg, fetch=fetch, hosts=hosts)
            except OSError as e:
                print(f"Couldn't reach the radio directory: {e}")
                return 1
            if not station:
                print(f"I couldn't find a station for \"{clean_title(words, 60)}\"")
                return 1
            count_click(station.get("uuid", ""), fetch=fetch, hosts=hosts)
        print(play(station, st, spawn=spawn))
        return 0
    if cmd == "stop":
        print(stop(st))
        return 0
    if cmd in ("pause", "resume", "toggle"):
        if not player_running():
            print("The radio isn't on")
            return 1
        val = {"pause": True, "resume": False}.get(cmd, not get("pause"))
        setp("pause", val)
        print("Paused the radio" if val else "Radio back on")
        return 0
    if cmd == "volume":
        cur = volume(st, cfg)
        if words in ("up", "down"):
            v = cur + (10 if words == "up" else -10)
        elif re.fullmatch(r"\d{1,3}", words):
            v = int(words)
        else:
            print(f"Radio volume {cur}")
            return 0
        v = max(0, min(100, v))
        st["volume"] = v
        save(st)
        setp("volume", v)
        print(f"Radio volume {v}")
        return 0
    if cmd == "status":
        print(status(st))
        return 0
    if cmd in ("duck", "unduck"):
        full = volume(st, cfg)
        setp("volume", full * cfg["duck_volume"] / 100.0 if cmd == "duck" else full)
        print("ok")
        return 0
    if cmd in ("list", "search"):
        if not words:
            favs = favorites(st, cfg)
            print("Favourites:" if favs else "No favourites yet (walterctl radio favorites add NAME)")
            for f in favs:
                print(f"  {f['name']}" + (" [preset]" if f.get("preset") else ""))
            if st["recent"]:
                print("Recent:")
                for r in st["recent"][:5]:
                    print(f"  {r['name']}")
            return 0
        try:
            found = search(words, fetch=fetch, hosts=hosts)
        except OSError as e:
            print(f"Couldn't reach the radio directory: {e}")
            return 1
        if not found:
            print("No stations found")
            return 1
        for i, s in enumerate(found[:10], 1):
            print(f"{i}. {_fmt(s)}")
        return 0
    if cmd in ("favorites", "favourites", "fav", "favs"):
        sub = rest[0] if rest else "list"
        arg = " ".join(rest[1:]).strip()
        if sub == "list":
            return cli(["list"], fetch=fetch, hosts=hosts)
        if sub == "add":
            if not arg:
                print("Add which station?")
                return 2
            name, _, url = arg.partition("=")
            name, url = clean_title(name.strip(), 80), url.strip()
            if url:
                if not safe_url(url):
                    print("That isn't an http(s) stream address")
                    return 2
                fav = {"name": name, "url": url, "uuid": ""}
            else:
                cur = st.get("now") if not name or _norm(name) in ("this", "this station", "it") else None
                if cur:
                    fav = {"name": cur["name"], "url": cur["url"], "uuid": cur.get("uuid", "")}
                else:
                    try:
                        found = search(name, fetch=fetch, hosts=hosts)
                    except OSError as e:
                        print(f"Couldn't reach the radio directory: {e}")
                        return 1
                    if not found:
                        print(f"No station called \"{name}\"")
                        return 1
                    fav = {"name": found[0]["name"], "url": found[0]["url"], "uuid": found[0]["uuid"]}
            st["favorites"] = [f for f in st["favorites"] if _norm(f["name"]) != _norm(fav["name"])] + [fav]
            save(st)
            print(f"Saved {fav['name']} to radio favourites")
            return 0
        if sub in ("remove", "rm", "delete"):
            f = find_favorite(arg, st["favorites"])
            if not f:
                presets = [p for p in favorites(st, cfg) if p.get("preset")]
                if find_favorite(arg, presets):
                    print("That one is a preset in config.toml [radio] favorites; remove it there")
                    return 1
                print("No favourite by that name")
                return 1
            st["favorites"] = [x for x in st["favorites"] if x is not f]
            save(st)
            print(f"Removed {f['name']}")
            return 0
    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
