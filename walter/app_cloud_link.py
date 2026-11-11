"""Companion app Settings > Cloud Link card (walter/cloud_link.py, docs/CLOUD-LINK.md).

GET  /api/app/cloud-link            {enabled, url, connector, remote_status, last_heartbeat, via_link, ...}
                                    (?refresh=1 also asks the Cloud Link service)
POST /api/app/cloud-link  {"action": "enable", "code": "...", "replace": bool}
                          {"action": "disable", "confirm": true}
                          {"action": "rotate"}

Paired devices only. Every action runs on this box (claim, cloudflared unit, config);
nothing is relayed. The app itself may be reached THROUGH the link, so turning it
off needs {"confirm": true}; without it the answer is 409 with a warning that says
"you'll lose remote access" when the request came in over the link.
Enable attempts are rate-limited (the service limits them too).
"""
from __future__ import annotations

import logging
import time

from aiohttp import web

from . import cloud_link as CL
from . import devices as D

log = logging.getLogger("walter.app.cloud_link")

ENABLE_MAX = 5           # enable attempts...
ENABLE_WINDOW_S = 600    # ...per 10 minutes


def via_link(request, host: str) -> bool:
    """Did this request come in through the Cloud Link tunnel itself?"""
    if not host or not D.proxied(request.remote, request.headers):
        return False
    return D.host_name(request.headers.get("Host", "")).lower() == host.lower()


def warning(remote: bool) -> str:
    if remote:
        return ("You're connected through Cloud Link right now: turning it off means you'll lose remote "
                "access, and this app stops working until you're back on the home network.")
    return "Turning Cloud Link off means you'll lose remote access: the app works on your home network only."


class Card:
    def __init__(self, cfg, live=None, make=None):
        self.cfg = cfg
        self.live = live
        self._make = make
        self._tries: list[float] = []

    def manager(self) -> CL.CloudLink:
        if self._make is not None:
            return self._make()
        kw = {}
        if self.live is not None:
            kw["set_public_url"] = lambda url: self.live.set("app.public_url", url) and ""
            kw["get_public_url"] = lambda: self.cfg.app.public_url or ""
        return CL.CloudLink.from_config(self.cfg, **kw)

    def allow_enable(self) -> bool:
        now = time.monotonic()
        self._tries = [t for t in self._tries if now - t < ENABLE_WINDOW_S]
        if len(self._tries) >= ENABLE_MAX:
            return False
        self._tries.append(now)
        return True


def add_routes(routes, apphub, json_body, cfg, conductor=None, make=None) -> Card:
    card = Card(cfg, getattr(conductor, "live", None), make)

    def fail(e: CL.CloudLinkError, status: int = 400):
        return web.json_response({"ok": False, "error": str(e), "code": e.code}, status=status)

    @routes.get("/api/app/cloud-link")
    async def cl_status(request):
        apphub.auth(request)
        m = card.manager()
        st = await m.status(remote=request.query.get("refresh") == "1")
        st["via_link"] = via_link(request, st.get("host") or "")
        st.pop("api_base", None)
        return web.json_response(st)

    @routes.post("/api/app/cloud-link")
    async def cl_action(request):
        dev = apphub.auth(request)
        d = await json_body(request, 1024)
        action = d.get("action")
        m = card.manager()
        host = m.state().get("host") or ""
        remote = via_link(request, host)
        try:
            if action == "enable":
                code = d.get("code")
                if not isinstance(code, str) or not code.strip():
                    return web.json_response({"ok": False, "error": "Type the invite code first.",
                                              "code": "bad_code"}, status=400)
                if not card.allow_enable():
                    return web.json_response({"ok": False, "error": "Too many tries; wait ten minutes.",
                                              "code": "rate_limited"}, status=429)
                r = await m.enable(code, replace=d.get("replace") is True)
                log.info("app: %s turned Cloud Link on (%s)", dev.get("name"), r.get("host"))
                return web.json_response(r)
            if action == "disable":
                if d.get("confirm") is not True:
                    return web.json_response({"ok": False, "needs_confirm": True, "via_link": remote,
                                              "warning": warning(remote)}, status=409)
                r = await m.disable()
                log.info("app: %s turned Cloud Link off (remote=%s)", dev.get("name"), remote)
                return web.json_response({**r, "via_link": remote})
            if action == "rotate":
                r = await m.rotate()
                log.info("app: %s rotated the Cloud Link token", dev.get("name"))
                return web.json_response(r)
        except CL.CloudLinkError as e:
            status = {"existing_tunnel": 409, "already_on": 409, "rate_limited": 429, "offline": 502,
                      "off": 409}.get(e.code, 400)
            return fail(e, status)
        return web.json_response({"ok": False, "error": "action must be enable, disable or rotate",
                                  "code": "bad_request"}, status=400)

    return card
