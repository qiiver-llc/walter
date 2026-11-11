"""Dashboard mode: what the wall screen shows while Walter sleeps.

Three kinds of data, all kept out of git:
  * the person's list (tasks / notes / shopping): ~/.walter/dashboard.json, edited by
    bin/walter-dashboard, the phone page (/dashboard/add) and the face's type box;
    mirrored to face/dashboard-list.json for the face to poll;
  * weather: face/dashboard-weather.json (bin/walter-dashboard refresh, every 15 min
    from walter-dashboard.timer);
  * calendar events for this month's grid (month_window(): the Sunday on/before the
    1st to the Saturday on/after the last day): face/dashboard-events.json, written by
    Walter himself (bin/walter-dashboard set-events FILE.json). The today/tomorrow
    sections pick their two days; the portrait month calendar uses all of it.

Face files are written with a fixed old mtime so the face never reloads for them.
Stdlib only: imported by the server and by the CLI.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import ipaddress
import json
import os
import re
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
FACE_DIR = REPO_DIR / "face"
FIXED_MTIME = 946684800  # 2000-01-01: never looks like a face edit

SECTIONS = ("tasks", "shopping", "notes", "ideas", "inbox")   # inbox: said in passing, not sorted yet
MAX_TEXT = 160        # characters per item
MAX_ITEMS = 150       # items kept at once (done ones included)
MAX_EVENTS = 1000    # a month grid (up to 6 weeks) from four calendars
DONE_KEEP_S = 24 * 3600   # ticked-off items can be un-ticked for a day, then vanish
USER_AGENT = "walter-wall-assistant"


def data_dir() -> Path:
    return Path(os.environ.get("WALTER_DATA", Path.home() / ".walter"))


# ------------------------------------------------------------------ helpers
def write_face_json(path: Path, data) -> None:
    """Atomic write with a fixed mtime (so the face's auto-reload ignores it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False))
    os.utime(tmp, (FIXED_MTIME, FIXED_MTIME))
    os.replace(tmp, path)


def clean_text(s, limit: int = MAX_TEXT) -> str:
    """One line, no control characters, trimmed to `limit`."""
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


_PRIVATE_NETS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
    "::1/128", "fc00::/7", "fe80::/10")]


def is_private_ip(addr) -> bool:
    """True for loopback, RFC 1918, link-local and IPv6 ULA/link-local addresses."""
    if not addr:
        return False
    try:
        ip = ipaddress.ip_address(str(addr).split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in n for n in _PRIVATE_NETS if n.version == ip.version)


def is_loopback(addr) -> bool:
    try:
        ip = ipaddress.ip_address(str(addr).split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def lan_ip() -> str:
    """This machine's address on the home network ("" if offline). Sends nothing."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))   # TEST-NET: just picks the outgoing interface
        ip = s.getsockname()[0]
        return ip if is_private_ip(ip) and not is_loopback(ip) else ""
    except OSError:
        return ""
    finally:
        s.close()


# ------------------------------------------------------------------ due dates
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_due(s, today: dt.date | None = None) -> str:
    """'today', 'tomorrow', 'friday', '2026-11-06', '11/6', optionally followed by a time
    ('5pm', '17:30', '5:30 pm'), or a bare time (today). Returns ISO 'YYYY-MM-DD' or
    'YYYY-MM-DDTHH:MM'. Raises ValueError if it can't tell."""
    s = clean_text(s, 40).lower()
    if not s:
        return ""
    today = today or dt.date.today()
    date, rest = None, s
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[t ]\s*(.*))?$", s)
    if m:
        date = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        rest = m.group(4) or ""
    else:
        m = re.match(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b\s*(.*)$", s)
        if m:
            y = int(m.group(3)) if m.group(3) else today.year
            y = y + 2000 if y < 100 else y
            date = dt.date(y, int(m.group(1)), int(m.group(2)))
            if not m.group(3) and date < today:
                date = date.replace(year=y + 1)
            rest = m.group(4)
        else:
            word, _, rest = s.partition(" ")
            if word in ("today", "tonight"):
                date = today
                if word == "tonight" and not rest:
                    rest = "8pm"
            elif word == "tomorrow":
                date = today + dt.timedelta(days=1)
            elif len(word) >= 3 and any(w.startswith(word.rstrip(".")) for w in _WEEKDAYS):
                idx = next(i for i, w in enumerate(_WEEKDAYS) if w.startswith(word.rstrip(".")))
                date = today + dt.timedelta(days=(idx - today.weekday()) % 7 or 7)
            else:
                rest = s
    rest = rest.strip().removeprefix("at ").strip()
    tm = None
    if rest:
        m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?$", rest)
        if not m:
            raise ValueError(f"can't read the due date {s!r}")
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").replace(".", "")
        if ap and not 1 <= h <= 12:
            raise ValueError(f"can't read the time {rest!r}")
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        if h > 23 or mi > 59:
            raise ValueError(f"can't read the time {rest!r}")
        tm = dt.time(h, mi)
        if date is None:
            date = today
    if date is None:
        raise ValueError(f"can't read the due date {s!r}")
    return f"{date.isoformat()}T{tm.strftime('%H:%M')}" if tm else date.isoformat()


# ------------------------------------------------------------------ the list
_PREFIXES = {
    "todo": "tasks", "to do": "tasks", "task": "tasks", "tasks": "tasks",
    "note": "notes", "notes": "notes", "remember": "notes",
    "shopping": "shopping", "shop": "shopping", "grocery": "shopping", "groceries": "shopping",
    "idea": "ideas", "ideas": "ideas", "parked": "ideas",
    "inbox": "inbox",
}


def parse_quick(text: str) -> tuple[str, str]:
    """Typed shorthand: 'todo buy milk', 'note: gate code changed', 'shopping eggs',
    'add: call the vet'. Returns (section, text); no prefix means a task."""
    t = clean_text(text)
    t = re.sub(r"^add\b\s*:?\s*", "", t, flags=re.I)
    m = re.match(r"^(to do|todo|tasks?|notes?|remember|shopping|shop|groceries|grocery|ideas?|parked|inbox)\b\s*[:\-]?\s*(.+)$",
                 t, re.I)
    if m:
        return _PREFIXES[m.group(1).lower()], m.group(2).strip()
    return "tasks", t


# Typed on the dashboard (or the phone page): commands, not to-dos (5 Nov audit:
# "walter wake up" typed while asleep became a to-do item).
_COMMAND_RE = re.compile(
    r"^\W*(?:(?:hey|ok|okay)\s+)?(?:walter\W*)?(?:please\s+)?"
    r"(wake(?:\s*up)?|wakeup|go\s+to\s+sleep|sleep|un-?mute|mute)?\W*(?:please)?\W*$", re.I)


def parse_command(text) -> str | None:
    """'wake', 'walter wake up', 'sleep', 'mute', 'unmute' (and bare 'walter') ->
    "wake" | "sleep" | "mute" | "unmute"; anything else -> None."""
    t = clean_text(text, 60)
    if not t:
        return None
    m = _COMMAND_RE.match(t)
    if not m:
        return None
    w = (m.group(1) or "").lower()
    if not w:
        return "wake" if re.search(r"walter", t, re.I) else None
    if w.startswith("wake"):
        return "wake"
    if "sleep" in w:
        return "sleep"
    return "unmute" if w.startswith("un") else "mute"


class ListStore:
    """The person's own list in ~/.walter/dashboard.json (file-locked; CLI and server share it)."""

    def __init__(self, path: Path | None = None, face_out: Path | None = None, title: str = "My list"):
        self.path = Path(path) if path else data_dir() / "dashboard.json"
        self.face_out = Path(face_out) if face_out else FACE_DIR / "dashboard-list.json"
        self.title = title

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_name(self.path.name + ".lock"), "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            data = self._read()
            yield data
            self._purge(data)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        self.publish(data)

    def _read(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
            if isinstance(d, dict) and isinstance(d.get("items"), list):
                d.setdefault("next_id", 1 + max([i.get("id", 0) for i in d["items"]] or [0]))
                return d
        except (OSError, ValueError):
            pass
        return {"items": [], "next_id": 1}

    @staticmethod
    def _purge(data: dict, now: float | None = None):
        now = now or time.time()
        data["items"] = [i for i in data["items"]
                         if not i.get("done_at") or now - i["done_at"] < DONE_KEEP_S]

    def items(self) -> list[dict]:
        d = self._read()
        self._purge(d)
        return d["items"]

    def _find(self, data: dict, ref) -> dict:
        """ref: {'id': 7} or a 1-based number as printed by `list`."""
        items = data["items"]
        if isinstance(ref, dict):
            for i in items:
                if i["id"] == ref.get("id"):
                    return i
            raise KeyError("no such item")
        try:
            n = int(ref)
        except (TypeError, ValueError):
            raise KeyError("give the item's number") from None
        if not 1 <= n <= len(items):
            raise KeyError(f"no item number {n}")
        return items[n - 1]

    def add(self, text: str, section: str = "tasks", due: str = "") -> dict:
        text = clean_text(text)
        if not text:
            raise ValueError("text required")
        section = (section or "tasks").lower()
        if section in _PREFIXES:
            section = _PREFIXES[section]
        if section not in SECTIONS:
            raise ValueError(f"section must be one of {', '.join(SECTIONS)}")
        due = parse_due(due) if due else ""
        with self._locked() as data:
            if len(data["items"]) >= MAX_ITEMS:
                raise ValueError("the list is full; tick some things off first")
            item = {"id": data["next_id"], "text": text, "section": section,
                    "created": round(time.time(), 1)}
            if due:
                item["due"] = due
            data["next_id"] += 1
            data["items"].append(item)
        return item

    def done(self, ref, on: bool = True) -> dict:
        with self._locked() as data:
            item = self._find(data, ref)
            if on:
                item["done_at"] = item.get("done_at") or round(time.time(), 1)
            else:
                item.pop("done_at", None)
        return item

    def move(self, ref, section: str, text: str | None = None, due: str | None = None) -> dict:
        """Move an item to another section (the inbox review), optionally renaming it
        or setting a due date ("" clears it; None leaves it)."""
        section = _PREFIXES.get((section or "").lower(), (section or "").lower())
        if section not in SECTIONS:
            raise ValueError(f"section must be one of {', '.join(SECTIONS)}")
        due = parse_due(due) if due else due
        with self._locked() as data:
            item = self._find(data, ref)
            item["section"] = section
            if text is not None and clean_text(text):
                item["text"] = clean_text(text)
            if due is not None:
                if due:
                    item["due"] = due
                else:
                    item.pop("due", None)
        return item

    def remove(self, ref) -> dict:
        with self._locked() as data:
            item = self._find(data, ref)
            data["items"].remove(item)
        return item

    def publish(self, data: dict | None = None, extra: dict | None = None):
        """Mirror to the face (no ids needed there beyond display; nothing secret)."""
        items = (data or {"items": self.items()})["items"]
        out = {"title": self.title, "items": items, "changed_at": time.time()}
        try:
            prev = json.loads(self.face_out.read_text())
            for k in ("qr", "url", "quiet", "dark", "enabled", "show_until", "cal_colors", "verse"):
                if k in prev:
                    out[k] = prev[k]
        except (OSError, ValueError):
            pass
        out.update(extra or {})
        write_face_json(self.face_out, out)


def format_list(items: list[dict]) -> str:
    if not items:
        return "(the list is empty)"
    lines = []
    for n, i in enumerate(items, 1):
        box = "x" if i.get("done_at") else " "
        due = f"  (due {i['due'].replace('T', ' ')})" if i.get("due") else ""
        lines.append(f"{n:>3}. [{box}] {i['section']:<8} {i['text']}{due}")
    return "\n".join(lines)


# ------------------------------------------------------------------ cards
CARD_OUT: Path | None = None   # tests point this away from the real face/card.json


def show_card(title: str, body: str = "", seconds: float = 30, out: Path | None = None) -> dict:
    """A card on the face (same file as bin/walter-show): `seconds` 0 = until cleared."""
    card = {"id": time.time(), "title": clean_text(title, 80), "body": str(body or "")[:1200],
            "until": (time.time() + seconds) if seconds > 0 else 0}
    write_face_json(Path(out) if out else (CARD_OUT or FACE_DIR / "card.json"), card)
    return card


# ------------------------------------------------------------------ while you were away
AWAY_MAX = 50           # entries kept in ~/.walter/away.json
AWAY_FACE_MAX = 6       # shown on the dashboard
AWAY_FACE_KEEP_S = 12 * 3600   # seen entries stay on the dashboard (dimmed) this long
_away_hooks: list = []  # fn(item) called after add_away (the server sends Web Push / app badges)


def on_away(fn) -> None:
    """Register fn(item) to run after every add_away in this process."""
    if fn not in _away_hooks:
        _away_hooks.append(fn)


class AwayFeed:
    """Things Walter couldn't say out loud (muted, asleep, nobody in the room):
    reminders, finished workers, routine results, unanswered requests.
    ~/.walter/away.json (mode 600, newest last, capped); mirrored to
    face/dashboard-away.json for the dashboard panel."""

    DEFAULT_PATH: Path | None = None   # tests: never the real ~/.walter
    DEFAULT_FACE: Path | None = None

    def __init__(self, path: Path | None = None, face_out: Path | None = None):
        self.path = Path(path) if path else (self.DEFAULT_PATH or data_dir() / "away.json")
        self.face_out = Path(face_out) if face_out else (self.DEFAULT_FACE or FACE_DIR / "dashboard-away.json")

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_name(self.path.name + ".lock"), "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            data = self._read()
            yield data
            data["items"] = data["items"][-AWAY_MAX:]
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        self.publish(data)

    def _read(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
            if isinstance(d, dict) and isinstance(d.get("items"), list):
                d.setdefault("next_id", 1 + max([i.get("id", 0) for i in d["items"]] or [0]))
                return d
        except (OSError, ValueError):
            pass
        return {"items": [], "next_id": 1}

    def items(self, unseen_only: bool = False) -> list[dict]:
        its = self._read()["items"]
        return [i for i in its if not i.get("seen")] if unseen_only else its

    def unseen(self) -> list[dict]:
        return self.items(unseen_only=True)

    def add(self, title: str, body: str = "", kind: str = "info") -> dict:
        title = clean_text(title, 80) or "Note"
        body = clean_text(body, 600)
        with self._locked() as data:
            last = data["items"][-1] if data["items"] else None
            if (last and not last.get("seen") and last["title"] == title and last["body"] == body
                    and time.time() - last["t"] < 120):
                return last   # the same thing twice (two code paths noticed it): keep one
            item = {"id": data["next_id"], "t": round(time.time(), 1), "title": title,
                    "body": body, "kind": clean_text(kind, 20) or "info", "seen": False}
            data["next_id"] += 1
            data["items"].append(item)
        return item

    def mark_seen(self, ids=None) -> int:
        """Mark these ids (or everything) seen; returns how many changed."""
        n = 0
        with self._locked() as data:
            for i in data["items"]:
                if not i.get("seen") and (ids is None or i["id"] in ids):
                    i["seen"] = True
                    i["seen_at"] = round(time.time(), 1)
                    n += 1
        return n

    def clear(self) -> int:
        with self._locked() as data:
            n = len(data["items"])
            data["items"] = []
        return n

    def publish(self, data: dict | None = None):
        items = (data or self._read())["items"]
        now = time.time()
        show = [i for i in items if not i.get("seen") or now - i.get("seen_at", i["t"]) < AWAY_FACE_KEEP_S]
        write_face_json(self.face_out, {"items": show[-AWAY_FACE_MAX:][::-1],
                                        "unseen": sum(1 for i in items if not i.get("seen")),
                                        "changed_at": now})


def add_away(title: str, body: str = "", kind: str = "info", push: bool = True,
             feed: AwayFeed | None = None) -> dict:
    """Record something Walter couldn't say (stable API: other modules call this).
    push=True also lets the server's hooks send it to paired phones (Web Push)."""
    item = (feed or AwayFeed()).add(title, body, kind)
    for fn in list(_away_hooks):
        try:
            fn(dict(item, push=push))
        except Exception:  # a hook must never lose the entry
            pass
    return item


def format_away(items: list[dict]) -> str:
    if not items:
        return "(nothing while you were away)"
    out = []
    for i in items:
        when = dt.datetime.fromtimestamp(i["t"]).strftime("%a %I:%M %p").replace(" 0", " ")
        mark = " " if i.get("seen") else "*"
        out.append(f"{mark} #{i['id']:<3} {when}  {i['title']}" + (f": {i['body']}" if i.get("body") else ""))
    return "\n".join(out)


def away_card_body(items: list[dict], limit: int = 5) -> str:
    """The wall card's text: one line per thing, newest first."""
    lines = []
    for i in list(reversed(items))[:limit]:
        when = dt.datetime.fromtimestamp(i["t"]).strftime("%I:%M %p").lstrip("0")
        body = clean_text(i.get("body"), 110)
        lines.append(f"{when}  {i['title']}" + (f": {body}" if body else ""))
    if len(items) > limit:
        lines.append(f"+ {len(items) - limit} more on the dashboard")
    return "\n".join(lines)


# ------------------------------------------------------------------ events
def _iso(s, field_name: str) -> str:
    s = str(s or "").strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
            dt.date.fromisoformat(s)
        else:
            dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{field_name}: not an ISO date/time: {s!r}") from None
    return s


def validate_events(raw) -> list[dict]:
    """[{title, start, end?, calendar?, all_day?, location?}] -> cleaned list, sorted by start."""
    if isinstance(raw, dict):
        raw = raw.get("events", [])
    if not isinstance(raw, list):
        raise ValueError("expected a JSON list of events (or {\"events\": [...]})")
    out = []
    for e in raw[:MAX_EVENTS]:
        if not isinstance(e, dict):
            raise ValueError("each event must be an object")
        title = clean_text(e.get("title") or e.get("summary"), 100)
        if not title:
            raise ValueError("each event needs a title")
        start = _iso(e.get("start"), "start")
        ev = {"title": title, "start": start,
              "all_day": bool(e.get("all_day", len(start) == 10))}
        if e.get("end"):
            ev["end"] = _iso(e["end"], "end")
        for k, lim in (("calendar", 30), ("location", 60)):
            if e.get(k):
                ev[k] = clean_text(e[k], lim)
        if isinstance(e.get("outdoor"), bool):   # explicit override from the events JSON
            ev["outdoor_set"] = e["outdoor"]
        out.append(ev)

    def key(ev):
        s = ev["start"]
        if len(s) == 10:
            return dt.datetime.fromisoformat(s).timestamp() - 1   # all-day first
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d.timestamp()
    out.sort(key=key)
    return out


def _kw_pattern(keywords) -> re.Pattern | None:
    words = [re.escape(str(k).strip().lower()) for k in (keywords or []) if str(k).strip()]
    if not words:
        return None
    words.sort(key=len, reverse=True)
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(words) + r")(?:e?s)?(?![a-z0-9])")


def is_outdoor(ev: dict, keywords=None) -> bool:
    """An event's own "outdoor" override wins; else a keyword in its title or location."""
    if isinstance(ev.get("outdoor_set"), bool):
        return ev["outdoor_set"]
    pat = _kw_pattern(_default_keywords() if keywords is None else keywords)
    text = (str(ev.get("title") or "") + " " + str(ev.get("location") or "")).lower()
    return bool(pat and pat.search(text))


def _default_keywords() -> list[str]:
    from walter.config import DEFAULT_OUTDOOR_KEYWORDS
    return list(DEFAULT_OUTDOOR_KEYWORDS)


def tag_outdoor(events: list[dict], keywords=None) -> list[dict]:
    """Set ev["outdoor"] on each event (the face shows a weather chip next to those)."""
    for ev in events:
        ev["outdoor"] = is_outdoor(ev, keywords)
    return events


def retag_events(keywords=None, path: Path | None = None) -> int:
    """Re-apply the keyword rule to the stored events (config edits show up at the next refresh)."""
    path = Path(path) if path else FACE_DIR / "dashboard-events.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return 0
    evs = data.get("events") if isinstance(data, dict) else None
    if not isinstance(evs, list):
        return 0
    before = [bool(e.get("outdoor")) for e in evs]
    tag_outdoor(evs, keywords)
    if before != [bool(e.get("outdoor")) for e in evs]:
        write_face_json(path, data)
    return sum(1 for e in evs if e.get("outdoor"))


def month_window(today: dt.date | None = None) -> tuple[dt.date, dt.date]:
    """The month calendar's grid: (first Sunday, day after the last Saturday), both dates.
    Sunday on/before the 1st to the Saturday on/after the month's last day (4-6 rows)."""
    today = today or dt.date.today()
    first = today.replace(day=1)
    nxt = (first + dt.timedelta(days=32)).replace(day=1)
    start = first - dt.timedelta(days=(first.weekday() + 1) % 7)
    last = nxt - dt.timedelta(days=1)
    end = last + dt.timedelta(days=(5 - last.weekday()) % 7 + 1)
    return start, end


def event_days(events: list[dict]) -> int:
    """How many calendar days the events span, first start to last start (0 if none)."""
    days = []
    for e in events:
        s = str(e.get("start") or "")
        try:
            days.append(dt.date.fromisoformat(s[:10]))
        except ValueError:
            continue
    return (max(days) - min(days)).days + 1 if days else 0


def set_events(raw, out: Path | None = None, keywords=None) -> list[dict]:
    events = tag_outdoor(validate_events(raw), keywords)
    write_face_json(Path(out) if out else FACE_DIR / "dashboard-events.json",
                    {"updated": time.time(), "events": events})
    return events


# ------------------------------------------------------------------ weather
WMO = {0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy", 45: "Fog", 48: "Freezing fog",
       51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
       61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
       71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
       80: "Showers", 81: "Showers", 82: "Heavy showers", 85: "Snow showers", 86: "Snow showers",
       95: "Thunderstorms", 96: "Storms with hail", 99: "Storms with hail"}


def _get_json(url: str, headers: dict | None = None, timeout: float = 15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(2_000_000))


def compass(deg) -> str:
    if deg is None:
        return ""
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((float(deg) % 360) / 45 + 0.5) % 8]


def parse_forecast(f: dict) -> dict:
    cur, daily = f.get("current") or {}, f.get("daily") or {}

    def day(i):
        try:
            code = daily["weather_code"][i]
            return {"date": daily["time"][i], "high": daily["temperature_2m_max"][i],
                    "low": daily["temperature_2m_min"][i],
                    "rain": daily.get("precipitation_probability_max", [None] * 3)[i],
                    "code": code, "text": WMO.get(code, ""),
                    "sunrise": (daily.get("sunrise") or [None] * 3)[i],
                    "sunset": (daily.get("sunset") or [None] * 3)[i]}
        except (KeyError, IndexError, TypeError):
            return None
    code = cur.get("weather_code")
    return {"temp": cur.get("temperature_2m"), "feels": cur.get("apparent_temperature"),
            "code": code, "text": WMO.get(code, ""), "is_day": cur.get("is_day", 1),
            "wind": cur.get("wind_speed_10m"), "gust": cur.get("wind_gusts_10m"),
            "wind_dir": compass(cur.get("wind_direction_10m")),
            "today": day(0), "tomorrow": day(1), "days": parse_days(daily),
            "hourly": parse_hourly(f.get("hourly") or {})}


def parse_days(daily: dict, n: int = 16) -> list[dict]:
    """Open-Meteo daily block -> up to n compact days for the month calendar's day cells
    (portrait): date, high, low, weather code, max rain chance (%). Days with no
    date are dropped; a missing value stays None."""
    try:
        dates = list(daily["time"])[:n]
    except (KeyError, TypeError):
        return []
    col = lambda k: (list(daily.get(k) or []) + [None] * n)[:len(dates)]  # noqa: E731
    out = []
    for d, hi, lo, code, rain in zip(dates, col("temperature_2m_max"), col("temperature_2m_min"),
                                     col("weather_code"), col("precipitation_probability_max")):
        if d:
            out.append({"date": d, "high": hi, "low": lo, "code": code, "rain": rain})
    return out


def parse_hourly(h: dict) -> dict:
    """Open-Meteo hourly block -> compact columns for the face's outdoor-event chips.
    Times are local wall-clock hours ("2026-11-06T14:00"), like the daily dates.
    Only today + tomorrow (48 h) are kept: the fetch asks for 16 days for the calendar."""
    try:
        t = list(h["time"])[:48]
        n = len(t)
        col = lambda k: list(h.get(k) or [None] * n)[:n]  # noqa: E731
        return {"time": t, "temp": col("temperature_2m"), "rain": col("precipitation_probability"),
                "code": col("weather_code"), "is_day": col("is_day")}
    except (KeyError, TypeError):
        return {}


def parse_alerts(a: dict) -> list[dict]:
    out = []
    for f in (a.get("features") or [])[:5]:
        p = f.get("properties") or {}
        if p.get("status", "Actual") != "Actual":
            continue
        out.append({"event": clean_text(p.get("event"), 60), "severity": p.get("severity") or "",
                    "headline": clean_text(p.get("headline"), 160),
                    "ends": p.get("ends") or p.get("expires") or ""})
    return out


def fetch_weather(lat: float, lon: float) -> dict:
    q = urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,apparent_temperature,weather_code,is_day,wind_speed_10m,"
                   "wind_gusts_10m,wind_direction_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                 "sunrise,sunset",
        "hourly": "temperature_2m,precipitation_probability,weather_code,is_day",
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "auto",
        "forecast_days": 16})
    out = {"updated": time.time(), "ok": True}
    out.update(parse_forecast(_get_json("https://api.open-meteo.com/v1/forecast?" + q)))
    try:
        out["alerts"] = parse_alerts(_get_json(
            f"https://api.weather.gov/alerts/active?point={lat:.4f},{lon:.4f}",
            {"Accept": "application/geo+json"}))
    except Exception as e:  # alerts are a bonus: keep the forecast
        out["alerts"], out["alerts_error"] = [], str(e)[:120]
    return out
