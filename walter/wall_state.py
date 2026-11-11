"""What the wall is REALLY showing, reported by the face itself (the owner, 11 Nov 10:08: "sometimes the
actual board and the PWA switch are not aligned").

POST /api/wall/state   the wall face (face/wallstate.js) every ~2 s on change + a 10 s heartbeat.
                       This machine's own browser or a home-network / tailnet address, never through a
                       proxy or the public tunnel; JSON, same origin, small. Strict whitelist: words from
                       fixed lists, booleans and a workspace id. No free text is accepted (no titles).
GET  /api/app/wall     the companion app (paired device auth): the last snapshot + its age; after
                       STALE_S without a report: {stale: true, ...} so the app can say "wall not responding".

Memory only (a restart forgets it; the face reports again within 2 s).
"""
from __future__ import annotations

import logging
import re
import time

from aiohttp import web

log = logging.getLogger("walter.wallstate")

MAX_BODY = 2048
STALE_S = 15.0
MODES = ("board", "face", "build", "conversation", "chat", "focus", "sleep", "presentation", "canvas",
         "browser", "setup")
ZOOMS = ("", "diff", "tests", "board", "stream", "graph", "plan", "projects", "pipeline")
PHASES = ("plan", "build")
LAYOUTS = ("compact", "large")
LAYOUT_PREFS = ("auto", "compact", "large")
ORIENTATIONS = ("portrait", "landscape")
VIEWS = ("default", "chat", "build")
FOCUS_STATUS = ("", "running", "checkin")
THEME_RE = re.compile(r"^[a-z]{1,20}$")
WS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

# the exact shape: top-level keys and nested objects (anything else is refused, so no text sneaks in)
TOP = {"t", "mode", "build", "theme", "theme_auto", "layout", "layout_pref", "orientation", "conversation",
       "focus", "demo", "workspace", "asleep", "muted", "canvas", "browser"}
BUILD_KEYS = {"on", "sticky", "showing", "phase", "zoom", "plan"}
CONVO_KEYS = {"on", "view"}
FOCUS_KEYS = {"on", "status"}


class Invalid(ValueError):
    pass


def _bool(d, k):
    v = d.get(k, False)
    if not isinstance(v, bool):
        raise Invalid(f"{k} must be true or false")
    return v


def _word(d, k, allowed, dflt=None):
    v = d.get(k, dflt)
    if not isinstance(v, str) or v not in allowed:
        raise Invalid(f"{k} must be one of " + ", ".join(a or '""' for a in allowed))
    return v


def _obj(d, k, keys):
    v = d.get(k, {})
    if not isinstance(v, dict):
        raise Invalid(f"{k} must be an object")
    extra = set(v) - keys
    if extra:
        raise Invalid(f"{k}: unknown field {sorted(extra)[0]}")
    return v


def parse(d) -> dict:
    """Strict validation of a face report. Returns the cleaned snapshot or raises Invalid."""
    if not isinstance(d, dict):
        raise Invalid("send a JSON object")
    extra = set(d) - TOP
    if extra:
        raise Invalid(f"unknown field {sorted(extra)[0]}")
    t = d.get("t")
    if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 < t < 1e11:
        raise Invalid("t must be epoch seconds")
    b = _obj(d, "build", BUILD_KEYS)
    c = _obj(d, "conversation", CONVO_KEYS)
    f = _obj(d, "focus", FOCUS_KEYS)
    theme = d.get("theme", "")
    if not isinstance(theme, str) or (theme and not THEME_RE.match(theme)):
        raise Invalid("theme must be a theme name")
    ws = d.get("workspace")
    if ws is not None and (not isinstance(ws, str) or not WS_RE.match(ws)):
        raise Invalid("workspace must be a workspace id")
    return {
        "t": int(t),
        "mode": _word(d, "mode", MODES),
        "build": {"on": _bool(b, "on"), "sticky": _bool(b, "sticky"), "showing": _bool(b, "showing"),
                  "phase": _word(b, "phase", PHASES, "build"), "zoom": _word(b, "zoom", ZOOMS, ""),
                  "plan": _bool(b, "plan")},
        "theme": theme, "theme_auto": _bool(d, "theme_auto"),
        "layout": _word(d, "layout", LAYOUTS, "compact"),
        "layout_pref": _word(d, "layout_pref", LAYOUT_PREFS, "auto"),
        "orientation": _word(d, "orientation", ORIENTATIONS, "portrait"),
        "conversation": {"on": _bool(c, "on"), "view": _word(c, "view", VIEWS, "default")},
        "focus": {"on": _bool(f, "on"), "status": _word(f, "status", FOCUS_STATUS, "")},
        "demo": _bool(d, "demo"),
        "workspace": None if _bool(d, "demo") else ws,
        "asleep": _bool(d, "asleep"), "muted": _bool(d, "muted"),
        "canvas": _bool(d, "canvas"), "browser": _bool(d, "browser"),
    }


class WallState:
    def __init__(self, now=time.monotonic):
        self.now = now
        self.snap: dict | None = None
        self.at = 0.0
        self.mode_seen = ""

    def put(self, snap: dict):
        if snap["mode"] != self.mode_seen:
            log.info("wall: showing %s", snap["mode"])
            self.mode_seen = snap["mode"]
        self.snap, self.at = snap, self.now()

    def get(self) -> dict:
        if self.snap is None:
            return {"stale": True, "reported": False, "age": None}
        age = max(0.0, self.now() - self.at)
        out = {**self.snap, "stale": age > STALE_S, "reported": True, "age": round(age, 1)}
        return out


def local_ok(peer, headers) -> bool:
    """The wall's own browser, or a home-network / tailnet face; never via a proxy (= the tunnel)."""
    from . import dashboard as dash
    from . import devices as devmod
    if devmod.proxied(peer, headers):
        return False
    return devmod.is_kiosk(peer, headers) or dash.is_private_ip(peer) or devmod.is_tailnet(peer)


def handle_post(store: WallState, peer, headers, host: str, length, raw: bytes | None) -> tuple[int, dict]:
    """POST /api/wall/state, testable without aiohttp. raw = the body (None if it was too big)."""
    import json
    if not local_ok(peer, headers):
        return 403, {"error": "local only"}
    from .server import dash_check_request
    code, why = dash_check_request(headers, host, length)
    if code:
        return code, {"error": why}
    if raw is None or len(raw) > MAX_BODY:
        return 413, {"error": "too big"}
    try:
        d = json.loads(raw or b"{}")
        snap = parse(d)
    except ValueError as e:   # Invalid and JSON errors
        return 400, {"error": str(e) if isinstance(e, Invalid) else "send a JSON object"}
    store.put(snap)
    return 200, {"ok": True}


def add_routes(routes, store: WallState, apphub=None):
    """POST /api/wall/state (face) on the main routes; GET /api/app/wall (app) when the app is on."""
    @routes.post("/api/wall/state")
    async def wall_state_post(request):
        raw = await request.content.read(MAX_BODY + 1)
        status, out = handle_post(store, request.remote, request.headers, request.host,
                                  request.content_length, raw if len(raw) <= MAX_BODY else None)
        return web.json_response(out, status=status)

    @routes.get("/api/wall/state")
    async def wall_state_get(request):
        """walterctl wall: this machine only."""
        from . import devices as devmod
        if not devmod.is_kiosk(request.remote, request.headers):
            return web.json_response({"error": "local only"}, status=403)
        return web.json_response(store.get())

    if apphub is not None:
        @routes.get("/api/app/wall")
        async def app_wall(request):
            apphub.auth(request)
            return web.json_response(store.get())
