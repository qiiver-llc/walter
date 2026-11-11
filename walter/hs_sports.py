"""High school teams for the portrait sports row (Nebraska, from the NSAA's public pages).

ESPN has no high school data, so for [dashboard] hs_teams we read the Nebraska School
Activities Association's own public pages (no login; robots.txt allows them, crawl-delay 5):

  season   secure.nsaahome.org/wildcards/schedules/index.php?sport=fb&year=2026&class=B&sid=N
           every regular-season game (and districts for volleyball): date, opponent, W/L, score.
           No kick-off times, so a future game from here shows "TBD".
  playoffs football: secure.nsaahome.org/nsaaforms/officials/fbbracket.php?class=B (an HTML
           bracket with dates + times; a result is read from the next round, where the
           winner appears with that game's score).
           volleyball state: the NSAA's published Google Sheet bracket (found on
           nsaahome.org/nsaa-volleyball-championships-central/, or `bracket` in the config),
           fetched as CSV.

Config entry (TOML inline table), e.g.
  { sport = "football", school = "Springfield", class = "C1", sid = 101, abbr = "SPR" }
optional: name (shown name), color ("1f4e9a"), schedule / bracket (URL overrides),
year (school year start, default this season). `walter-dashboard hs-find SCHOOL` finds
class + sid.

Polite: every URL is cached on disk (~/.walter/cache/hs) and fetched at most every 30 min
(the season page every 6 h once the playoffs start), 5 s apart, only in season, with the
repo's generic User-Agent. Any failure (offline, page changed) keeps the last good entry
(sports.refresh) and never raises past fetch_team(). Stdlib only.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import html as H
import io
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

from walter import dashboard as D
from walter import sports as SP     # (sports imports this module lazily, inside refresh)

CT = ZoneInfo("America/Chicago")
MT = ZoneInfo("America/Denver")
SCHED = "https://secure.nsaahome.org/wildcards/schedules/index.php"
FB_BRACKET = "https://secure.nsaahome.org/nsaaforms/officials/fbbracket.php?class={cls}"
VB_CENTRAL = "https://nsaahome.org/nsaa-volleyball-championships-central/"
SPORTS = {"football": "fb", "fb": "fb", "volleyball": "vb", "vb": "vb"}
TAG = {"fb": "FB", "vb": "VB"}
# (month, day) windows: no network and no row outside them
SEASON = {"fb": ((8, 15), (11, 30)), "vb": ((8, 15), (11, 15))}
POSTSEASON = {"fb": (10, 20), "vb": (10, 25)}      # start looking at brackets from here
DEFAULT_HOUR = {"fb": 19, "vb": 18}                 # placeholder time for date-only games (tbd)
TTL_S = 30 * 60
TTL_SEASON_LATE_S = 6 * 3600
TTL_DISCOVER_S = 12 * 3600
GAP_S = 5                                           # NSAA robots.txt crawl-delay
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                      "sep", "oct", "nov", "dec"], 1)}


# ------------------------------------------------------------------ config + fetching
def parse_cfg(c) -> dict | None:
    """A config entry -> normalised dict, or None if unusable."""
    if not isinstance(c, dict):
        return None
    sp = SPORTS.get(str(c.get("sport") or "").lower().strip())
    school = D.clean_text(c.get("school"), 60)
    if not sp or not school:
        return None
    cls = re.sub(r"[^A-Za-z0-9]", "", str(c.get("class") or "")).upper()
    sid = re.sub(r"\D", "", str(c.get("sid") or ""))
    if not c.get("schedule") and not (cls and sid):
        return None
    abbr = re.sub(r"[^A-Z0-9]", "", str(c.get("abbr") or "").upper())[:5] or abbr_for(school)
    color = str(c.get("color") or "").lower().lstrip("#")
    return {"sport": sp, "school": school, "cls": cls, "sid": sid, "abbr": abbr,
            "name": D.clean_text(c.get("name") or school, 30),
            "color": color if re.fullmatch(r"[0-9a-f]{6}", color) else "",
            "schedule": str(c.get("schedule") or ""), "bracket": str(c.get("bracket") or ""),
            "year": int(c["year"]) if str(c.get("year") or "").isdigit() else None,
            "spec": f"nsaa/{sp}/{cls or 'x'}/{sid or school.lower()}"}


def season_year(now: float) -> int:
    d = dt.datetime.fromtimestamp(now, CT)
    return d.year if d.month >= 7 else d.year - 1


def _md(now: float) -> tuple[int, int]:
    d = dt.datetime.fromtimestamp(now, CT)
    return d.month, d.day


def in_season(sp: str, now: float) -> bool:
    a, b = SEASON[sp]
    return a <= _md(now) <= b


def postseason(sp: str, now: float) -> bool:
    return in_season(sp, now) and _md(now) >= POSTSEASON[sp]


def cache_dir() -> Path:
    return D.data_dir() / "cache" / "hs"


class Fetcher:
    """GET with an on-disk cache: a URL is fetched at most every `ttl` seconds; on a failed
    fetch the stale copy is used (and the error remembered). 5 s between network calls."""

    def __init__(self, now: float | None = None, get=None, cache: Path | None = None):
        self.now = now or time.time()
        self.get = get or _http_get
        self.cache = Path(cache) if cache else cache_dir()
        self.last_net = 0.0
        self.errors: list[str] = []
        self.calls = 0

    def text(self, url: str, ttl: float = TTL_S) -> str | None:
        f = self.cache / (hashlib.sha1(url.encode()).hexdigest()[:20] + ".txt")
        try:
            if self.now - f.stat().st_mtime < ttl:
                return f.read_text(errors="replace")
        except OSError:
            pass
        try:
            wait = GAP_S - (time.time() - self.last_net)
            if self.last_net and wait > 0 and self.get is _http_get:
                time.sleep(wait)
            self.calls += 1
            body = self.get(url)
            self.last_net = time.time()
            if not body or len(body) < 200:
                raise ValueError("empty page")
            self.cache.mkdir(parents=True, exist_ok=True)
            f.write_text(body)
            os.utime(f, (self.now, self.now))           # age is measured on our clock
            return body
        except Exception as e:
            self.errors.append(f"{urllib.parse.urlsplit(url).path[-40:]}: {str(e)[:60]}")
            try:
                return f.read_text(errors="replace")      # stale is better than nothing
            except OSError:
                return None


def _http_get(url: str, timeout: float = 15) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": D.USER_AGENT, "Accept": "text/html,text/csv,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(2_000_000).decode("utf-8", "replace")


# ------------------------------------------------------------------ names
_DROP_FRONT = {"omaha", "lincoln", "bishop", "saint", "st", "st.", "the"}
_DROP_BACK = {"catholic", "academy", "high", "school", "public", "hs", "christian"}


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", H.unescape(str(name or "")).lower()).strip()


def abbr_for(name: str) -> str:
    """'Omaha Skutt Catholic' -> 'SKU', 'Bishop Neumann' -> 'NEU', 'Gretna East' -> 'GE',
    'Laurel-Concord-Coleridge' -> 'LCC'."""
    raw = H.unescape(str(name or "")).strip()
    if re.fullmatch(r"[A-Za-z]+(-[A-Za-z]+){2,}", raw):
        return "".join(p[0] for p in raw.split("-")).upper()[:4]
    w = [x for x in re.split(r"[\s/]+", raw) if x]
    while len(w) > 1 and w[0].lower() in _DROP_FRONT:
        w = w[1:]
    while len(w) > 1 and w[-1].lower() in _DROP_BACK:
        w = w[:-1]
    w = [re.sub(r"[^A-Za-z0-9]", "", x) for x in w if re.sub(r"[^A-Za-z0-9]", "", x)]
    if not w:
        return "?"
    if len(w) == 1:
        return w[0][:3].upper()
    return "".join(x[0] for x in w).upper()[:4]


def same(a: str, b: str) -> bool:
    """Loose school-name match ('North Star' ~ 'Lincoln North Star')."""
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    return a == b or (" " + b + " ").find(" " + a + " ") >= 0 or (" " + a + " ").find(" " + b + " ") >= 0


# ------------------------------------------------------------------ times
def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _ts(iso: str) -> float:
    try:
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def _date(month: str, day: str, year: int) -> dt.date | None:
    m = MONTHS.get(month[:3].lower())
    try:
        return dt.date(year, m, int(day)) if m else None
    except ValueError:
        return None


def _at(day: dt.date, hour: int, minute: int = 0, tz=CT) -> str:
    return _iso(dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz))


def _clock(s: str) -> tuple[int, int, object] | None:
    """'7:00 PM cst' / '3 PM' -> (19, 0, tz)."""
    m = re.search(r"(\d{1,2})(?::(\d\d))?\s*([ap])\.?m\.?\s*([a-z]{1,3})?", s or "", re.I)
    if not m:
        return None
    h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
    tz = MT if (m.group(4) or "").lower().startswith("m") else CT
    return h, int(m.group(2) or 0), tz


# ------------------------------------------------------------------ season page
def _cells(row: str) -> list[str]:
    return [D.clean_text(H.unescape(re.sub(r"<[^>]+>", " ", c)), 80)
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]


def parse_schedule(page: str, sp: str, year: int) -> tuple[list[dict], str]:
    """NSAA season page -> (games, record). Games: start (date only, tbd), them, home, result."""
    rec = ""
    m = re.search(r"<h3[^>]*>\s*[^<(]*?\((\d+-\d+(?:-\d+)?)\)\s*</h3>", page or "")
    if m:
        rec = m.group(1)
    games = []
    body = (page or "").split("<tbody", 1)[-1]
    for i, row in enumerate(re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S | re.I)):
        c = _cells(row)
        if len(c) < 7:
            continue                                   # tournament header rows etc.
        dm = re.match(r"\w{3}\s+(\d{1,2})\s+([A-Za-z]{3})", c[0])
        if not dm:
            continue
        day = _date(dm.group(2), dm.group(1), year if MONTHS.get(dm.group(2)[:3].lower(), 12) >= 7 else year + 1)
        if not day:
            continue
        opp = c[1]
        away = opp.startswith("@")
        opp = opp.lstrip("@ ").strip()
        if not opp:
            continue
        wl = c[5].strip().upper()[:1]
        sc = re.match(r"\s*(\d+)\s*\D\s*(\d+)", c[6])
        g = {"id": f"s{day.isoformat()}-{norm(opp)[:12]}", "start": _at(day, DEFAULT_HOUR[sp], min(i, 59)),
             "tbd": True, "home": not away, "us": {"abbr": "", "score": None},
             "them": {"abbr": abbr_for(opp), "score": None, "name": opp}, "state": "pre", "src": "season"}
        if wl in ("W", "L", "T") and sc:
            g.update(state="post", result=wl, detail="Final")
            g["us"]["score"], g["them"]["score"] = int(sc.group(1)), int(sc.group(2))
        games.append(g)
    return games, rec


# ------------------------------------------------------------------ football bracket (HTML)
def _li_name(txt: str) -> tuple[str, int | None, int | None, bool]:
    """'#4 Springfield (8-1) H 42.4 pts' / 'Springfield 20-0 H' -> (name, score, opp score, home)."""
    t = D.clean_text(H.unescape(re.sub(r"<[^>]+>", " ", txt)).replace("\xa0", " "), 120)
    t = re.sub(r"\s*[\d.]+\s*pts\s*$", "", t)
    home = bool(re.search(r"\sH$", t))
    t = re.sub(r"\s+H$", "", t)
    t = re.sub(r"^#\d+\s+", "", t)
    a = b = None
    m = re.search(r"\s+\(?(\d+)-(\d+)\)?$", t)
    if m:
        if "(" not in m.group(0):
            a, b = int(m.group(1)), int(m.group(2))
        t = t[:m.start()]
    return t.strip(), a, b, home


def parse_fb_bracket(page: str, school: str, year: int) -> list[dict]:
    cards = {}
    for m in re.finditer(r'<div id="fb-(\d+-\d+)"([^>]*)>(.*?)</ul>', page or "", re.S):
        cid, attrs, inner = m.groups()
        feeds = re.search(r'data-feeds="([^"]*)"', attrs)
        hd = [D.clean_text(H.unescape(re.sub(r"<[^>]+>", "", x)), 60)
              for x in re.findall(r'<span class="fb-hd">(.*?)</span>', inner, re.S)]
        slots = {s: _li_name(x) for s, x in re.findall(r'data-team="(top|bot)"[^>]*>(.*?)</li>', inner, re.S)}
        cards[cid] = {"feeds": feeds.group(1) if feeds else "", "hd": hd, "slots": slots}
    champ = re.search(r'<div id="fb-w"[^>]*>(.*?)</div>', page or "", re.S)
    champ_txt = D.clean_text(H.unescape(re.sub(r"<[^>]+>", " ", champ.group(1))), 80) if champ else ""
    games = []
    for cid, c in cards.items():
        side = next((s for s, v in c["slots"].items() if same(v[0], school)), None)
        if not side:
            continue
        other = c["slots"].get("bot" if side == "top" else "top", ("", None, None, False))
        day = None
        for h in c["hd"]:
            dm = re.search(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})", h.split(",", 1)[-1])
            if dm and _date(dm.group(1), dm.group(2), year):
                day = _date(dm.group(1), dm.group(2), year)
                break
        if not day:
            continue
        clk = next((_clock(h) for h in c["hd"] if _clock(h)), None)
        neutral = any(h.startswith("@") for h in c["hd"])
        opp = other[0]
        g = {"id": f"b{cid}", "start": _at(day, clk[0], clk[1], clk[2]) if clk else _at(day, DEFAULT_HOUR["fb"]),
             "tbd": not clk, "home": c["slots"][side][3] or neutral, "state": "pre", "src": "bracket",
             "us": {"abbr": "", "score": None},
             "them": {"abbr": abbr_for(opp) if opp else "TBD", "score": None, "name": opp}}
        # the result: who shows up in the next round (with this game's score)
        nxt = None
        if c["feeds"] and c["feeds"] != "w" and ":" in c["feeds"]:
            tid, ts = c["feeds"].split(":", 1)
            nxt = (cards.get(tid) or {}).get("slots", {}).get(ts)
        elif c["feeds"] == "w" and champ_txt and not re.search(r"\bchampion\b", champ_txt, re.I):
            nxt = _li_name(champ_txt)
        if opp and nxt and nxt[0]:
            if same(nxt[0], school) and nxt[1] is not None:
                g.update(state="post", result="W", detail="Final")
                g["us"]["score"], g["them"]["score"] = nxt[1], nxt[2]
            elif same(nxt[0], opp) and nxt[1] is not None:
                g.update(state="post", result="L", detail="Final")
                g["us"]["score"], g["them"]["score"] = nxt[2], nxt[1]
        games.append(g)
    return games


# ------------------------------------------------------------------ volleyball state (sheet CSV)
_SEED = re.compile(r"^#(\d+)\s+(.+?)\s*\((\d+-\d+)\)")
_WHEN = re.compile(r"\b([A-Z][a-z]{2})[a-z]*\.?\s*(\d{1,2})\s*[–—-]\s*(\d{1,2}(?::\d\d)?\s*[AaPp]\.?[Mm])")
_SETS = re.compile(r"^\s*\d{1,2}\s*-\s*\d{1,2}(\s*,\s*\d{1,2}\s*-\s*\d{1,2})*\s*,?\s*$")


def sheet_csv_url(url: str) -> str:
    """A published Google Sheet link (pubhtml) -> its CSV export."""
    u = url.strip()
    if "docs.google.com/spreadsheets" in u and "output=csv" not in u:
        u = re.sub(r"/pub(html)?(\?.*)?$", "", u.split("#")[0]) + "/pub?output=csv"
    return u


def discover_vb_sheet(page: str) -> str:
    for m in re.finditer(r'href="(https://docs\.google\.com/spreadsheets/[^"]+)"[^>]*>(.*?)</a>', page or "", re.S):
        if re.search(r"bracket", m.group(2), re.I):
            return H.unescape(m.group(1))
    return ""


def parse_vb_sheet(text: str, school: str, year: int) -> list[dict]:
    """The NSAA's state bracket sheet: a grid where each round is a column. A game is two
    team cells in one column with a 'Nov. 6 - 3 PM' cell between them; the winner is written
    in the next column between those rows, followed by the set scores."""
    try:
        grid = [[(x or "").strip() for x in r] for r in csv.reader(io.StringIO(text or ""))]
    except csv.Error:
        return []
    known = {}
    for r in grid:
        for x in r:
            m = _SEED.match(x)
            if m:
                known[norm(m.group(2))] = m.group(2)

    def team(x):                          # a team cell -> its name, else None
        if not x or _WHEN.search(x) or _SETS.match(x):
            return None
        m = _SEED.match(x)
        if m:
            return m.group(2)
        if len(x) > 40 or re.search(r"\b(match|court|champion|place|class)\b", x, re.I):
            return None
        n = norm(x)
        return next((v for k, v in known.items() if k == n), None) or \
            next((v for k, v in known.items() if same(n, k)), None)

    ncols = max((len(r) for r in grid), default=0)
    col = lambda k: [(i, r[k]) for i, r in enumerate(grid) if k < len(r) and r[k]]
    games = []
    for k in range(ncols):
        cells = col(k)
        ents = [(i, team(x)) for i, x in cells if team(x)]
        for j, (i, name) in enumerate(ents):
            if not same(name, school):
                continue
            for nb in (ents[j - 1] if j else None, ents[j + 1] if j + 1 < len(ents) else None):
                if not nb:
                    continue
                lo, hi = sorted((i, nb[0]))
                when = next((_WHEN.search(x) for r_, x in cells if lo < r_ < hi and _WHEN.search(x)), None)
                if not when or hi - lo > 12:
                    continue
                day = _date(when.group(1), when.group(2), year)
                clk = _clock(when.group(3))
                if not day:
                    continue
                opp = nb[1]
                g = {"id": f"v{day.isoformat()}-{k}", "start": _at(day, clk[0], clk[1]) if clk else _at(day, 18),
                     "tbd": not clk, "home": True, "state": "pre", "src": "bracket",
                     "us": {"abbr": "", "score": None},
                     "them": {"abbr": abbr_for(opp), "score": None, "name": opp}}
                # winner: next column, between the two rows, followed by set scores
                nxt = col(k + 1) if k + 1 < ncols else []
                for r_, x in nxt:
                    if not (lo <= r_ <= hi) or not team(x):
                        continue
                    sets = []
                    for r2, y in nxt:
                        if r2 > r_ and r2 <= r_ + 3 and _SETS.match(y):
                            sets += re.findall(r"(\d+)\s*-\s*(\d+)", y)
                        elif r2 > r_ and sets:
                            break
                    if not sets:
                        continue
                    w = sum(int(a) > int(b) for a, b in sets)
                    l = len(sets) - w
                    if same(team(x), school):
                        g.update(state="post", result="W", detail="Final")
                        g["us"]["score"], g["them"]["score"] = w, l
                    elif same(team(x), opp):
                        g.update(state="post", result="L", detail="Final")
                        g["us"]["score"], g["them"]["score"] = l, w
                    break
                games.append(g)
                break
    return games


# ------------------------------------------------------------------ one team
def fetch_team(c: dict, fx: Fetcher) -> dict | None:
    """One configured team -> the face entry (same shape as sports.summarize), or None when
    out of season. Raises only if nothing at all could be read."""
    now = fx.now
    sp = c["sport"]
    if not in_season(sp, now):
        return None
    year = c["year"] or season_year(now)
    late = postseason(sp, now)
    url = c["schedule"] or f"{SCHED}?" + urllib.parse.urlencode(
        {"sport": sp, "year": year, "class": c["cls"], "sid": c["sid"]})
    page = fx.text(url, TTL_SEASON_LATE_S if late else TTL_S)
    games, rec = parse_schedule(page or "", sp, year)
    br = []
    if late:
        try:
            if sp == "fb":
                bu = c["bracket"] or (FB_BRACKET.format(cls=urllib.parse.quote(c["cls"])) if c["cls"] else "")
                if bu:
                    br = parse_fb_bracket(fx.text(bu) or "", c["school"], year)
            else:
                bu = c["bracket"] or discover_vb_sheet(fx.text(VB_CENTRAL, TTL_DISCOVER_S) or "")
                if bu:
                    br = parse_vb_sheet(fx.text(sheet_csv_url(bu)) or "", c["school"], year)
        except Exception as e:                      # a changed bracket must not lose the season
            fx.errors.append(f"bracket: {str(e)[:60]}")
    if page is None and not br:
        raise OSError("; ".join(fx.errors[-2:]) or "no data")
    days = {g["start"][:10] for g in br}
    evs = sorted([g for g in games if g["start"][:10] not in days] + br, key=lambda g: _ts(g["start"]))
    for g in evs:
        g["us"]["abbr"] = c["abbr"]
    entry = {"league": "nsaa-" + sp, "abbr": c["abbr"], "tag": TAG[sp], "name": c["name"],
             "color": c["color"], "record": rec, "live": None, "last": None, "next": None,
             "spec": c["spec"], "src": "nsaa"}
    done = [g for g in evs if g["state"] == "post" and _ts(g["start"]) <= now + 3600]
    if done:
        entry["last"] = done[-1]
    # next: the first unplayed game that hasn't long started (a game today stays "next"
    # until its result is posted, for up to 4 hours)
    nxt = [g for g in evs if g["state"] == "pre" and _ts(g["start"]) > now - 4 * 3600
           and (not entry["last"] or _ts(g["start"]) > _ts(entry["last"]["start"]))]
    if nxt:
        entry["next"] = nxt[0]
    entry["games"] = SP.games_window(evs, now)
    return entry


def find_school(name: str, get=None, sports=("fb", "vb"), year: int | None = None) -> list[dict]:
    """Search the NSAA schedule pages for a school: [{sport, class, sid, school}]. Slow (one
    polite request per class)."""
    get = get or _http_get
    year = year or season_year(time.time())
    out = []
    for sp in sports:
        for cls in ("A", "B", "C1", "C2", "D1", "D2", "D3", "D6"):
            if sp == "vb" and cls in ("D3", "D6"):
                continue
            try:
                page = get(f"{SCHED}?" + urllib.parse.urlencode({"sport": sp, "year": year, "class": cls}))
            except Exception:
                continue
            finally:
                get is _http_get and time.sleep(GAP_S)
            sel = page.split('name="sid"', 1)[-1].split("</select>", 1)[0]
            for sid, label in re.findall(r"<option[^>]*value='(\d+)'>([^<]+)", sel):
                if same(name, label.strip()) or norm(name) in norm(label):
                    out.append({"sport": "football" if sp == "fb" else "volleyball", "class": cls,
                                "sid": int(sid), "school": label.strip()})
    return out
