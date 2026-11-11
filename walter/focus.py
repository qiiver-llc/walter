"""Focus mode: time boxes, the Now/Next strip and the GTD inbox review (5 Nov 2026).

State lives in files so the CLI (walterctl focus ...), the server and the face
share it without an API:

  ~/.walter/focus.json      the running box (if any) + the strip setting
  ~/.walter/focus-log.json  boxes finished (for the evening wrap-up), capped
  face/focus.json           what the face polls (box + strip setting); the face
                            works out the time left, the top task and the next
                            calendar event itself from the other dashboard files

A box: {"id", "task", "start", "end", "planned_min", "status": "running"|"checkin",
        "warned", "checkin_at", "events_warned": [...]}
The server's focus loop calls tick() every few seconds: it returns what to say
(5-minute warning, the end-of-box check-in, a calendar heads-up) exactly once,
even across restarts, because the flags are stored in the file.

Stdlib only: imported by the server, the CLI and bin/walter-wrapup.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import time
from pathlib import Path

from . import dashboard as D

WARN_S = 5 * 60            # the 5-minute warning
CHECKIN_EXPIRE_S = 20 * 60 # an unanswered check-in closes the box by itself after this
EVENT_SOON_S = 10 * 60     # calendar heads-up while a box runs
LOG_MAX = 300
MAX_MIN = 4 * 60
STRIP_MODES = ("auto", "box", "off")


def parse_minutes(s) -> float:
    """'25m', '25', '1h', '1h30m', '90s', '25 min', 'half an hour' -> minutes."""
    t = str(s or "").strip().lower()
    if t in ("half an hour", "half hour", "30 min"):
        return 30.0
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\s*h(?:ours?|rs?)?)?\s*(?:(\d+(?:\.\d+)?)\s*m(?:in(?:ute)?s?)?)?"
                     r"\s*(?:(\d+)\s*s(?:ec(?:ond)?s?)?)?", t)
    if t and m and any(m.groups()):
        h, mi, se = (float(x or 0) for x in m.groups())
        mins = h * 60 + mi + se / 60
    elif re.fullmatch(r"\d+(?:\.\d+)?", t):
        mins = float(t)
    else:
        raise ValueError(f"can't read the length {s!r} (try 25m, 1h, 1h30m)")
    if not 0 < mins <= MAX_MIN:
        raise ValueError(f"a box is 1 minute to {MAX_MIN // 60} hours")
    return mins


def hm(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%I:%M %p").lstrip("0")


def mins_text(m: float) -> str:
    m = max(0, int(round(m)))
    if m < 60:
        return f"{m} min"
    h, r = divmod(m, 60)
    return f"{h} h" + (f" {r} min" if r else "")


def spoken_mins(m: float) -> str:
    m = max(1, int(round(m)))
    return "a minute" if m == 1 else f"{m} minutes"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", str(s or "").lower()).strip()


class FocusStore:
    DEFAULT_PATH: Path | None = None    # tests: never the real ~/.walter
    DEFAULT_LOG: Path | None = None
    DEFAULT_FACE: Path | None = None

    def __init__(self, path: Path | None = None, log_path: Path | None = None,
                 face_out: Path | None = None):
        self.path = Path(path) if path else (self.DEFAULT_PATH or D.data_dir() / "focus.json")
        self.log_path = Path(log_path) if log_path else (self.DEFAULT_LOG or D.data_dir() / "focus-log.json")
        self.face_out = Path(face_out) if face_out else (self.DEFAULT_FACE or D.FACE_DIR / "focus.json")

    # ------------------------------------------------------------ storage
    def _read(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
            if isinstance(d, dict):
                d.setdefault("box", None)
                d.setdefault("strip", "auto")
                d.setdefault("next_id", 1)
                return d
        except (OSError, ValueError):
            pass
        return {"box": None, "strip": "auto", "next_id": 1}

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_name(self.path.name + ".lock"), "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            data = self._read()
            before = json.dumps(data, sort_keys=True)
            yield data
            if json.dumps(data, sort_keys=True) != before:
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
                self.publish(data)

    def publish(self, data: dict | None = None):
        data = data or self._read()
        box = data.get("box")
        face_box = None
        if box:
            face_box = {k: box[k] for k in ("task", "start", "end", "status", "planned_min") if k in box}
        D.write_face_json(self.face_out, {"box": face_box, "strip": data.get("strip", "auto"),
                                          "changed_at": time.time()})

    def state(self) -> dict:
        return self._read()

    def box(self) -> dict | None:
        return self._read().get("box")

    def running(self) -> bool:
        b = self.box()
        return bool(b and b.get("status") == "running")

    def log(self) -> list[dict]:
        try:
            d = json.loads(self.log_path.read_text())
            return d if isinstance(d, list) else []
        except (OSError, ValueError):
            return []

    def _append_log(self, entry: dict):
        items = self.log()
        items.append(entry)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.log_path.with_name(self.log_path.name + ".tmp")
        tmp.write_text(json.dumps(items[-LOG_MAX:], indent=1, ensure_ascii=False))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.log_path)

    def _close(self, data: dict, outcome: str, now: float) -> dict:
        box = data["box"]
        stop = min(now, box.get("checkin_at") or now) if outcome == "ended" else now
        worked = max(0.0, (stop - box["start"]) / 60)
        entry = {"id": box["id"], "task": box["task"], "start": round(box["start"], 1),
                 "stop": round(stop, 1), "minutes": round(worked, 1),
                 "planned_min": box.get("planned_min"), "outcome": outcome}
        if box.get("demo"):
            entry["demo"] = True     # the presentation's demo box leaves no trace in the log
        else:
            self._append_log(entry)
        data["box"] = None
        return entry

    # ------------------------------------------------------------ commands
    def start(self, task: str, minutes: float, now: float | None = None, demo: bool = False) -> dict:
        """Start a box (a running one is closed as 'stopped' first). Returns the box.
        demo=True (the presentation's box): shown like any box, never written to the log."""
        now = now or time.time()
        task = D.clean_text(task, 80)
        if not task:
            raise ValueError("what's the box for? (a short task name)")
        minutes = float(minutes)
        if not 0 < minutes <= MAX_MIN:
            raise ValueError(f"a box is 1 minute to {MAX_MIN // 60} hours")
        with self._locked() as data:
            if data.get("box"):
                self._close(data, "switched", now)
            box = {"id": data["next_id"], "task": task, "start": round(now, 1),
                   "end": round(now + minutes * 60, 1), "planned_min": round(minutes, 1),
                   "status": "running", "warned": minutes * 60 <= WARN_S, "checkin_at": None,
                   "events_warned": []}
            if demo:
                box["demo"] = True
            data["next_id"] += 1
            data["box"] = box
        return box

    def extend(self, minutes: float, now: float | None = None) -> dict:
        """More time: from the current end, or from now if time was already up."""
        now = now or time.time()
        minutes = float(minutes)
        if not 0 < minutes <= MAX_MIN:
            raise ValueError(f"add 1 minute to {MAX_MIN // 60} hours")
        with self._locked() as data:
            box = data.get("box")
            if not box:
                raise KeyError("no focus box running")
            base = max(box["end"], now)
            box["end"] = round(base + minutes * 60, 1)
            box["planned_min"] = round(box.get("planned_min", 0) + minutes, 1)
            box["status"] = "running"
            box["checkin_at"] = None
            box["warned"] = box["end"] - now <= WARN_S
        return box

    def finish(self, outcome: str, now: float | None = None) -> dict:
        """outcome: done | parked | stopped. Returns the log entry."""
        now = now or time.time()
        with self._locked() as data:
            if not data.get("box"):
                raise KeyError("no focus box running")
            return self._close(data, outcome, now)

    def set_fallback(self, ids: list[int]):
        """Reminder ids standing in for the warning/check-in on an older server."""
        with self._locked() as data:
            if ids:
                data["fallback_ids"] = list(ids)
            else:
                data.pop("fallback_ids", None)

    def take_fallback(self) -> list[int]:
        with self._locked() as data:
            return data.pop("fallback_ids", None) or []

    def set_strip(self, mode: str) -> str:
        mode = {"on": "auto", "always": "auto"}.get(mode, mode)
        if mode not in STRIP_MODES:
            raise ValueError("strip: on (auto), box (only during a box) or off")
        with self._locked() as data:
            data["strip"] = mode
        self.publish()
        return mode

    # ------------------------------------------------------------ the clock
    def tick(self, now: float | None = None, events: list | None = None) -> list[dict]:
        """What the server should say now, each exactly once:
        {"kind": "warn"|"checkin"|"expired"|"event", "task", "line", ...}."""
        now = now or time.time()
        out = []
        with self._locked() as data:
            box = data.get("box")
            if not box:
                return []
            left = box["end"] - now
            if box["status"] == "running":
                if not box.get("warned") and 0 < left <= WARN_S:
                    box["warned"] = True
                    out.append({"kind": "warn", "task": box["task"],
                                "line": f"Five minutes left on {box['task']}."
                                if left > WARN_S - 60 else
                                f"{spoken_mins(left / 60).capitalize()} left on {box['task']}."})
                for ev in upcoming_events(events or [], now, EVENT_SOON_S):
                    key = f"{ev['start']}|{ev['title']}"
                    if key in box.setdefault("events_warned", []):
                        continue
                    box["events_warned"].append(key)
                    mins = max(1, round((ev["ts"] - now) / 60))
                    out.append({"kind": "event", "task": box["task"], "title": ev["title"],
                                "line": f"Heads up: {ev['title']} at {hm(ev['ts'])}, "
                                        f"in {spoken_mins(mins)}."})
                if left <= 0:
                    box["status"] = "checkin"
                    box["checkin_at"] = round(now, 1)
                    out.append({"kind": "checkin", "task": box["task"],
                                "line": f"Time's up on {box['task']}. Done, more time, or park it?"})
            elif box["status"] == "checkin" and now - (box.get("checkin_at") or now) > CHECKIN_EXPIRE_S:
                entry = self._close(data, "ended", now)
                out.append({"kind": "expired", "task": entry["task"], "entry": entry, "line": ""})
        return out

    def status(self, now: float | None = None) -> dict:
        now = now or time.time()
        box = self.box()
        if not box:
            return {"box": None}
        left = box["end"] - now
        total = max(1.0, box["end"] - box["start"])
        return {"box": box, "left_min": max(0.0, left / 60), "over_min": max(0.0, -left / 60),
                "frac": min(1.0, max(0.0, (now - box["start"]) / total)),
                "elapsed_min": (now - box["start"]) / 60}

    def today(self, day: dt.date | None = None) -> list[dict]:
        day = day or dt.date.today()
        return [e for e in self.log() if dt.date.fromtimestamp(e["start"]) == day and not e.get("demo")]


# ------------------------------------------------------------ the list
def open_tasks(items: list[dict]) -> list[dict]:
    """Open to-dos in the order the strip uses: anything due today or earlier
    first (soonest first), then the list's own order."""
    today = dt.date.today().isoformat()
    todo = [i for i in items if i.get("section") == "tasks" and not i.get("done_at")]
    due = sorted((i for i in todo if i.get("due") and i["due"][:10] <= today), key=lambda i: i["due"])
    return due + [i for i in todo if i not in due]


def match_task(items: list[dict], task: str) -> dict | None:
    """The open list item a box is about (same words, or one contains the other)."""
    t = _norm(task)
    if not t:
        return None
    todo = [i for i in items if i.get("section") in ("tasks", "inbox") and not i.get("done_at")]
    for i in todo:
        if _norm(i["text"]) == t:
            return i
    for i in todo:
        n = _norm(i["text"])
        if n and len(min(n, t, key=len)) >= 4 and (t in n or n in t):
            return i
    return None


def tick_off(store: D.ListStore, task: str) -> dict | None:
    it = match_task(store.items(), task)
    if it:
        return store.done({"id": it["id"]})
    return None


def park_on_list(store: D.ListStore, task: str) -> dict:
    """Parked: make sure it's on the to-do list for later."""
    it = match_task(store.items(), task)
    return it or store.add(task, "tasks")


# ------------------------------------------------------------ calendar
def _event_ts(e: dict) -> float | None:
    s = str(e.get("start") or "")
    if len(s) == 10 or e.get("all_day"):
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.timestamp()


def load_events() -> list[dict]:
    try:
        return json.loads((D.FACE_DIR / "dashboard-events.json").read_text()).get("events") or []
    except (OSError, ValueError):
        return []


def upcoming_events(events: list, now: float, within_s: float) -> list[dict]:
    out = []
    for e in events:
        ts = _event_ts(e)
        if ts is not None and now < ts <= now + within_s:
            out.append({**e, "ts": ts})
    return sorted(out, key=lambda e: e["ts"])


def next_event_today(events: list, now: float) -> dict | None:
    end_of_day = dt.datetime.combine(dt.date.fromtimestamp(now), dt.time(23, 59, 59)).timestamp()
    evs = upcoming_events(events, now, end_of_day - now)
    return evs[0] if evs else None


def now_next(store: FocusStore | None = None, items: list | None = None, events: list | None = None,
             now: float | None = None) -> dict:
    """{"now": "Network app", "left_min": 12 | None, "status", "next": "3:30 PM Dentist" | "Call the vet"}"""
    now = now or time.time()
    store = store or FocusStore()
    items = D.ListStore().items() if items is None else items
    events = load_events() if events is None else events
    st = store.status(now)
    box = st.get("box")
    tasks = open_tasks(items)
    out = {"now": "", "left_min": None, "status": "", "next": "", "next_kind": ""}
    if box:
        out.update(now=box["task"], left_min=st["left_min"], status=box["status"])
    elif tasks:
        out["now"] = tasks[0]["text"]
    ev = next_event_today(events, now)
    if ev:
        out.update(next=f"{hm(ev['ts'])} {ev['title']}", next_kind="event")
    else:
        rest = [i for i in tasks if _norm(i["text"]) != _norm(out["now"])]
        if rest:
            out.update(next=rest[0]["text"], next_kind="task")
    return out


def spoken_now_next(nn: dict) -> str:
    bits = []
    if nn.get("now"):
        if nn.get("status") == "checkin":
            bits.append(f"Time's up on {nn['now']}")
        elif nn.get("left_min") is not None:
            bits.append(f"You're on {nn['now']}, {spoken_mins(nn['left_min'])} left")
        else:
            bits.append(f"Top of your list is {nn['now']}")
    if nn.get("next"):
        bits.append(("next up is " if bits else "Next up is ") + nn["next"])
    if not bits:
        return "Nothing lined up. Your list's clear and there's nothing else on the calendar today."
    return ", and ".join(bits) + "."


def strip_text(nn: dict) -> tuple[str, str]:
    """The two lines on the face (the face computes the same thing itself)."""
    now = ""
    if nn.get("now"):
        if nn.get("status") == "checkin":
            now = f"Now: {nn['now']} - time's up"
        elif nn.get("left_min") is not None:
            now = f"Now: {nn['now']} - {mins_text(max(1, nn['left_min']))} left"
        else:
            now = f"Now: {nn['now']}"
    return now, (f"Next: {nn['next']}" if nn.get("next") else "")


# ------------------------------------------------------------ wrap-up
def summarize_day(entries: list[dict]) -> list[tuple[str, float, bool]]:
    """[(task, minutes, done)] per task, in first-worked order."""
    per: dict[str, list] = {}
    for e in entries:
        k = _norm(e["task"]) or e["task"]
        if k not in per:
            per[k] = [e["task"], 0.0, False]
        per[k][1] += float(e.get("minutes") or 0)
        per[k][2] = per[k][2] or e.get("outcome") == "done"
    return [tuple(v) for v in per.values()]


def suggest_first_box(items: list[dict], entries: list[dict]) -> str:
    """Tomorrow's first box: a parked box from today, else the top open task, else
    the oldest inbox item."""
    open_ = {_norm(i["text"]): i["text"] for i in items
             if i.get("section") == "tasks" and not i.get("done_at")}
    for e in reversed(entries):
        if e.get("outcome") in ("parked", "ended", "stopped") and _norm(e["task"]) in open_:
            return open_[_norm(e["task"])]
    tasks = open_tasks(items)
    if tasks:
        return tasks[0]["text"]
    inbox = [i for i in items if i.get("section") == "inbox" and not i.get("done_at")]
    return inbox[0]["text"] if inbox else ""


# ------------------------------------------------------------ inbox review
def inbox_items(items: list[dict]) -> list[dict]:
    return [i for i in items if i.get("section") == "inbox" and not i.get("done_at")]


def review_action(store: D.ListStore, action: str, item_id: int, arg: str = "") -> str:
    """do / defer WHEN / delegate WHO / drop an inbox item (by its id). Returns what happened."""
    it = next((i for i in store.items() if i["id"] == item_id), None)
    if it is None:
        raise KeyError(f"no list item #{item_id}")
    text = it["text"]
    if action == "do":
        new = store.move({"id": item_id}, "tasks", due=arg or None)
        return f"to do: {new['text']}" + (f" (due {new['due'].replace('T', ' ')})" if new.get("due") else "")
    if action == "defer":
        if not arg:
            raise ValueError("defer needs a when (tomorrow, friday, 11/9, next week)")
        when = "today" if arg in ("later", "later today") else arg
        if when == "next week":
            when = (dt.date.today() + dt.timedelta(days=7)).isoformat()
        new = store.move({"id": item_id}, "tasks", due=when)
        return f"deferred: {new['text']} (due {new['due'].replace('T', ' ')})"
    if action == "delegate":
        who = D.clean_text(arg, 40)
        if not who:
            raise ValueError("delegate needs a who")
        new = store.move({"id": item_id}, "tasks", text=f"Waiting on {who}: {text}")
        return f"delegated: {new['text']}"
    if action == "drop":
        store.remove({"id": item_id})
        return f"dropped: {text}"
    raise ValueError("action: do, defer, delegate or drop")
