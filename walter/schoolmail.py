"""School / sports / church inbox -> PROPOSED calendar items.

Read-only by design:
  * this module never talks to Gmail or Google Calendar itself. The brain fetches
    messages with its read-only Gmail tools, saves them as JSON (or the tool's text
    output) and hands the file to `walterctl school scan FILE`;
  * scanning only *proposes* dated items (a card "Add these 3?" + a pending list in
    ~/.walter/school-proposals.json);
  * `walterctl school accept ... --yes` (the person said yes out loud) adds them to the
    person's list and PRINTS the calendar events for the brain to create. Nothing is
    ever created, sent or replied to here.

Email text is untrusted data: titles are cleaned (no links, addresses, tags or control
characters), clipped, and the CLI wraps them in <untrusted> when it prints them.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import email.utils
import fcntl
import json
import os
import re
from pathlib import Path

from walter.dashboard import data_dir

MAX_PROPOSALS = 8          # per scan (a card has to stay readable)
MAX_PENDING = 20
MAX_SEEN = 500             # message ids remembered so heartbeats don't re-propose
MAX_DECIDED = 300
HORIZON_DAYS = 120         # ignore dates further out than this
TITLE_MAX = 70

GMAIL_QUERY = ('newer_than:3d -in:sent -category:promotions ('
               'school OR teacher OR classroom OR PTA OR PTO OR "field trip" OR "picture day" OR '
               '"no school" OR "early dismissal" OR "late start" OR conference OR concert OR rehearsal OR '
               'practice OR game OR tournament OR meet OR volleyball OR football OR basketball OR '
               'wrestling OR "4-H" OR FFA OR church OR "youth group" OR "Sunday school" OR "permission slip")')

_KEYWORDS = re.compile(
    r"\b(school|teacher|class(room)?|pta|pto|field trip|picture day|no school|early (dismissal|out)|"
    r"late start|conferences?|concert|recital|rehearsal|choir|band|practice|games?|tournament|"
    r"scrimmage|meet|match|volleyball|football|basketball|baseball|softball|soccer|wrestling|track|"
    r"4-h|ffa|church|youth group|sunday school|vbs|mass|worship|homecoming|prom|"
    r"permission slip|booster|coach|team|roster|bus)\b", re.I)
_TODO = re.compile(r"\b(due|deadline|bring|sign(ed)? up|sign and return|return|turn(ed)? in|permission|"
                   r"pay|payment|fee|register|registration|rsvp|order|form|reply by|by)\b", re.I)

_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august",
           "september", "october", "november", "december"]
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MON_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|" \
          r"sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_WD_RE = r"(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)(?:day|nesday|sday|urday|rsday)?\.?"
_AP = r"(?:a\.?m\.?|p\.?m\.?)"
_TIME_RANGE = re.compile(rf"\b(?P<h1>\d{{1,2}})(?::(?P<m1>\d{{2}}))?\s*(?P<ap1>{_AP})?\s*(?:-|\u2013|to|until)\s*"
                         rf"(?P<h2>\d{{1,2}})(?::(?P<m2>\d{{2}}))?\s*(?P<ap2>{_AP})", re.I)
_TIME_ONE = re.compile(r"\b(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>a\.?m\.?|p\.?m\.?)|\b(?P<noon>noon)\b", re.I)
_DATE_PATTERNS = [
    # (Friday,) Nov 13(th)(, 2026)
    ("md", re.compile(rf"\b(?:{_WD_RE},?\s+)?{_MON_RE}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", re.I)),
    # (Friday,) 13 November
    ("dm", re.compile(rf"\b(?:{_WD_RE},?\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MON_RE}\b(?:,?\s+(\d{{4}}))?", re.I)),
    # (Fri) 11/13(/26)
    ("num", re.compile(rf"\b(?:{_WD_RE},?\s+)?(\d{{1,2}})/(\d{{1,2}})(?:/(\d{{2,4}}))?\b", re.I)),
    # tomorrow / this Friday / next Tuesday / on Monday
    ("rel", re.compile(rf"\b(tomorrow|tonight|(?:this|next|on)\s+{_WD_RE})", re.I)),
]

_URL = re.compile(r"(https?://|www\.)\S+", re.I)
_ADDR = re.compile(r"\S+@\S+")
_TAG = re.compile(r"\[[^\]]{0,40}\]|<[^>]{0,200}>")
_CTRL = re.compile(r"[\x00-\x1f\x7f]+")


# ------------------------------------------------------------------ cleaning
def clean(s, limit: int = TITLE_MAX) -> str:
    """Untrusted email text -> a short, harmless line (no links, addresses, tags, controls)."""
    s = _CTRL.sub(" ", str(s or ""))
    s = _URL.sub("", s)
    s = _ADDR.sub("", s)
    s = _TAG.sub("", s)
    s = s.replace("*", "").replace("`", "").replace("|", " ")
    s = re.sub(r"\s+", " ", s).strip(" -:;,.")
    if len(s) > limit:
        s = s[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return s


# ------------------------------------------------------------------ input
def _msg(d: dict) -> dict:
    g = lambda *ks: next((str(d[k]) for k in ks if d.get(k)), "")
    return {"id": g("id", "message_id", "messageId", "Message ID"),
            "subject": g("subject", "Subject"),
            "from": g("from", "sender", "From"),
            "date": g("date", "Date", "received"),
            "body": g("body", "text", "content", "snippet", "Body")}


def parse_messages(raw) -> list[dict]:
    """JSON list / {"messages": [...]} / the Gmail tool's plain-text output."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        txt = raw.strip()
        try:
            raw = json.loads(txt)
        except ValueError:
            return _parse_text(txt)
    if isinstance(raw, dict):
        raw = raw.get("messages") or raw.get("items") or [raw]
    if not isinstance(raw, list):
        raise ValueError("expected a list of messages")
    return [_msg(m) for m in raw if isinstance(m, dict)]


def _parse_text(txt: str) -> list[dict]:
    blocks = re.split(r"(?im)^(?=\s*message id\s*:)", txt)
    out = []
    for b in blocks:
        if not b.strip():
            continue
        head, body, in_body = {}, [], False
        for line in b.splitlines():
            m = None if in_body else re.match(r"\s*(message id|subject|from|date|to)\s*:\s*(.*)$", line, re.I)
            if m:
                head[m.group(1).lower()] = m.group(2).strip()
            elif head:
                in_body = True
                body.append(line)
        if head:
            out.append({"id": head.get("message id", ""), "subject": head.get("subject", ""),
                        "from": head.get("from", ""), "date": head.get("date", ""),
                        "body": "\n".join(body)})
    return out


def is_relevant(msg: dict) -> bool:
    return bool(_KEYWORDS.search(" ".join((msg.get("subject", ""), msg.get("from", ""),
                                           msg.get("body", "")[:4000]))))


# ------------------------------------------------------------------ dates
def _mon(s: str) -> int:
    s = s.lower().rstrip(".")[:3]
    return next(i + 1 for i, m in enumerate(_MONTHS) if m.startswith(s))


def _wd(s: str) -> int:
    s = s.lower().rstrip(".")[:3]
    return next(i for i, w in enumerate(_WEEKDAYS) if w.startswith(s))


def _year_fix(d: dt.date, ref: dt.date, explicit: bool) -> dt.date:
    if not explicit and d < ref - dt.timedelta(days=30):
        with contextlib.suppress(ValueError):
            return d.replace(year=d.year + 1)
    return d


def find_dates(text: str, ref: dt.date) -> list[tuple[dt.date, int, int]]:
    """[(date, start, end)] for each date phrase in text, relative to `ref` (the email's day)."""
    found = []
    for kind, rx in _DATE_PATTERNS:
        for m in rx.finditer(text):
            if any(s <= m.start() < e or m.start() <= s < m.end() for _, s, e in found):
                continue
            try:
                if kind == "md":
                    y = int(m.group(4)) if m.group(4) else ref.year
                    d = _year_fix(dt.date(y, _mon(m.group(2)), int(m.group(3))), ref, bool(m.group(4)))
                elif kind == "dm":
                    y = int(m.group(4)) if m.group(4) else ref.year
                    d = _year_fix(dt.date(y, _mon(m.group(3)), int(m.group(2))), ref, bool(m.group(4)))
                elif kind == "num":
                    if m.group(4):
                        y = int(m.group(4))
                        y = y + 2000 if y < 100 else y
                    else:
                        y = ref.year
                    d = _year_fix(dt.date(y, int(m.group(2)), int(m.group(3))), ref, bool(m.group(4)))
                else:
                    w = m.group(1).lower()
                    if w in ("tomorrow", "tonight"):
                        d = ref + dt.timedelta(days=1 if w == "tomorrow" else 0)
                    else:
                        idx = _wd(m.group(2))
                        d = ref + dt.timedelta(days=(idx - ref.weekday()) % 7 or 7)
            except (ValueError, StopIteration):
                continue
            found.append((d, m.start(), m.end()))
    return sorted(found, key=lambda t: t[1])


def _hm(h, m, ap) -> tuple[int, int] | None:
    h, m = int(h), int(m or 0)
    ap = (ap or "").lower().replace(".", "")
    if ap and not 1 <= h <= 12 or m > 59:
        return None
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    return (h, m) if h <= 23 else None


def find_time(text: str) -> tuple[str, str]:
    """('18:30', '20:00') / ('18:30', '') / ('', '') from a line."""
    m = _TIME_RANGE.search(text)
    if m:
        ap1 = m.group("ap1") or m.group("ap2")
        a, b = _hm(m.group("h1"), m.group("m1"), ap1), _hm(m.group("h2"), m.group("m2"), m.group("ap2"))
        if a and b:
            return f"{a[0]:02d}:{a[1]:02d}", f"{b[0]:02d}:{b[1]:02d}"
    m = _TIME_ONE.search(text)
    if m:
        if m.group("noon"):
            return "12:00", ""
        a = _hm(m.group("h"), m.group("m"), m.group("ap"))
        if a:
            return f"{a[0]:02d}:{a[1]:02d}", ""
    return "", ""


def _email_day(msg: dict, today: dt.date) -> dt.date:
    with contextlib.suppress(Exception):
        return email.utils.parsedate_to_datetime(msg["date"]).date()
    with contextlib.suppress(Exception):
        return dt.date.fromisoformat(msg["date"][:10])
    return today


def _strip_phrases(line: str) -> str:
    for _, rx in _DATE_PATTERNS:
        line = rx.sub(" ", line)
    line = _TIME_RANGE.sub(" ", line)
    line = _TIME_ONE.sub(" ", line)
    for _ in range(3):   # "on <date> at <time>." leaves "on at ." behind
        line = re.sub(r"\b(on|at|from|by|is|will be|this|next|and)\s*(?=[,.;:!]|$)", "", line.strip(), flags=re.I)
    return re.sub(r"\s+", " ", line)


def extract(msg: dict, today: dt.date | None = None) -> list[dict]:
    """Dated items in one message: [{'kind','title','date','time','end','subject','msg_id'}]."""
    today = today or dt.date.today()
    ref = _email_day(msg, today)
    subject = clean(msg.get("subject", ""))
    text = (msg.get("subject", "") + "\n" + msg.get("body", ""))[:20000]
    text = _URL.sub(" ", text)
    lines = [ln for chunk in text.splitlines() for ln in re.split(r"(?<=[.!?])\s+(?=[A-Z])", chunk)]
    out, seen = [], set()
    for i, line in enumerate(lines):
        if len(line) > 400 or re.match(r"\s*(>|on .* wrote:|from:|sent:|unsubscribe)", line, re.I):
            continue
        for d, s, e in find_dates(line, ref):
            if d < today or (d - today).days > HORIZON_DAYS:
                continue
            start, end = find_time(line[s:] or line)
            if not start:
                start, end = find_time(line)
            title = clean(_strip_phrases(line))
            if len(title.split()) < 3 and i == 0:
                title = subject
            elif len(title.split()) < 3:
                title = clean(f"{subject}: {title}" if title else subject)
            if not title:
                continue
            kind = "todo" if _TODO.search(line) and not start else "event"
            key = (d.isoformat(), title.lower()[:40])
            if key in seen:
                continue
            seen.add(key)
            out.append({"kind": kind, "title": title, "date": d.isoformat(), "time": start, "end": end,
                        "subject": subject, "msg_id": clean(msg.get("id", ""), 80)})
    # the subject usually repeats a body line: drop the bare-subject item when the body
    # gave something better for the same day
    better = {it["date"] for it in out if it["title"] != subject}
    return [it for it in out if it["title"] != subject or it["date"] not in better]


# ------------------------------------------------------------------ store
def _key(it: dict) -> str:
    return f"{it['date']}|{re.sub(r'[^a-z0-9]', '', it['title'].lower())[:40]}"


class ProposalStore:
    DEFAULT_PATH: Path | None = None

    def __init__(self, path: Path | None = None):
        self.path = Path(path or self.DEFAULT_PATH or data_dir() / "school-proposals.json")

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_name(self.path.name + ".lock"), "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            data = self._read()
            yield data
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)

    def _read(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
            if isinstance(d, dict):
                d.setdefault("pending", []); d.setdefault("seen", []); d.setdefault("decided", [])
                d.setdefault("next_id", 1)
                return d
        except (OSError, ValueError):
            pass
        return {"pending": [], "seen": [], "decided": [], "next_id": 1}

    def pending(self, today: dt.date | None = None) -> list[dict]:
        today = (today or dt.date.today()).isoformat()
        return [p for p in self._read()["pending"] if p["date"] >= today]

    def scan(self, messages: list[dict], account: str = "", today: dt.date | None = None,
             require_keywords: bool = True) -> list[dict]:
        """Add new proposals from messages; returns only the NEW ones."""
        today = today or dt.date.today()
        new = []
        with self._locked() as data:
            data["pending"] = [p for p in data["pending"] if p["date"] >= today.isoformat()]
            known = set(data["decided"]) | {_key(p) for p in data["pending"]}
            seen = set(data["seen"])
            for msg in messages:
                mid = msg.get("id", "")
                if mid and mid in seen:
                    continue
                if mid:
                    data["seen"].append(mid); seen.add(mid)
                if require_keywords and not is_relevant(msg):
                    continue
                for it in extract(msg, today):
                    k = _key(it)
                    if k in known or len(new) >= MAX_PROPOSALS:
                        continue
                    known.add(k)
                    it = {"id": data["next_id"], **it, "account": clean(account, 80)}
                    data["next_id"] += 1
                    data["pending"].append(it)
                    new.append(it)
            data["pending"] = sorted(data["pending"], key=lambda p: (p["date"], p["time"]))[:MAX_PENDING]
            data["seen"] = data["seen"][-MAX_SEEN:]
        return new

    def _pick(self, data: dict, refs) -> list[dict]:
        if refs in ("all", ["all"]) or refs == []:
            return list(data["pending"])
        ids = {int(str(r).lstrip("#")) for r in refs}
        got = [p for p in data["pending"] if p["id"] in ids]
        missing = ids - {p["id"] for p in got}
        if missing:
            raise KeyError(f"no proposal #{min(missing)}")
        return got

    def decide(self, refs, accepted: bool) -> list[dict]:
        with self._locked() as data:
            got = self._pick(data, refs)
            for p in got:
                data["pending"].remove(p)
                data["decided"].append(_key(p))
            data["decided"] = data["decided"][-MAX_DECIDED:]
        return got

    def clear(self) -> int:
        with self._locked() as data:
            n = len(data["pending"])
            data["decided"] += [_key(p) for p in data["pending"]]
            data["pending"] = []
        return n


# ------------------------------------------------------------------ output
def when(p: dict) -> str:
    d = dt.date.fromisoformat(p["date"])
    s = d.strftime("%a %b %d").replace(" 0", " ")
    if p.get("time"):
        h, m = map(int, p["time"].split(":"))
        s += " " + dt.time(h, m).strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
    return s


def card_body(items: list[dict], limit: int = 6) -> str:
    lines = [f"{p['id']}. {when(p)}: {p['title']}" + (" (to-do)" if p["kind"] == "todo" else "")
             for p in items[:limit]]
    if len(items) > limit:
        lines.append(f"+ {len(items) - limit} more")
    return "\n".join(lines) + "\n\nFrom email. Add these? Say yes, or which ones."


def calendar_event(p: dict, minutes: int = 60) -> dict:
    """The event the brain may create (after the spoken yes) with its calendar tool."""
    if p.get("time"):
        h, m = map(int, p["time"].split(":"))
        start = dt.datetime.combine(dt.date.fromisoformat(p["date"]), dt.time(h, m))
        if p.get("end"):
            eh, em = map(int, p["end"].split(":"))
            end = dt.datetime.combine(start.date(), dt.time(eh, em))
            if end <= start:
                end = start + dt.timedelta(minutes=minutes)
        else:
            end = start + dt.timedelta(minutes=minutes)
        return {"summary": p["title"], "start": start.isoformat(timespec="minutes"),
                "end": end.isoformat(timespec="minutes"), "all_day": False,
                "description": f"From an email: {p['subject']}"}
    nxt = dt.date.fromisoformat(p["date"]) + dt.timedelta(days=1)
    return {"summary": p["title"], "start": p["date"], "end": nxt.isoformat(), "all_day": True,
            "description": f"From an email: {p['subject']}"}


def list_due(p: dict) -> str:
    return f"{p['date']}T{p['time']}" if p.get("time") else p["date"]
