"""Companion app Rotate button: turn the wall's screen portrait / landscape / flip (the owner, 10 Nov 12:57).

GET  /api/app/rotate   {orientation, outputs, saved, portrait_transform, live, error, choices}
POST /api/app/rotate   {orientation: "portrait"|"landscape"|"flip"}  -> {ok, orientation, outputs, saved, error, ...}

Strict whitelist; the work is screen_rotate.py (wlr-randr live + WALTER_ROTATE in ~/.walter/kiosk.env),
the same helper `walterctl display rotate portrait|landscape|flip` uses.
"""
from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from . import screen_rotate as R

log = logging.getLogger("walter.app.rotate")


def parse(d) -> str:
    if not isinstance(d, dict):
        raise ValueError("send a JSON object")
    o = d.get("orientation")
    if not isinstance(o, str) or o not in R.ORIENTATIONS:
        raise ValueError("orientation must be one of " + ", ".join(R.ORIENTATIONS))
    return o


def add_routes(routes, apphub, json_body):
    @routes.get("/api/app/rotate")
    async def app_rotate(request):
        apphub.auth(request)
        st = await asyncio.to_thread(R.current)
        return web.json_response({**st, "choices": list(R.ORIENTATIONS)})

    @routes.post("/api/app/rotate")
    async def app_rotate_set(request):
        dev = apphub.auth(request)
        d = await json_body(request, 512)
        try:
            o = parse(d)
        except ValueError as e:
            return web.json_response({"error": str(e), "code": "bad_request"}, status=400)
        res = await asyncio.to_thread(R.set_orientation, o)
        log.info("app: %s turned the screen %s -> %s (ok=%s)", dev.get("name"), o, res.get("orientation"),
                 res.get("ok"))
        # 200 even when wlr-randr failed: the choice may still be saved; the app shows res["error"]
        return web.json_response({**res, "choices": list(R.ORIENTATIONS)})
