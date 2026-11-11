"""YouTube channels: follow by public page/handle, newest videos from the public RSS feed.

No Google login and no API key: a channel is resolved from its public page (a handle,
a channel URL or a search on the results page), and its videos come from
https://www.youtube.com/feeds/videos.xml?channel_id=UC... (cached 30 min).
Playback goes through the wall browser (``walterctl media youtube url``).

State in ~/.walter/youtube.json (no secrets):
  {"channels": [{"title", "id", "handle"}], "watched": {video_id: ts}}

All fetched titles are untrusted data: they go through security.clean_title and are
only ever shown or read out, never acted on.
"""
from __future__ import annotations
from .security import clean_title  # noqa: E402

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(os.environ.get("WALTER_DATA", Path.home() / ".walter"))
STATE = DATA_DIR / "youtube.json"
CACHE_DIR = DATA_DIR / "youtube-cache"
CACHE_TTL = 30 * 60
NEW_DAYS = 7
FEED = "https://www.youtube.com/feeds/videos.xml?channel_id="
UA = "Mozilla/5.0 (X11; Linux x86_64) Walter-youtube/1.0"
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}
CID = re.compile(r"UC[A-Za-z0-9_-]{22}")

USAGE = """walterctl youtube ... : follow YouTube channels (public RSS, no login)
  follow "channel name" | @handle | channel URL
  unfollow "channel"
  list                          followed channels
  latest [channel] [N]          newest videos (all channels if none named)
  play [channel] [latest|N|title words]   newest unwatched (or the Nth / a title match)
  upnext                        what's next across channels (also on the face's Up next)
  watched N | unwatched N       mark the Nth of 'upnext'"""


# ------------------------------------------------------------------ state
def load(path: Path | None = None) -> dict:
    try:
        st = json.loads((path or STATE).read_text())
    except (OSError, ValueError):
        st = {}
    st.setdefault("channels", [])
    st.setdefault("watched", {})
    return st


def save(st: dict, path: Path | None = None):
    path = path or STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1))
    tmp.replace(path)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (s or "").lower()).strip()


def find_channel(st: dict, words: str | None) -> dict | None:
    chans = st["channels"]
    if not chans:
        return None
    if not words:
        return chans[0] if len(chans) == 1 else None
    w = _norm(words.lstrip("@"))
    for c in chans:
        if w in (_norm(c["title"]), _norm(c.get("handle", "").lstrip("@"))):
            return c
    toks = [t for t in w.split() if t not in ("the", "channel", "youtube", "on", "a")]
    best, score = None, 0
    for c in chans:
        title = _norm(c["title"]).split() + [_norm(c.get("handle", "").lstrip("@"))]
        n = sum(1 for t in toks if t in title)
        if n > score:
            best, score = c, n
    return best


# ------------------------------------------------------------------ network
def _get(url: str, timeout=15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(3_000_000)


def page_url(term: str) -> str | None:
    """Channel page for a handle or URL; None means 'search for it'."""
    t = term.strip()
    if t.startswith("@") and " " not in t:
        return "https://www.youtube.com/" + urllib.parse.quote(t, safe="@._-")
    if t.startswith(("http://", "https://")):
        host = urllib.parse.urlparse(t).hostname or ""
        if host == "youtube.com" or host.endswith(".youtube.com"):
            return t
    return None


def channel_id_from_page(html: str) -> str | None:
    """The page's own channel id (not a recommended one): canonical/meta first."""
    for pat in (r'"externalId":"(UC[A-Za-z0-9_-]{22})"',
                r'<meta itemprop="(?:identifier|channelId)" content="(UC[A-Za-z0-9_-]{22})"',
                r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[A-Za-z0-9_-]{22})"',
                r'"browseId":"(UC[A-Za-z0-9_-]{22})"'):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


def search_channels(html: str) -> list[dict]:
    """channelRenderer entries on a results page: [{id, title, handle}] in page order."""
    out, seen = [], set()
    for m in re.finditer(r'"channelRenderer":\{"channelId":"(UC[A-Za-z0-9_-]{22})"', html):
        cid = m.group(1)
        if cid in seen:
            continue
        seen.add(cid)
        chunk = html[m.end():m.end() + 4000]
        t = re.search(r'"title":\{"simpleText":"((?:[^"\\]|\\.)*)"', chunk)
        h = re.search(r'"canonicalBaseUrl":"/(@[^"]+)"', chunk)
        title = json.loads('"' + t.group(1) + '"') if t else ""
        out.append({"id": cid, "title": clean_title(title), "handle": h.group(1) if h else ""})
    return out


def pick_channel(term: str, found: list[dict]) -> dict | None:
    if not found:
        return None
    t = _norm(term)
    for c in found:
        if _norm(c["title"]) == t or _norm(c["handle"].lstrip("@")) == t.replace(" ", ""):
            return c
    for c in found:
        if t and (t in _norm(c["title"]) or _norm(c["title"]) in t):
            return c
    return found[0]


def resolve(term: str, fetch=_get) -> dict | None:
    """{"id", "title", "handle"} for a name, @handle, channel URL or bare UC id; None if not found."""
    term = term.strip()
    m = CID.fullmatch(term) or (CID.search(term) if "/channel/" in term else None)
    name = ""
    if m:
        cid, handle = m.group(0), ""
    else:
        url = page_url(term)
        if url:
            html = fetch(url).decode("utf-8", "replace")
            cid = channel_id_from_page(html)
            handle = term if term.startswith("@") else ""
            og = re.search(r'<meta property="og:title" content="([^"]*)"', html)
            name = clean_title(og.group(1).replace("&amp;", "&")) if og else ""
        else:
            q = urllib.parse.urlencode({"search_query": term, "sp": "EgIQAg=="})  # Type: Channel
            best = pick_channel(term, search_channels(
                fetch("https://www.youtube.com/results?" + q).decode("utf-8", "replace")))
            if not best:
                return None
            cid, handle, name = best["id"], best["handle"], best["title"]
    if not cid:
        return None
    title = ""
    try:
        root = ET.fromstring(fetch(FEED + cid))
        title = clean_title(root.findtext("a:title", "", NS) or "")
    except (OSError, ET.ParseError):
        pass
    return {"id": cid, "title": title or name or handle or cid, "handle": handle}


def parse_feed(xml_bytes: bytes, channel: str = "") -> list[dict]:
    """Videos newest first: {vid, title, url, date, img, channel}."""
    root = ET.fromstring(xml_bytes)
    channel = channel or clean_title(root.findtext("a:title", "", NS) or "")
    out = []
    for e in root.findall("a:entry", NS):
        vid = (e.findtext("yt:videoId", "", NS) or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            continue
        ts = 0.0
        try:
            ts = datetime.fromisoformat((e.findtext("a:published", "", NS) or "").strip()).timestamp()
        except ValueError:
            pass
        th = e.find("media:group/media:thumbnail", NS)
        img = (th.get("url") or "").strip() if th is not None else ""
        if not img.startswith("https://") or not (urllib.parse.urlparse(img).hostname or "").endswith("ytimg.com"):
            img = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        out.append({"vid": vid, "title": clean_title(e.findtext("a:title", "", NS) or ""),
                    "url": f"https://www.youtube.com/watch?v={vid}", "date": ts, "img": img,
                    "channel": channel})
    out.sort(key=lambda v: v["date"], reverse=True)
    return out


_AGE = {"s": 1, "sec": 1, "second": 1, "m": 60, "min": 60, "minute": 60, "h": 3600, "hr": 3600, "hour": 3600,
        "d": 86400, "day": 86400, "w": 604800, "wk": 604800, "week": 604800, "mo": 2592000, "month": 2592000,
        "y": 31536000, "yr": 31536000, "year": 31536000}


def _age(text: str) -> float | None:
    """Seconds for '2w ago' / '3 days ago' / 'Streamed 5 hours ago'; None if it isn't an age."""
    m = re.search(r"(\d+)\s*([a-z]+?)s?\s+ago", (text or "").lower())
    if not m or m.group(2) not in _AGE:
        return None
    return int(m.group(1)) * _AGE[m.group(2)]


def parse_videos_page(html: str, channel: str = "", now=None) -> list[dict]:
    """Fallback when the RSS feed is down: the channel's public /videos page (ytInitialData
    lockups). Dates are approximate ('2w ago'). Same shape as parse_feed."""
    now = time.time() if now is None else now
    starts = [m.start() for m in re.finditer(r'"lockupViewModel":\{"contentImage"', html)]
    out, seen = [], set()
    for k, a in enumerate(starts):
        seg = html[a:starts[k + 1] if k + 1 < len(starts) else a + 20000]
        vid = re.search(r'"contentId":"([A-Za-z0-9_-]{11})"', seg)
        title = re.search(r'"lockupMetadataViewModel":\{"title":\{"content":"((?:[^"\\]|\\.)*)"', seg)
        if not vid or not title or vid.group(1) in seen:
            continue
        seen.add(vid.group(1))
        try:
            t = json.loads('"' + title.group(1) + '"')
        except ValueError:
            t = title.group(1)
        age = None
        for c in re.findall(r'"text":\{"content":"([^"]{1,40})"\}', seg):
            age = _age(c)
            if age is not None:
                break
        v = vid.group(1)
        out.append({"vid": v, "title": clean_title(t), "url": f"https://www.youtube.com/watch?v={v}",
                    "date": round(now - age) if age is not None else 0.0,
                    "img": f"https://i.ytimg.com/vi/{v}/hqdefault.jpg", "channel": channel})
    out.sort(key=lambda x: x["date"], reverse=True)
    return out


def videos(ch: dict, fetch=_get, cache_dir: Path | None = None, now=None, cache_only=False) -> list[dict]:
    """Newest first, from the public RSS feed (cached 30 min); if YouTube's feed is down
    (it 404s for hours at times) the public /videos page instead (cached as JSON).
    cache_only: whatever is cached however old (no network; [] if none)."""
    cache_dir = cache_dir or CACHE_DIR
    now = time.time() if now is None else now
    f, fj = cache_dir / f"{ch['id']}.xml", cache_dir / f"{ch['id']}.page.json"
    title = ch.get("title", "")

    def fresh(p):
        try:
            return cache_only or now - p.stat().st_mtime < CACHE_TTL
        except OSError:
            return False

    def cached_page():
        return json.loads(fj.read_text()).get("videos", [])

    newest = max((p for p in (f, fj) if p.exists()), key=lambda p: p.stat().st_mtime, default=None)
    if newest is not None and fresh(newest):
        try:
            return parse_feed(f.read_bytes(), title) if newest == f else cached_page()
        except (OSError, ValueError, ET.ParseError):
            pass
    if cache_only:
        return []
    err = None
    try:
        data = fetch(FEED + ch["id"])
        vids = parse_feed(data, title)
        cache_dir.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
        return vids
    except (OSError, ET.ParseError) as e:
        err = e
    try:
        page = fetch(f"https://www.youtube.com/channel/{ch['id']}/videos").decode("utf-8", "replace")
        vids = parse_videos_page(page, title, now=now)
        if vids:
            cache_dir.mkdir(parents=True, exist_ok=True)
            fj.write_text(json.dumps({"videos": vids}))
            return vids
    except OSError:
        pass
    for p in (f, fj):                        # offline: a stale cache beats nothing
        try:
            return parse_feed(p.read_bytes(), title) if p == f else cached_page()
        except (OSError, ValueError, ET.ParseError):
            continue
    raise err


def choose(vids: list[dict], what: str | None, st: dict) -> dict | None:
    if not vids:
        return None
    w = (what or "").strip().lower()
    if not w or w in ("latest", "newest", "new", "next"):
        return next((v for v in vids if v["vid"] not in st["watched"]), vids[0] if w in ("latest", "newest") else None)
    if w.isdigit():
        i = int(w) - 1
        return vids[i] if 0 <= i < len(vids) else None
    toks = _norm(w).split()
    best, score = None, 0
    for v in vids:
        n = sum(1 for t in toks if t in _norm(v["title"]).split())
        if n > score:
            best, score = v, n
    return best


def upnext(st: dict, fetch=_get, cache_only=False, now=None, n=3) -> list[dict]:
    """Newest unwatched videos across followed channels, as up-next items for the face."""
    now = time.time() if now is None else now
    items = []
    for ch in st.get("channels", []):
        try:
            vids = videos(ch, fetch=fetch, cache_only=cache_only, now=now)
        except Exception:  # noqa: BLE001  (offline with no cache: skip the channel)
            continue
        for v in vids[:5]:
            if v["vid"] in st.get("watched", {}):
                continue
            items.append({"source": "youtube", "show": ch.get("title") or v["channel"], "title": v["title"],
                          "guid": "yt:" + v["vid"], "url": v["url"], "img": v["img"],
                          "date": round(v["date"]), "dur": 0,
                          "new": bool(v["date"]) and now - v["date"] < NEW_DAYS * 86400,
                          "resume": False, "pos": 0})
    items.sort(key=lambda i: i["date"], reverse=True)
    return items[:n]


def mark_watched(st: dict, vid: str, on: bool = True):
    if on:
        st["watched"][vid] = round(time.time())
        if len(st["watched"]) > 2000:          # keep the newest marks only
            st["watched"] = dict(sorted(st["watched"].items(), key=lambda kv: kv[1])[-1500:])
    else:
        st["watched"].pop(vid, None)


# ------------------------------------------------------------------ cli
def _date(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %b %-d") if ts else "?"


def _refresh_face():
    try:
        from . import podcast
        podcast.publish_upnext(cache_only=True)
    except Exception:  # noqa: BLE001
        pass


def _play(v: dict, st: dict, player=None) -> int:
    if player is None:
        from . import media
        player = media.cli
    code = player(["youtube", "url", v["url"]])
    if code == 0:
        mark_watched(st, v["vid"])
        save(st)
        _refresh_face()
    return code


def _split(st: dict, rest: list[str]) -> tuple[dict | None, str]:
    """[channel words] [latest|N|title words] -> (channel or None, what)."""
    words = " ".join(rest).strip()
    if not words:
        return None, ""
    toks = words.split()
    for k in range(len(toks), 0, -1):            # longest leading run that names a channel
        ch = find_channel(st, " ".join(toks[:k]))
        if ch and _norm(" ".join(toks[:k])) in (_norm(ch["title"]) + " " + _norm(ch.get("handle", ""))):
            return ch, " ".join(toks[k:])
    return None, words


def cli(argv: list[str], fetch=_get, player=None) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd, rest = argv[0], argv[1:]
    st = load()
    if cmd == "follow":
        term = " ".join(rest).strip()
        if not term:
            print('follow which channel? walterctl youtube follow "channel name"')
            return 2
        try:
            ch = resolve(term, fetch=fetch)
        except OSError as e:
            print(f"Couldn't reach YouTube ({e.__class__.__name__})")
            return 1
        if not ch:
            print(f"Couldn't find a YouTube channel called {term}")
            return 1
        st["channels"] = [c for c in st["channels"] if c["id"] != ch["id"]] + [ch]
        save(st)
        print(f"Following {ch['title']}" + (f" ({ch['handle']})" if ch["handle"] else "") + f"\nchannel: {ch['id']}")
        try:
            for i, v in enumerate(videos(ch, fetch=fetch)[:3], 1):
                print(f"  {i}. {v['title']} ({_date(v['date'])})")
        except Exception as e:  # noqa: BLE001
            print(f"  (couldn't read the feed yet: {e})")
        _refresh_face()
        return 0
    if cmd == "list":
        if not st["channels"]:
            print("Not following any YouTube channels yet")
        for c in st["channels"]:
            print(c["title"] + (f" ({c['handle']})" if c.get("handle") else ""))
        return 0
    if cmd == "unfollow":
        ch = find_channel(st, " ".join(rest))
        if not ch:
            print("No followed channel matches that")
            return 1
        st["channels"] = [c for c in st["channels"] if c is not ch]
        save(st)
        print(f"Unfollowed {ch['title']}")
        _refresh_face()
        return 0
    if cmd == "latest":
        n = 5
        if rest and rest[-1].isdigit():
            n = int(rest.pop())
        chans = [find_channel(st, " ".join(rest))] if rest else st["channels"]
        if not chans or chans[0] is None:
            print("Which channel? " + ", ".join(c["title"] for c in st["channels"]) if st["channels"]
                  else "Not following any YouTube channels yet")
            return 1
        for ch in chans:
            print(ch["title"])
            for i, v in enumerate(videos(ch, fetch=fetch)[:n], 1):
                print(f"  {i}. {v['title']} ({_date(v['date'])})" + (" [watched]" if v["vid"] in st["watched"] else ""))
        return 0
    if cmd == "upnext":
        items = upnext(st, fetch=fetch, n=int(rest[0]) if rest and rest[0].isdigit() else 5)
        if not items:
            print("Nothing new on YouTube: all caught up" if st["channels"] else "Not following any YouTube channels yet")
        for i, e in enumerate(items, 1):
            print(f"{i}. {e['show']}: {e['title']} ({_date(e['date'])})" + (" [new]" if e["new"] else ""))
        return 0
    if cmd in ("watched", "unwatched"):
        items = upnext(st, fetch=fetch, cache_only=True, n=10) if cmd == "watched" else []
        vid = rest[0] if rest else ""
        if vid.isdigit() and cmd == "watched":
            i = int(vid) - 1
            vid = items[i]["guid"][3:] if 0 <= i < len(items) else ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            print(f"walterctl youtube {cmd} N (from upnext) or a video id")
            return 2
        mark_watched(st, vid, cmd == "watched")
        save(st)
        _refresh_face()
        print(f"Marked {vid} {cmd}")
        return 0
    if cmd == "play":
        if not st["channels"]:
            print('Not following any YouTube channels yet: walterctl youtube follow "name"')
            return 1
        ch, what = _split(st, rest)
        if ch:
            v = choose(videos(ch, fetch=fetch), what, st)
        elif not what or what.lower() in ("latest", "newest", "new", "next"):
            nx = upnext(st, fetch=fetch, n=1)
            v = {"vid": nx[0]["guid"][3:], "url": nx[0]["url"], "title": nx[0]["title"]} if nx else None
        else:
            v = None
            for c in st["channels"]:              # title words across every channel
                v = choose(videos(c, fetch=fetch), what, st)
                if v:
                    break
        if not v:
            print("Nothing unwatched to play" + (f" on {ch['title']}" if ch else "")
                  + " (try: walterctl youtube latest)")
            return 1
        return _play(v, st, player)
    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
