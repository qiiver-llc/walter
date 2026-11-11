"""Companion app Settings > Add-ons (walter/addons.py, docs/ADDONS.md).

GET  /api/app/addons                 {"addons": [installed...], "walled": bool}
GET  /api/app/addons/catalog         the catalog, each entry marked installed / update / on
POST /api/app/addons  {"action": "on", "id", "confirm": true}     (409 needs_confirm + permissions without it)
                      {"action": "off", "id"} | {"action": "off_all", "confirm": true}   (the kill switch)
                      {"action": "setting", "id", "key", "value"}
                      {"action": "secret", "id", "key"}            -> {"url"} private phone page (+ QR on the wall)
                      {"action": "install", "id"} | {"action": "update", "id", "confirm": true}
                      {"action": "remove", "id", "confirm": true}
                      {"action": "preview", "url"}                 -> name, permissions, "not made by Qiiver" warning
                      {"action": "add", "url", "commit", "confirm": true}   pinned to the previewed commit

Paired devices only. Secrets never pass through here: the owner types them into the drop page.
Network and disk work runs off the event loop.
"""
from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from . import addons as A

log = logging.getLogger("walter.app.addons")

STATUS = {"not_installed": 404, "not_found": 404, "exists": 409, "off": 409, "walled": 409, "too_old": 409,
          "offline": 502, "rate_limited": 429, "sha_mismatch": 422, "traversal": 422, "link": 422,
          "too_big": 422, "bad_archive": 422, "bad_manifest": 422, "bad_catalog": 502}


def _perms(m: dict) -> list[dict]:
    return [{"id": p, "label": A.PERMISSIONS[p], "danger": p in A.DANGER} for p in m.get("permissions", [])]


def add_routes(routes, apphub, json_body, make=None, start_drop=None):
    make = make or (lambda: A.Addons())
    start_drop = start_drop or (lambda s, aid, key: A.start_secret_drop(aid, key, s))

    async def off_loop(fn, *a):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *a)

    def fail(e: A.AddonError):
        return web.json_response({"ok": False, "error": str(e), "code": e.code}, status=STATUS.get(e.code, 400))

    @routes.get("/api/app/addons")
    async def addons_list(request):
        apphub.auth(request)
        s = make()
        items = await off_loop(s.list)
        return web.json_response({"addons": items, "walled": await off_loop(A.current_walled)})

    @routes.get("/api/app/addons/catalog")
    async def addons_catalog(request):
        apphub.auth(request)
        try:
            return web.json_response(await off_loop(make().catalog))
        except A.AddonError as e:
            return fail(e)

    @routes.post("/api/app/addons")
    async def addons_action(request):
        dev = apphub.auth(request)
        d = await json_body(request, 4096)
        action, aid = d.get("action"), d.get("id")
        s = make()
        who = dev.get("name") if isinstance(dev, dict) else "app"
        try:
            if action in ("on", "off", "setting", "secret", "install", "update", "remove") and \
                    (not isinstance(aid, str) or not A.ID_RE.match(aid)):
                raise A.AddonError("pick an add-on", "bad_id")
            if action == "on":
                m = s.installed(aid)
                if m is None:
                    raise A.AddonError("that add-on isn't installed", "not_installed")
                if m["permissions"] and d.get("confirm") is not True:
                    return web.json_response({"ok": False, "needs_confirm": True, "name": m["name"],
                                              "permissions": _perms(m)}, status=409)
                await off_loop(s.enable, aid)
                log.info("app: %s turned add-on %s on", who, aid)
                return web.json_response({"ok": True, "addon": s.describe(s.installed(aid)),
                                          "mcp_note": "Its tools arrive after Walter's next new session."
                                          if m.get("mcp") else ""})
            if action == "off":
                await off_loop(s.disable, aid)
                log.info("app: %s turned add-on %s off", who, aid)
                return web.json_response({"ok": True})
            if action == "off_all":
                if d.get("confirm") is not True:
                    return web.json_response({"ok": False, "needs_confirm": True}, status=409)
                ids = await off_loop(s.disable_all)
                log.warning("app: %s used the add-ons kill switch", who)
                return web.json_response({"ok": True, "turned_off": ids})
            if action == "setting":
                key = d.get("key")
                if not isinstance(key, str):
                    raise A.AddonError("which setting?", "bad_key")
                v = await off_loop(s.set_setting, aid, key, d.get("value"))
                return web.json_response({"ok": True, "key": key, "value": v})
            if action == "secret":
                key = d.get("key")
                if not isinstance(key, str) or not A.KEY_RE.match(key):
                    raise A.AddonError("which secret?", "bad_key")
                url = await off_loop(start_drop, s, aid, key)
                log.info("app: %s opened the secret page for %s/%s", who, aid, key)
                return web.json_response({"ok": True, "url": url, "minutes": 15})
            if action == "install":
                r = await off_loop(s.install, aid)
                log.info("app: %s installed add-on %s", who, aid)
                return web.json_response({"ok": True, "addon": r})
            if action == "update":
                m = s.installed(aid)
                if m is None:
                    raise A.AddonError("that add-on isn't installed", "not_installed")
                if (m.get("source") or {}).get("origin") == "url" and d.get("confirm") is not True:
                    p = await off_loop(s.preview_url, m["source"]["url"])
                    return web.json_response({"ok": False, "needs_confirm": True, "preview": p}, status=409)
                r = await off_loop(s.update, aid)
                return web.json_response({"ok": True, "addon": r})
            if action == "remove":
                if d.get("confirm") is not True:
                    return web.json_response({"ok": False, "needs_confirm": True}, status=409)
                await off_loop(s.remove, aid)
                log.info("app: %s removed add-on %s", who, aid)
                return web.json_response({"ok": True})
            if action in ("preview", "add"):
                url = d.get("url")
                if not isinstance(url, str) or not url.strip():
                    raise A.AddonError("paste a GitHub repo address", "bad_url")
                p = await off_loop(s.preview_url, url)
                if action == "preview":
                    return web.json_response({"ok": True, "preview": p})
                if d.get("confirm") is not True:
                    return web.json_response({"ok": False, "needs_confirm": True, "preview": p}, status=409)
                commit = d.get("commit") or p["commit"]
                if not isinstance(commit, str) or not A.SHA_RE.match(commit):
                    raise A.AddonError("bad commit", "bad_url")
                r = await off_loop(lambda: s.install_url(url, commit, update=p["installed"]))
                log.info("app: %s added add-on %s from %s@%s", who, r["id"], p["repo"], commit[:10])
                return web.json_response({"ok": True, "addon": r})
        except A.AddonError as e:
            return fail(e)
        return web.json_response({"ok": False, "error": "unknown action", "code": "bad_request"}, status=400)
