"""Companion app Projects screen: list, switch, add and edit workspaces (the owner approved 11 Nov 10:05).

GET  /api/app/workspaces   {defined, current, max_share, workspaces: [{id, name, current, walled, focus, share,
                            running, ready, goals}]}
       WALLED workspaces only ever show {id, name, current, walled, share, running, ready}: no focus text, no
       goals, no titles (even when one is the current workspace: a phone can be anywhere).
POST /api/app/workspaces
       {action: "switch", id}                          the WALL face's current workspace (same helper as
                                                       `walterctl workspace switch`)
       {action: "add", name, walled?: bool}            same helper as `walterctl workspace add` (+ privacy)
       {action: "set", id, key, value, confirm?: bool} whitelist: name, goals (list), budget_share (0..1),
                                                       privacy (normal|walled), aliases (list). Walled -> normal
                                                       needs confirm: true (else 409 code "confirm_unlock").
No delete: removing a workspace stays a manual / walterctl thing.

Everything is whitelisted: ids must be existing exact ids, names are short plain text (letters, digits,
spaces and a few marks), lists are capped. A small per-process rate limit keeps a stuck phone from
hammering the store.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
import time

from aiohttp import web

from . import workspaces as WS

log = logging.getLogger("walter.app.workspaces")

ACTIONS = ("switch", "add", "set")
SET_KEYS = ("name", "goals", "budget_share", "privacy", "aliases")
NAME_MAX = 40
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 .&'+()-]*")
GOAL_MAX, GOALS_MAX = 120, 8
ALIAS_MAX, ALIASES_MAX = 40, 10
WORKSPACES_MAX = 30          # the app can't grow the list without bound
BODY_MAX = 4096
RATE_N, RATE_S = 30, 60      # at most 30 changes a minute from the app (all devices together)
_CTRL = re.compile(r"[\x00-\x1f\x7f  ]")


class WsError(ValueError):
    def __init__(self, msg: str, status: int = 400, code: str = "bad_request"):
        super().__init__(msg)
        self.status, self.code = status, code


# ------------------------------------------------------------------ validation
def clean_name(v) -> str:
    if not isinstance(v, str):
        raise WsError("name must be text")
    if _CTRL.search(v):
        raise WsError("name: one line, no control characters")
    s = " ".join(v.split())
    if not 1 <= len(s) <= NAME_MAX:
        raise WsError(f"name: 1 to {NAME_MAX} characters")
    if not NAME_RE.fullmatch(s) or not WS.slug(s):
        raise WsError("name: letters, numbers, spaces and . & ' + ( ) - only")
    return s


def _clean_line(v, cap: int, what: str) -> str:
    if not isinstance(v, str):
        raise WsError(f"{what} must be text")
    if _CTRL.search(v):
        raise WsError(f"{what}: one line each, no control characters")
    s = " ".join(v.split())
    if len(s) > cap:
        raise WsError(f"{what}: {cap} characters at most")
    return s


def clean_list(v, cap: int, n: int, what: str) -> list[str]:
    if not isinstance(v, list):
        raise WsError(f"{what} must be a list")
    if len(v) > n:
        raise WsError(f"{what}: {n} at most")
    out = []
    for x in v:
        s = _clean_line(x, cap, what)
        if s and s not in out:
            out.append(s)
    return out


def clean_aliases(v) -> list[str]:
    out = []
    for a in clean_list(v, ALIAS_MAX, ALIASES_MAX, "aliases"):
        n = WS.norm(a)
        if n and n not in out:
            out.append(n)
    return out


def clean_share(v) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1:
        raise WsError("budget_share must be a number from 0 to 1")
    return round(float(v), 3)


def clean_id(v) -> str:
    if not isinstance(v, str) or not WS._ID.fullmatch(v):
        raise WsError("id must be a workspace id")
    return v


def parse(d) -> dict:
    """Strict validation of a POST body (shape only; store checks come in apply)."""
    if not isinstance(d, dict):
        raise WsError("send a JSON object")
    act = d.get("action")
    if act not in ACTIONS:
        raise WsError("action must be one of " + ", ".join(ACTIONS))
    if act == "switch":
        return {"action": act, "id": clean_id(d.get("id"))}
    if act == "add":
        walled = d.get("walled", False)
        if not isinstance(walled, bool):
            raise WsError("walled must be true or false")
        return {"action": act, "name": clean_name(d.get("name")), "walled": walled}
    key = d.get("key")
    if key not in SET_KEYS:
        raise WsError("key must be one of " + ", ".join(SET_KEYS))
    v = d.get("value")
    if key == "name":
        v = clean_name(v)
    elif key == "goals":
        v = clean_list(v, GOAL_MAX, GOALS_MAX, "goals")
    elif key == "aliases":
        v = clean_aliases(v)
    elif key == "budget_share":
        v = clean_share(v)
    elif key == "privacy":
        if v not in WS.PRIVACY:
            raise WsError("privacy must be normal or walled")
    confirm = d.get("confirm", False)
    if not isinstance(confirm, bool):
        raise WsError("confirm must be true or false")
    return {"action": act, "id": clean_id(d.get("id")), "key": key, "value": v, "confirm": confirm}


# ------------------------------------------------------------------ the store side
class Projects:
    def __init__(self, store=None, overview=None, refresh=None, now=time.monotonic):
        self._store = store
        self._overview = overview       # tests pass a fake; default cockpit.workspaces_overview
        self._refresh = refresh if refresh is not None else WS.refresh_build_view
        self.now = now
        self._hits: list[float] = []

    def store(self):
        return self._store or WS.WorkspaceStore()

    def rate_ok(self) -> bool:
        t = self.now()
        self._hits = [h for h in self._hits if t - h < RATE_S]
        if len(self._hits) >= RATE_N:
            return False
        self._hits.append(t)
        return True

    def _badges(self, st) -> dict:
        try:
            if self._overview is not None:
                rows = self._overview(WS.WALL, st)
            else:
                from . import cockpit
                rows = cockpit.workspaces_overview(WS.WALL, store=st)
        except Exception:  # noqa: BLE001
            log.exception("app workspaces: couldn't count workers / backlog")
            rows = []
        return {r.get("id"): r for r in rows if isinstance(r, dict)}

    def state(self) -> dict:
        st = self.store()
        defined = st.defined()
        cur = st.current_id(WS.WALL)
        rows = self._badges(st) if defined else {}
        out = []
        for w in st.all():
            r = rows.get(w["id"]) or {}
            b = r.get("badges") or {}
            walled = w["privacy"] == "walled"
            item = {"id": w["id"], "name": w["name"], "current": w["id"] == cur, "walled": walled,
                    "share": round(float(w["budget_share"]), 3),
                    "running": int(b.get("running", 0) or 0), "ready": int(b.get("ready", 0) or 0)}
            if not walled:
                item["focus"] = r.get("focus") if isinstance(r.get("focus"), str) else None
                item["goals"] = [str(g)[:GOAL_MAX] for g in w["goals"]][:GOALS_MAX]
            out.append(item)
        return {"defined": defined, "current": cur, "workspaces": out}

    def _after(self, st):
        try:
            st.publish(WS.WALL)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._refresh()
        except Exception:  # noqa: BLE001
            pass

    def _clash(self, st, name: str, skip: str | None = None):
        n = WS.norm(name)
        for w in st.all():
            if w["id"] == skip:
                continue
            if n in (WS.norm(w["id"]), WS.norm(w["name"])) or WS.slug(name) == w["id"]:
                raise WsError(f"there's already a project called {w['name']}", 409, "duplicate")

    def apply(self, req: dict) -> dict:
        st = self.store()
        act = req["action"]
        if act == "switch":
            status, res = WS.switch_request({"id": req["id"]}, WS.WALL, st)
            if status != 200:
                raise WsError(res.get("error", "couldn't switch"), status if status == 404 else 400,
                              "unknown" if status != 404 else "none")
            self._after(st)
            return {"done": f"The wall is on {res['name']} now.", "id": res["id"]}
        if act == "add":
            st.ensure()
            if len(st.all()) >= WORKSPACES_MAX:
                raise WsError(f"{WORKSPACES_MAX} projects at most", 409, "too_many")
            self._clash(st, req["name"])
            if len(WS.slug(req["name"])) > NAME_MAX:
                raise WsError("name too long")
            try:
                w = st.add(req["name"], **({"privacy": "walled"} if req["walled"] else {}))
            except ValueError as e:
                raise WsError(str(e), 409, "duplicate") from None
            self._after(st)
            return {"done": f"Added {w['name']}" + (" (walled off)." if req["walled"] else "."), "id": w["id"]}
        # set
        if not st.defined() or req["id"] not in st.ids():
            raise WsError("unknown project", 404, "unknown")
        w = next(x for x in st.all() if x["id"] == req["id"])
        key, v = req["key"], req["value"]
        if key == "privacy" and w["privacy"] == "walled" and v == "normal" and not req["confirm"]:
            raise WsError(f"{w['name']} is walled off. Opening it lets other projects and workers see its "
                          "notes and files. Send confirm to do it.", 409, "confirm_unlock")
        if key == "name":
            self._clash(st, v, skip=w["id"])
        if key == "budget_share":
            v = repr(v)          # store.set reads text; 0..1 stays as is
        try:
            nw = st.set(w["id"], key, v)
        except (KeyError, ValueError) as e:
            raise WsError(str(e.args[0] if e.args else e)) from None
        if key == "privacy":
            st.ensure_folder(nw)   # walled folders get 0700
        self._after(st)
        return {"done": f"Saved {nw['name']}.", "id": nw["id"]}


def add_routes(routes, apphub, json_body, store=None) -> Projects:
    pj = Projects(store)

    @routes.get("/api/app/workspaces")
    async def app_workspaces(request):
        apphub.auth(request)
        return web.json_response(await asyncio.to_thread(pj.state))

    @routes.post("/api/app/workspaces")
    async def app_workspaces_set(request):
        dev = apphub.auth(request)
        d = await json_body(request, BODY_MAX)
        if not pj.rate_ok():
            return web.json_response({"error": "too many changes, wait a minute", "code": "rate"}, status=429)
        try:
            req = parse(d)
            res = await asyncio.to_thread(pj.apply, req)
        except WsError as e:
            return web.json_response({"error": str(e), "code": e.code}, status=e.status)
        except OSError as e:
            log.warning("app workspaces: couldn't save: %s", e)
            return web.json_response({"error": "couldn't save it"}, status=500)
        log.info("app: %s projects %s %s", dev.get("name"), req["action"], req.get("id") or "(new)")
        return web.json_response({"ok": True, **res, **(await asyncio.to_thread(pj.state))})

    return pj
