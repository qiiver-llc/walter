"""Companion app Schedule screen: everything that runs on a timer, in one place (the owner asked 11 Nov 10:48).

GET  /api/app/schedule
       {now, reminders: [...], routines: [...], later: [...], loops: {...}, timers: [...],
        workspaces: [{id, name, walled}], current_workspace: id|null}
       ?workspace=ID shows only that project's items (?workspace=none: the ones without a project)
       reminders  one-off "tell me" items          {id, kind, due, text, workspace}
       later      one-off "do this later" jobs     {id, kind, due, text, workspace}
       routines   recurring (`walterctl every`)    {id, kind, due, text, days: [0..6], days_label, time,
                                                    enabled, role}   role: briefing | wrapup | self_review | ""
       loops      {heartbeat: {minutes, min, max, quiet_hours: [start, end]},
                   sleep_review: {on}, routines: {briefing|wrapup|self_review: id}}
       timers     systemd --user timers, READ-ONLY  {name, description, every, next, last}
       Text is sent in full (routines are often long internal instructions); the app shows the first line.
POST /api/app/schedule
       {action: "add",    type: "reminder"|"later", text, time: "HH:MM", date?: "YYYY-MM-DD", workspace?}
       {action: "add",    type: "routine", text, time, days, kind?: "do"|"remind", workspace?}
       {action: "edit",   id, text?, time?, date?, days?, workspace?}   (routines need time + days together)
       workspace: a workspace id from workspaces.json (whitelist; unknown = 400 "unknown_workspace") or
       null / "" for none (global). A routine/later job with one runs headed with that project and its
       workers are tagged with it; a reminder just names it.
       {action: "pause"|"resume", id}                          routines only ("enabled": false in the store)
       {action: "delete", id, confirm: true}                   without confirm: 409 code "confirm"
       {action: "heartbeat", minutes: 5..120}                  [life] heartbeat_minutes, live (config set path)
       {action: "sleep_review", on: bool}                      [life] sleep_review, live (config set path)
       {action: "set_heartbeat_prompt", text}                  [life] heartbeat_prompt (the wake-up line), live
       {action: "reset_heartbeat_prompt"}                      back to the built-in line ("")
       {action: "set_initiative", text, base_mtime, allow_empty?}   <home>/initiative.md; 409 "conflict" if it
                                                               changed since base_mtime, 409 "empty" without allow_empty
       {action: "restore_initiative", backup: id}              put a backed-up version back (itself undoable)
GET  /api/app/schedule/heartbeat
       {prompt, default_prompt, initiative_md, initiative_md_mtime, backups: [{id, time, size}], limits, placeholders}
       (separate from the list so the 60 KB checklist isn't sent with every refresh; walter/heartbeat.py)
Nothing else: system timers and any other config key can't be changed from here.

Edits go through the running Reminders object (conductor.Reminders: add / update / set_enabled / cancel), the same
store `walterctl remind|later|every|cancel|pause|resume` reaches through /api/remind and /api/reminders, so the
app and the CLI never disagree. Days and times use the walterctl words: daily, weekdays, weekends, mon,wed,fri.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import subprocess
import os
import time
from pathlib import Path

from aiohttp import web

from . import heartbeat as hb
from .live import toml_value

log = logging.getLogger("walter.app.schedule")

HB_ACTIONS = ("set_heartbeat_prompt", "reset_heartbeat_prompt", "set_initiative", "restore_initiative")
ACTIONS = ("add", "edit", "pause", "resume", "delete", "heartbeat", "sleep_review") + HB_ACTIONS
THREAD_ACTIONS = ("heartbeat", "sleep_review") + HB_ACTIONS       # file/config writes: off the event loop
TYPES = ("reminder", "later", "routine")
TEXT_MAX = 2000
BODY_MAX = 200_000         # the heartbeat checklist is up to 60 KB (JSON-escaped); fields are checked one by one
ITEMS_MAX = 200            # the app can't grow the schedule without bound
HB_MIN, HB_MAX = 5, 120
AHEAD_MAX_S = 366 * 86400  # one-off items: within a year
RATE_N, RATE_S = 30, 60    # at most 30 changes a minute from the app (all devices together)
TIMER_CACHE_S = 30

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
FULL = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
NAMED = {"daily": list(range(7)), "everyday": list(range(7)), "weekdays": list(range(5)), "weekends": [5, 6]}
HHMM = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")    # newlines and tabs are fine in instructions
ROLES = (("self_review", re.compile(r"self-improvement loop|self-review", re.I)),
         ("wrapup", re.compile(r"wrap-?up", re.I)),
         ("briefing", re.compile(r"morning briefing", re.I)))


class ScheduleError(ValueError):
    def __init__(self, msg: str, status: int = 400, code: str = "bad_request"):
        super().__init__(msg)
        self.status, self.code = status, code


# ------------------------------------------------------------------ validation (walterctl words)
def parse_days(v) -> list[int]:
    """daily | everyday | weekdays | weekends | mon,wed,fri (full day names too). Strict."""
    if not isinstance(v, str) or len(v) > 80:
        raise ScheduleError("days: daily, weekdays, weekends or mon,tue,...")
    s = v.strip().lower()
    if s in NAMED:
        return list(NAMED[s])
    out = set()
    for tok in s.split(","):
        t = tok.strip()
        if t in DAYS:
            out.add(DAYS.index(t))
        elif t in FULL:
            out.add(FULL.index(t))
        else:
            raise ScheduleError("days: daily, weekdays, weekends or mon,tue,...")
    return sorted(out)


def days_label(days: list[int]) -> str:
    ds = sorted(set(days))
    for name in ("daily", "weekdays", "weekends"):
        if ds == NAMED[name]:
            return name
    return ",".join(DAYS[d] for d in ds)


def parse_time(v) -> str:
    if not isinstance(v, str) or not HHMM.fullmatch(v.strip()):
        raise ScheduleError("time must be HH:MM (24-hour), e.g. 07:30")
    h, m = v.strip().split(":")
    return f"{int(h):02d}:{m}"


def clean_text(v) -> str:
    if not isinstance(v, str):
        raise ScheduleError("text must be text")
    s = v.replace("\r\n", "\n").strip()
    if not s:
        raise ScheduleError("text can't be empty")
    if len(s) > TEXT_MAX:
        raise ScheduleError(f"text: {TEXT_MAX} characters at most")
    if _CTRL.search(s):
        raise ScheduleError("text: no control characters")
    return s


def clean_id(v) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 < v < 10 ** 9:
        raise ScheduleError("id must be a schedule item number")
    return v


def due_at(hhmm: str, date: str | None, now: float) -> float:
    """A one-off due time: HH:MM today/tomorrow (like `walterctl remind 18:30`) or on DATE."""
    h, m = (int(x) for x in hhmm.split(":"))
    if date is None:
        base = dt.datetime.fromtimestamp(now)
        t = base.replace(hour=h, minute=m, second=0, microsecond=0)
        if t.timestamp() <= now:
            t += dt.timedelta(days=1)
        return t.timestamp()
    if not isinstance(date, str) or not DATE.fullmatch(date):
        raise ScheduleError("date must be YYYY-MM-DD")
    try:
        d = dt.datetime.strptime(date, "%Y-%m-%d").replace(hour=h, minute=m)
    except ValueError:
        raise ScheduleError("date must be a real day (YYYY-MM-DD)") from None
    ts = d.timestamp()
    if ts <= now:
        raise ScheduleError("that time has already passed")
    if ts > now + AHEAD_MAX_S:
        raise ScheduleError("within a year, please")
    return ts


def _bool(d: dict, key: str, default=None):
    v = d.get(key, default)
    if not isinstance(v, bool):
        raise ScheduleError(f"{key} must be true or false")
    return v


_WS_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def parse_workspace(v):
    """Shape only: None / "" = no project, else a workspace-id-looking string (apply checks it exists)."""
    if v is None or v == "":
        return None
    if not isinstance(v, str) or not _WS_ID.fullmatch(v):
        raise ScheduleError("workspace must be one of the workspace ids", 400, "unknown_workspace")
    return v


def parse(d) -> dict:
    """Strict validation of a POST body (shape only; the store checks come in apply)."""
    if not isinstance(d, dict):
        raise ScheduleError("send a JSON object")
    act = d.get("action")
    if act not in ACTIONS:
        raise ScheduleError("action must be one of " + ", ".join(ACTIONS))
    if act == "heartbeat":
        m = d.get("minutes")
        if isinstance(m, bool) or not isinstance(m, int) or not HB_MIN <= m <= HB_MAX:
            raise ScheduleError(f"minutes must be a whole number from {HB_MIN} to {HB_MAX}")
        return {"action": act, "minutes": m}
    if act == "sleep_review":
        return {"action": act, "on": _bool(d, "on")}
    try:
        if act == "set_heartbeat_prompt":
            return {"action": act, "text": hb.clean_prompt(d.get("text"))}
        if act == "reset_heartbeat_prompt":
            return {"action": act}
        if act == "set_initiative":
            allow = _bool(d, "allow_empty", False)
            bm = d.get("base_mtime")
            if bm is not None and (isinstance(bm, bool) or not isinstance(bm, (int, float))):
                raise ScheduleError("base_mtime must be the number from the last read")
            return {"action": act, "text": hb.clean_initiative(d.get("text"), allow), "base_mtime": bm}
        if act == "restore_initiative":
            b = d.get("backup")
            if not isinstance(b, str) or not hb.BACKUP_RE.fullmatch(b):
                raise ScheduleError("pick one of the listed versions")
            return {"action": act, "backup": b}
    except hb.HeartbeatError as e:
        raise ScheduleError(str(e), e.status, e.code) from None
    if act == "add":
        typ = d.get("type")
        if typ not in TYPES:
            raise ScheduleError("type must be reminder, later or routine")
        req = {"action": act, "type": typ, "text": clean_text(d.get("text")), "time": parse_time(d.get("time")),
               "workspace": parse_workspace(d.get("workspace"))}
        if typ == "routine":
            kind = d.get("kind", "do")
            if kind not in ("do", "remind"):
                raise ScheduleError("kind must be do or remind")
            req.update(days=parse_days(d.get("days")), kind=kind)
        else:
            req["date"] = d.get("date")
            if req["date"] is not None and (not isinstance(req["date"], str) or not DATE.fullmatch(req["date"])):
                raise ScheduleError("date must be YYYY-MM-DD")
        return req
    req = {"action": act, "id": clean_id(d.get("id"))}
    if act == "delete":
        req["confirm"] = _bool(d, "confirm", False)
    elif act == "edit":
        if "text" in d:
            req["text"] = clean_text(d["text"])
        if "time" in d:
            req["time"] = parse_time(d["time"])
        if "days" in d:
            req["days"] = parse_days(d["days"])
        if "date" in d:
            if not isinstance(d["date"], str) or not DATE.fullmatch(d["date"]):
                raise ScheduleError("date must be YYYY-MM-DD")
            req["date"] = d["date"]
        if "workspace" in d:
            req["workspace"] = parse_workspace(d["workspace"])
        if len(req) == 2:
            raise ScheduleError("nothing to change: send text, time, days, date or workspace")
    return req


# ------------------------------------------------------------------ system timers (read-only)
def _run(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=4).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def read_timers(run=_run) -> list[dict]:
    """systemd --user timers: name, description, how often, next/last run (epoch). Never raises."""
    try:
        rows = json.loads(run(["systemctl", "--user", "list-timers", "--all", "--output=json", "--no-pager"]) or "[]")
    except ValueError:
        rows = []
    out = []
    for r in rows if isinstance(rows, list) else []:
        unit = str((r or {}).get("unit") or "")
        if not unit.endswith(".timer") or not re.fullmatch(r"[\w.@-]{1,80}", unit):
            continue
        props = {}
        for ln in run(["systemctl", "--user", "show", unit, "-p", "Description", "-p", "TimersMonotonic",
                       "-p", "TimersCalendar", "--no-pager"]).splitlines():
            k, _, v = ln.partition("=")
            props.setdefault(k, []).append(v)
        every = []
        for v in props.get("TimersCalendar", []):
            m = re.search(r"OnCalendar=([^;]+?)\s*;", v)
            if m:
                every.append(m.group(1).strip())
        for v in props.get("TimersMonotonic", []):
            m = re.search(r"(OnUnitActiveUSec|OnUnitInactiveUSec)=([^;]+?)\s*;", v)
            if m:
                every.append("every " + m.group(2).strip())

        def ts(key):
            v = r.get(key)
            return round(v / 1e6, 1) if isinstance(v, (int, float)) and v > 0 else None
        out.append({"name": unit[:-6], "description": (props.get("Description") or [""])[0][:200],
                    "every": ", ".join(every)[:120], "next": ts("next"), "last": ts("last")})
    return out


# ------------------------------------------------------------------ the schedule
class Schedule:
    def __init__(self, reminders, cfg=None, live=None, timers=read_timers, now=time.time,
                 mono=time.monotonic, backup_dir=None, workspaces=None):
        self.reminders = reminders
        self._ws = workspaces      # callable -> WorkspaceStore (tests pass their own); default: the real one
        self.cfg = cfg
        home = getattr(getattr(cfg, "brain", None), "home", None)
        self.initiative = hb.InitiativeFile(home, backup_dir, now=now) if home and backup_dir else None
        self._live = live          # callable -> live.Live (or None); tests pass a fake
        self._timers = timers
        self.now = now
        self.mono = mono
        self._hits: list[float] = []
        self._tcache: tuple[float, list] | None = None

    def ws_store(self):
        if self._ws is not None:
            return self._ws()
        from .workspaces import WorkspaceStore
        return WorkspaceStore()

    def projects(self) -> tuple[list[dict], str | None]:
        """([{id, name, walled}], the wall's current id): names only, walled ones too. ([], None) when
        workspaces aren't defined (the app then hides the project bits)."""
        try:
            st = self.ws_store()
            if not st.defined():
                return [], None
            return ([{"id": w["id"], "name": w["name"], "walled": w["privacy"] == "walled"} for w in st.all()],
                    st.current_id())
        except Exception:  # noqa: BLE001
            log.exception("schedule: couldn't read the workspaces")
            return [], None

    def check_workspace(self, wid):
        """Whitelist: None, or an id that exists in workspaces.json."""
        if wid is None:
            return None
        if self.ws_store().valid_id(wid) is None:
            raise ScheduleError(f"there's no project '{wid}'", 400, "unknown_workspace")
        return wid

    def rate_ok(self) -> bool:
        t = self.mono()
        self._hits = [h for h in self._hits if t - h < RATE_S]
        if len(self._hits) >= RATE_N:
            return False
        self._hits.append(t)
        return True

    def timers(self) -> list[dict]:
        t = self.mono()
        if self._tcache and t - self._tcache[0] < TIMER_CACHE_S:
            return self._tcache[1]
        try:
            rows = self._timers()
        except Exception:  # noqa: BLE001
            log.exception("schedule: couldn't read the system timers")
            rows = []
        self._tcache = (t, rows)
        return rows

    @staticmethod
    def role(text: str) -> str:
        first = (text or "").split("\n", 1)[0][:200]
        for name, rx in ROLES:
            if rx.search(first):
                return name
        return ""

    def state(self, timers: list | None = None) -> dict:
        try:
            self.reminders.reload_if_changed()
        except Exception:  # noqa: BLE001
            pass
        rem, later, routines = [], [], []
        for i in sorted(self.reminders.items, key=lambda x: x.get("due", 0)):
            base = {"id": i.get("id"), "kind": i.get("kind", "remind"), "due": i.get("due"),
                    "text": str(i.get("text") or ""), "workspace": i.get("workspace") or None}
            rep = i.get("repeat")
            if rep:
                routines.append({**base, "days": list(rep.get("days") or []), "days_label": days_label(rep.get("days") or []),
                                 "time": rep.get("time"), "enabled": i.get("enabled") is not False,
                                 "role": self.role(base["text"])})
            elif base["kind"] == "do":
                later.append(base)
            else:
                rem.append(base)
        routines.sort(key=lambda r: (r["time"] or "", r["id"] or 0))
        life = getattr(self.cfg, "life", None)
        roles = {}
        for r in routines:
            if r["role"] and r["role"] not in roles:
                roles[r["role"]] = r["id"]
        loops = {"heartbeat": {"minutes": int(getattr(life, "heartbeat_minutes", 0) or 0), "min": HB_MIN, "max": HB_MAX,
                               "quiet_hours": list(getattr(life, "quiet_hours", []) or []),
                               "custom_prompt": bool(str(getattr(life, "heartbeat_prompt", "") or "").strip()),
                               "editable": self.initiative is not None},
                 "sleep_review": {"on": bool(getattr(life, "sleep_review", False))},
                 "routines": roles}
        projects, cur = self.projects()
        return {"now": self.now(), "reminders": rem, "later": later, "routines": routines, "loops": loops,
                "timers": self.timers() if timers is None else timers,
                "workspaces": projects, "current_workspace": cur}

    @staticmethod
    def only(state: dict, wid: str | None) -> dict:
        """The state with just one project's reminders / later jobs / routines ("none" = no project)."""
        if not wid:
            return state
        want = None if wid == "none" else wid
        out = dict(state)
        for k in ("reminders", "later", "routines"):
            out[k] = [i for i in state.get(k, []) if (i.get("workspace") or None) == want]
        out["filter"] = wid
        return out

    # -- changes
    def _item(self, rid: int) -> dict:
        try:
            self.reminders.reload_if_changed()
        except Exception:  # noqa: BLE001
            pass
        it = self.reminders.get(rid)
        if it is None:
            raise ScheduleError("that one's gone (it may have just run or been deleted)", 404, "unknown")
        return it

    def _config(self, key: str, value) -> dict:
        lv = self._live() if self._live else None
        if lv is None:
            raise ScheduleError("live settings aren't ready yet", 503, "not_ready")
        try:
            r = lv.set(key, value)
        except (ValueError, OSError) as e:
            raise ScheduleError(f"couldn't save it: {e}") from None
        if not r.get("ok", True):
            raise ScheduleError(f"couldn't apply it: {r.get('error')}", 500, "config")
        return r

    def heartbeat(self) -> dict:
        """The wake-up line and the checklist, for the Heartbeat instructions editor."""
        if self.initiative is None:
            raise ScheduleError("the heartbeat editor isn't set up", 503, "not_ready")
        life = getattr(self.cfg, "life", None)
        text, mtime, cut = self.initiative.read()
        return {"prompt": str(getattr(life, "heartbeat_prompt", "") or ""), "default_prompt": hb.DEFAULT_PROMPT,
                "silent_rule": hb.SILENT_RULE, "placeholders": ["{time}", "{home}"],
                "initiative_md": text, "initiative_md_mtime": mtime, "initiative_md_truncated": cut, "backups": self.initiative.backups(),
                "limits": {"prompt": hb.PROMPT_MAX, "initiative_bytes": hb.INITIATIVE_MAX, "backups": hb.KEEP}}

    def _apply_heartbeat(self, req: dict) -> dict:
        act = req["action"]
        if act in ("set_heartbeat_prompt", "reset_heartbeat_prompt"):
            text = req.get("text", "") if act == "set_heartbeat_prompt" else ""
            self._config("life.heartbeat_prompt", toml_value(text))
            return {"done": "Wake-up line saved." if text else "Wake-up line back to the default."}
        if self.initiative is None:
            raise ScheduleError("the heartbeat editor isn't set up", 503, "not_ready")
        try:
            if act == "set_initiative":
                if self.initiative.read()[2]:
                    raise ScheduleError("The checklist is over 60 KB on Walter, too big to edit here.", 409, "too_big")
                bid = self.initiative.save(req["text"], req["base_mtime"])
                return {"done": "Checklist saved.", "backup": bid}
            bid = self.initiative.restore(req["backup"])
            return {"done": "Earlier version restored.", "backup": bid}
        except hb.HeartbeatError as e:
            raise ScheduleError(str(e), e.status, e.code) from None

    def apply(self, req: dict) -> dict:
        act = req["action"]
        if act in HB_ACTIONS:
            return self._apply_heartbeat(req)
        if act == "heartbeat":
            self._config("life.heartbeat_minutes", req["minutes"])
            return {"done": f"Heartbeat every {req['minutes']} minutes."}
        if act == "sleep_review":
            self._config("life.sleep_review", req["on"])
            return {"done": "Sleep-time learning " + ("on." if req["on"] else "off.")}
        if act == "add":
            if len(self.reminders.items) >= ITEMS_MAX:
                raise ScheduleError(f"{ITEMS_MAX} scheduled items at most", 409, "too_many")
            ws = self.check_workspace(req.get("workspace"))
            try:
                if req["type"] == "routine":
                    it = self.reminders.add(None, req["text"], req["kind"], {"days": req["days"], "time": req["time"]},
                                            workspace=ws)
                else:
                    due = due_at(req["time"], req.get("date"), self.now())
                    it = self.reminders.add(due, req["text"], "do" if req["type"] == "later" else "remind",
                                            workspace=ws)
            except (ValueError, KeyError) as e:
                if isinstance(e, ScheduleError):
                    raise
                raise ScheduleError(str(e)) from None
            what = {"routine": "Routine", "later": "Job", "reminder": "Reminder"}[req["type"]]
            return {"done": f"{what} added.", "id": it["id"]}
        it = self._item(req["id"])
        rid = req["id"]
        if act == "delete":
            if not req["confirm"]:
                raise ScheduleError("Delete it? Send confirm to do it.", 409, "confirm")
            self.reminders.cancel(rid)
            return {"done": "Deleted.", "id": rid}
        if act in ("pause", "resume"):
            if not it.get("repeat"):
                raise ScheduleError("only routines can be paused", 400, "not_routine")
            self.reminders.set_enabled(rid, act == "resume")
            return {"done": "Paused." if act == "pause" else "Running again.", "id": rid}
        # edit
        kw = {}
        if "text" in req:
            kw["text"] = req["text"]
        if "workspace" in req:
            kw["workspace"] = self.check_workspace(req["workspace"])
        if it.get("repeat"):
            if "date" in req:
                raise ScheduleError("routines use days, not a date")
            if "time" in req or "days" in req:
                kw["repeat"] = {"days": req.get("days", it["repeat"]["days"]),
                                "time": req.get("time", it["repeat"]["time"])}
        else:
            if "days" in req:
                raise ScheduleError("one-off items use a date, not days")
            if "time" in req:
                kw["due"] = due_at(req["time"], req.get("date"), self.now())
            elif "date" in req:
                raise ScheduleError("send a time with the date")
        try:
            self.reminders.update(rid, **kw)
        except (ValueError, KeyError) as e:
            raise ScheduleError(str(e)) from None
        return {"done": "Saved.", "id": rid}


def add_routes(routes, apphub, json_body, reminders, cfg=None, conductor=None) -> Schedule:
    data = Path(os.environ.get("WALTER_DATA", Path.home() / ".walter"))
    sc = Schedule(reminders, cfg, live=lambda: getattr(conductor, "live", None),
                  backup_dir=data / "backups" / "initiative")

    @routes.get("/api/app/schedule/heartbeat")
    async def app_schedule_heartbeat(request):
        apphub.auth(request)
        try:
            return web.json_response(await asyncio.to_thread(sc.heartbeat))
        except ScheduleError as e:
            return web.json_response({"error": str(e), "code": e.code}, status=e.status)

    @routes.get("/api/app/schedule")
    async def app_schedule(request):
        apphub.auth(request)
        timers = await asyncio.to_thread(sc.timers)
        want = request.query.get("workspace") or None
        if want is not None and want != "none" and sc.ws_store().valid_id(want) is None:
            return web.json_response({"error": "no such project", "code": "unknown_workspace"}, status=400)
        return web.json_response(Schedule.only(sc.state(timers), want))

    @routes.post("/api/app/schedule")
    async def app_schedule_set(request):
        dev = apphub.auth(request)
        d = await json_body(request, BODY_MAX)
        if not sc.rate_ok():
            return web.json_response({"error": "too many changes, wait a minute", "code": "rate"}, status=429)
        try:
            req = parse(d)
            # on the event loop on purpose: the reminders runner lives there too, so no races with fire_due
            res = sc.apply(req) if req["action"] not in THREAD_ACTIONS \
                else await asyncio.to_thread(sc.apply, req)
            if req["action"] in HB_ACTIONS and sc.initiative is not None:
                res["heartbeat"] = await asyncio.to_thread(sc.heartbeat)
        except ScheduleError as e:
            return web.json_response({"error": str(e), "code": e.code}, status=e.status)
        except OSError as e:
            log.warning("app schedule: couldn't save: %s", e)
            return web.json_response({"error": "couldn't save it"}, status=500)
        log.info("app: %s schedule %s %s", dev.get("name"), req["action"],
                 req.get("id") or req.get("type") or req.get("backup") or "")
        return web.json_response({"ok": True, **res, **sc.state(sc._tcache[1] if sc._tcache else [])})

    return sc
