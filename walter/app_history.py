"""Companion app chat history (the owner, 10 Nov 13:01): read-only paging + search over walter/chatlog.py.

GET /api/app/history?before=<id>&limit=<1..100>&q=<text>
    -> {"items": [newest first: {id, ts, role, text, source, workspace}], "has_more": bool}

Paired devices only (apphub.auth, like every /api/app/* call). Strict validation: before is a
positive integer, limit 1..100 (default 50), q 1..100 printable characters; anything else is a 400.
"""
from __future__ import annotations

import asyncio
import re

from aiohttp import web

from .chatlog import ChatLog

LIMIT_MAX = 100
LIMIT_DEFAULT = 50
Q_MAX = 100
ALLOWED = {"before", "limit", "q"}


def parse(query) -> dict:
    extra = set(query.keys()) - ALLOWED
    if extra:
        raise ValueError("unknown parameter: " + sorted(extra)[0][:20])
    out: dict = {"before": None, "limit": LIMIT_DEFAULT, "q": None}
    if "before" in query:
        b = query.get("before", "")
        if not re.fullmatch(r"[1-9][0-9]{0,11}", b or ""):
            raise ValueError("before must be a positive whole number")
        out["before"] = int(b)
    if "limit" in query:
        lim = query.get("limit", "")
        if not re.fullmatch(r"[1-9][0-9]{0,2}", lim or "") or int(lim) > LIMIT_MAX:
            raise ValueError(f"limit must be 1 to {LIMIT_MAX}")
        out["limit"] = int(lim)
    if "q" in query:
        q = " ".join((query.get("q") or "").split())
        if not q or len(q) > Q_MAX or any(ord(c) < 32 or ord(c) == 127 for c in q):
            raise ValueError(f"q must be 1 to {Q_MAX} characters")
        out["q"] = q
    return out


def add_routes(routes, apphub, store: ChatLog | None = None):
    def log():
        return store or ChatLog()

    @routes.get("/api/app/history")
    async def app_history(request):
        apphub.auth(request)
        try:
            p = parse(request.query)
        except ValueError as e:
            return web.json_response({"error": str(e), "code": "bad_request"}, status=400)
        res = await asyncio.to_thread(log().page, p["before"], p["limit"], p["q"])
        return web.json_response(res, headers={"Cache-Control": "no-store"})
