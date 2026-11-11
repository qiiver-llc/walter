"""Hands, the brain's side (docs/HANDS.md): relays computer-use requests to a laptop face,
shows "About to: ..." on the wall and takes the yes/no, logs everything (redacted).

Routes (added by server.build_app; faces are LAN/tailnet only via server.gate_ok):

  face agent (Bearer face token, desktop/hands_agent.py):
    POST /api/face/hands/hello          capabilities + whether its kill switch is on
    GET  /api/face/hands/next?wait=N    the next request for this face (long poll), or {"stop": true}
    POST /api/face/hands/ask            {id, risk, summary, reason}: needs a person's yes -> wall card
    GET  /api/face/hands/decision/<id>  {decision: allow|deny|null, how} (long poll)
    POST /api/face/hands/decision       {id, allow}: tapped on the laptop's own dialog
    POST /api/face/hands/result         {id, ok, text?, error?, image_b64?}
  this machine only (walterctl hands ..., kiosk):
    GET  /api/hands/status
    POST /api/hands/request             {face, action, params} -> {id}
    GET  /api/hands/request/<id>?wait=N state + result (text wrapped in <untrusted>, image saved to a temp file)
    POST /api/hands/approve             {id, allow}: Walter heard the owner say yes / no out loud
    POST /api/hands/stop | /api/hands/start | /api/hands/enable | /api/hands/disable

Brain state: ~/.walter/hands.json {enabled (default false), stopped}. Audit: ~/.walter/hands-audit.log.
The laptop decides risk again itself (walter.hands.Gate): the brain can't skip its checks.
"""
from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import os
import secrets
import shutil
import tempfile
import time
from pathlib import Path

from aiohttp import web

from . import hands as H

log = logging.getLogger("walter.hands")
REQ_TTL_S = 900           # requests are forgotten after this
QUEUE_TTL_S = 120         # a request nobody picked up expires
SHOT_TTL_S = 600          # screenshots on the brain are deleted after this
MAX_RESULT_BODY = 12_000_000


def data_dir() -> Path:
    from .devices import data_dir as dd   # ~/.walter (WALTER_DATA in tests)
    return dd()


def state_path() -> Path:
    return data_dir() / "hands.json"


def audit_path() -> Path:
    return data_dir() / "hands-audit.log"


def load_state(path: Path | None = None) -> dict:
    try:
        d = json.loads((path or state_path()).read_text())
        return {"enabled": bool(d.get("enabled")), "stopped": bool(d.get("stopped"))}
    except (OSError, ValueError, AttributeError):
        return {"enabled": False, "stopped": False}


def save_state(d: dict, path: Path | None = None):
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"enabled": bool(d.get("enabled")), "stopped": bool(d.get("stopped"))}, f)
    os.replace(tmp, path)


class HandsHub:
    def __init__(self, store=None, conductor=None, card=None, state_file: Path | None = None,
                 audit_file: Path | None = None, clock=time.time):
        self.store = store                  # faces.FaceStore (names -> ids)
        self.conductor = conductor          # for "is someone around" (presence)
        self.card = card                    # fn(title, body, seconds) -> the wall card
        self.state_file = state_file or state_path()
        self.audit_file = audit_file or audit_path()
        self.clock = clock
        self.state = load_state(state_file)
        self.reqs: dict[str, dict] = {}
        self.queues: dict[str, collections.deque] = {}
        self.waiters: dict[str, asyncio.Event] = {}
        self.caps: dict[str, dict] = {}     # face id -> last hello
        self.shots = Path(tempfile.mkdtemp(prefix="walter-hands-"))
        os.chmod(self.shots, 0o700)

    # ---- state
    def save(self):
        save_state(self.state, self.state_file)

    def audit(self, event: str, **kw):
        H.audit(self.audit_file or audit_path(), event, **kw)

    def _wake(self, key: str):
        ev = self.waiters.pop(key, None)
        if ev is not None:
            ev.set()

    async def _wait(self, key: str, timeout: float):
        ev = self.waiters.setdefault(key, asyncio.Event())
        try:
            await asyncio.wait_for(ev.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    def owner_present(self) -> bool | None:
        c = self.conductor
        if c is None:
            return None
        if getattr(c, "sleeping", False):
            return False
        if self.clock() - float(getattr(c, "last_speech", 0) or 0) < 600:
            return True
        if getattr(c, "dozing", False):
            return False
        return None

    def sweep(self):
        now = self.clock()
        for rid, r in list(self.reqs.items()):
            if r["status"] == "queued" and now - r["created"] > QUEUE_TTL_S:
                r.update(status="expired", error="the laptop didn't pick it up (is walter-desktop running with hands on?)")
                self._wake("req:" + rid)
            if now - r["created"] > REQ_TTL_S:
                self.reqs.pop(rid, None)
        for f in self.shots.glob("*.png"):
            try:
                if now - f.stat().st_mtime > SHOT_TTL_S:
                    f.unlink()
            except OSError:
                pass

    def close(self):
        shutil.rmtree(self.shots, ignore_errors=True)

    # ---- the brain asks
    def face_ref(self, ref: str) -> dict:
        f = self.store.get(ref) if self.store is not None else None
        if f is None:
            raise KeyError(f"no face called {ref!r} (walterctl faces)")
        return f

    def request(self, ref: str, action: str, params: dict) -> dict:
        if not self.state["enabled"]:
            raise PermissionError("hands are off on the brain (walterctl hands enable)")
        if self.state["stopped"]:
            raise PermissionError("hands are stopped (walterctl hands start to resume)")
        if action not in H.ACTIONS:
            raise ValueError(f"unknown action {action!r}; one of: {', '.join(sorted(H.ACTIONS))}")
        if not isinstance(params, dict):
            raise ValueError("params must be an object")
        face = self.face_ref(ref)
        caps = self.caps.get(face["id"])
        if not caps or self.clock() - caps.get("at", 0) > 120:
            raise LookupError(f"{face['name']} has no hands online (walter-desktop run there, with hands enabled)")
        if not caps.get("enabled"):
            raise LookupError(f"hands are switched off on {face['name']} (walter-desktop hands enable, at the laptop)")
        if caps.get("stopped"):
            raise LookupError(f"{face['name']}'s kill switch is on (walter-desktop hands start, at the laptop)")
        self.sweep()
        rid = secrets.token_hex(3)
        while rid in self.reqs:
            rid = secrets.token_hex(3)
        r = {"id": rid, "face_id": face["id"], "face": face["name"], "action": action, "params": params,
             "status": "queued", "created": self.clock(), "summary": H.describe(action, params)}
        self.reqs[rid] = r
        self.queues.setdefault(face["id"], collections.deque()).append(rid)
        self._wake("face:" + face["id"])
        self.audit("request", id=rid, face=face["name"], action=action, params=params)
        return self.public(r)

    def public(self, r: dict) -> dict:
        out = {k: r.get(k) for k in ("id", "face", "action", "status", "summary", "risk", "reason", "how", "error")
               if r.get(k) not in (None, "")}
        res = r.get("result")
        if res is not None:
            if res.get("text"):
                out["result"] = H.wrap_untrusted(res["text"], f"{r['face']} {r['action']}")
            if res.get("image"):
                out["image"] = res["image"]
            out["ok"] = bool(res.get("ok"))
        return out

    def approve(self, rid: str, allow: bool, how: str = "spoken") -> dict:
        r = self.reqs.get(rid)
        if r is None:
            raise KeyError(f"no hands request #{rid}")
        if r["status"] != "asking":
            raise ValueError(f"#{rid} isn't waiting for a yes (it's {r['status']})")
        r.update(decision="allow" if allow else "deny", how=how, status="approved" if allow else "denied")
        self.audit("decision", id=rid, allow=allow, how=how)
        if self.card:
            self.card("Laptop hands", ("Allowed: " if allow else "Denied: ") + r.get("summary", ""), 4)
        self._wake("dec:" + rid)
        self._wake("req:" + rid)
        return self.public(r)

    def stop(self, why: str = "walterctl hands stop") -> int:
        self.state["stopped"] = True
        self.save()
        n = 0
        for r in self.reqs.values():
            if r["status"] in ("queued", "asking", "approved"):
                r.update(status="cancelled", error="hands were stopped")
                self._wake("dec:" + r["id"])
                self._wake("req:" + r["id"])
                n += 1
        self.queues.clear()
        for fid in list(self.caps):
            self._wake("face:" + fid)
        self.audit("stopped", reason=why, cancelled=n)
        if self.card:
            self.card("Laptop hands", "Stopped.", 5)
        return n

    # ---- the laptop answers
    def hello(self, face: dict, caps: dict):
        self.caps[face["id"]] = {"at": self.clock(), "enabled": bool(caps.get("enabled")),
                                 "stopped": bool(caps.get("stopped")),
                                 "tools": {str(k)[:20]: str(v)[:20] for k, v in dict(caps.get("tools") or {}).items()},
                                 "apps": [str(a)[:40] for a in list(caps.get("apps") or [])[:50]],
                                 "folders": int(caps.get("folders") or 0) if str(caps.get("folders", "0")).isdigit() else 0,
                                 "run_command": bool(caps.get("run_command")), "os": str(caps.get("os") or "")[:10],
                                 "local_prompt": bool(caps.get("local_prompt"))}

    def next_for(self, face_id: str) -> dict:
        if self.state["stopped"] or not self.state["enabled"]:
            return {"stop": bool(self.state["stopped"]), "enabled": self.state["enabled"]}
        q = self.queues.get(face_id)
        while q:
            rid = q.popleft()
            r = self.reqs.get(rid)
            if r and r["status"] == "queued":
                r["status"] = "sent"
                return {"request": {"id": rid, "action": r["action"], "params": r["params"],
                                    "owner_present": self.owner_present()}}
        return {}

    def asked(self, face: dict, d: dict) -> dict:
        r = self._mine(face, d.get("id"))
        risk = str(d.get("risk") or "")
        r.update(status="asking", risk=risk if risk in (H.MEDIUM, H.HIGH) else H.HIGH,
                 summary=H.redact(str(d.get("summary") or r["summary"]))[:200],
                 reason=H.redact(str(d.get("reason") or ""))[:200], asked=self.clock())
        self.audit("ask", id=r["id"], face=face["name"], risk=r["risk"], summary=r["summary"], reason=r["reason"])
        if self.card:
            self.card(f"{face['name']} wants to",
                      f"About to: {r['summary']}\nRisk: {r['risk']}. Say yes to allow, or no.", 120)
        self._wake("req:" + r["id"])
        return {"ok": True}

    def decision(self, face: dict, rid: str) -> dict:
        r = self._mine(face, rid)
        if r["status"] == "cancelled" or self.state["stopped"]:
            return {"decision": "deny", "how": "stopped"}
        return {"decision": r.get("decision"), "how": r.get("how")}

    def tapped(self, face: dict, d: dict) -> dict:
        r = self._mine(face, d.get("id"))
        if r["status"] == "asking":
            allow = d.get("allow") is True
            r.update(decision="allow" if allow else "deny", how="tapped", status="approved" if allow else "denied")
            self.audit("decision", id=r["id"], allow=allow, how="tapped")
            if self.card:
                self.card("Laptop hands", ("Allowed on the laptop: " if allow else "Denied on the laptop: ")
                          + r["summary"], 4)
            self._wake("req:" + r["id"])
        return {"ok": True}

    def result(self, face: dict, d: dict) -> dict:
        r = self._mine(face, d.get("id"))
        res = {"ok": bool(d.get("ok")), "text": str(d.get("text") or "")[:H.MAX_RESULT]}
        if d.get("error"):
            r["error"] = str(d["error"])[:300]
        if d.get("risk") in (H.LOW, H.MEDIUM, H.HIGH, H.REFUSE):
            r["risk"] = d["risk"]
        img = d.get("image_b64")
        if isinstance(img, str) and img:
            try:
                raw = base64.b64decode(img, validate=True)
                if H.png_size(raw):
                    p = self.shots / f"{r['id']}.png"
                    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "wb") as f:
                        f.write(raw)
                    res["image"] = str(p)
            except (ValueError, OSError):
                pass
        r["result"] = res
        r["status"] = "refused" if d.get("refused") else ("done" if res["ok"] else "failed")
        self.audit("result", id=r["id"], face=face["name"], action=r["action"], ok=res["ok"],
                   status=r["status"], error=r.get("error", ""))
        self._wake("req:" + r["id"])
        return {"ok": True}

    def _mine(self, face: dict, rid) -> dict:
        r = self.reqs.get(str(rid or ""))
        if r is None or r["face_id"] != face["id"]:
            raise web.HTTPNotFound(text="no such request")
        return r

    def status(self) -> dict:
        now = self.clock()
        return {"enabled": self.state["enabled"], "stopped": self.state["stopped"],
                "faces": [{"face": (self.store.get(fid) or {}).get("name", fid) if self.store else fid,
                           "online": now - c["at"] < 120, **{k: v for k, v in c.items() if k != "at"}}
                          for fid, c in self.caps.items()],
                "pending": [self.public(r) for r in self.reqs.values() if r["status"] in ("queued", "sent", "asking")]}


# ------------------------------------------------------------------ routes
def add_routes(app: web.Application, hands: HandsHub, facehub, kiosk):
    """facehub.auth(request) -> face (Bearer); kiosk(request) -> bool (this machine)."""
    routes = web.RouteTableDef()

    async def body(request, limit=8192) -> dict:
        if (request.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            raise web.HTTPUnsupportedMediaType(text="JSON only")
        raw = await request.content.read(limit + 1)
        if len(raw) > limit:
            raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(raw))
        try:
            d = json.loads(raw or b"{}")
        except ValueError:
            raise web.HTTPBadRequest(text="send a JSON object")
        if not isinstance(d, dict):
            raise web.HTTPBadRequest(text="send a JSON object")
        return d

    def wait_s(request, cap=30.0) -> float:
        try:
            return max(0.0, min(cap, float(request.query.get("wait", "0"))))
        except ValueError:
            return 0.0

    def local(request):
        if not kiosk(request):
            raise web.HTTPForbidden(text="local only")

    # ---- the laptop
    @routes.post("/api/face/hands/hello")
    async def h_hello(request):
        face = facehub.auth(request)
        hands.hello(face, await body(request))
        return web.json_response({"ok": True, "stop": hands.state["stopped"], "enabled": hands.state["enabled"]})

    @routes.get("/api/face/hands/next")
    async def h_next(request):
        face = facehub.auth(request)
        hands.caps.setdefault(face["id"], {"at": hands.clock()})["at"] = hands.clock()
        r = hands.next_for(face["id"])
        if not r:
            await hands._wait("face:" + face["id"], wait_s(request))
            r = hands.next_for(face["id"])
        return web.json_response(r)

    @routes.post("/api/face/hands/ask")
    async def h_ask(request):
        return web.json_response(hands.asked(facehub.auth(request), await body(request)))

    @routes.get("/api/face/hands/decision/{rid}")
    async def h_decision(request):
        face = facehub.auth(request)
        rid = request.match_info["rid"]
        d = hands.decision(face, rid)
        if not d.get("decision"):
            await hands._wait("dec:" + rid, wait_s(request))
            d = hands.decision(face, rid)
        return web.json_response(d)

    @routes.post("/api/face/hands/decision")
    async def h_tapped(request):
        return web.json_response(hands.tapped(facehub.auth(request), await body(request)))

    @routes.post("/api/face/hands/result")
    async def h_result(request):
        face = facehub.auth(request)
        return web.json_response(hands.result(face, await body(request, MAX_RESULT_BODY)))

    # ---- walterctl (this machine)
    @routes.get("/api/hands/status")
    async def k_status(request):
        local(request)
        hands.sweep()
        return web.json_response(hands.status())

    @routes.post("/api/hands/request")
    async def k_request(request):
        local(request)
        d = await body(request, 200_000)
        try:
            return web.json_response(hands.request(str(d.get("face") or ""), str(d.get("action") or ""),
                                                   d.get("params") or {}))
        except KeyError as e:
            return web.json_response({"error": str(e.args[0])}, status=404)
        except PermissionError as e:
            return web.json_response({"error": str(e)}, status=403)
        except LookupError as e:
            return web.json_response({"error": str(e)}, status=409)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)

    @routes.get("/api/hands/request/{rid}")
    async def k_get(request):
        local(request)
        rid = request.match_info["rid"]
        r = hands.reqs.get(rid)
        if r is None:
            return web.json_response({"error": f"no hands request #{rid}"}, status=404)
        if r["status"] in ("queued", "sent", "approved"):
            await hands._wait("req:" + rid, wait_s(request, 60))
        return web.json_response(hands.public(r))

    @routes.post("/api/hands/approve")
    async def k_approve(request):
        local(request)
        d = await body(request)
        try:
            return web.json_response(hands.approve(str(d.get("id") or "").lstrip("#"), d.get("allow") is True,
                                                   "spoken"))
        except KeyError as e:
            return web.json_response({"error": str(e.args[0])}, status=404)
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=409)

    @routes.post("/api/hands/{what:stop|start|enable|disable}")
    async def k_switch(request):
        local(request)
        what = request.match_info["what"]
        if what == "stop":
            return web.json_response({"ok": True, "cancelled": hands.stop()})
        if what == "start":
            hands.state["stopped"] = False
        else:
            hands.state["enabled"] = what == "enable"
        hands.save()
        hands.audit(what)
        for fid in list(hands.caps):
            hands._wake("face:" + fid)
        return web.json_response({"ok": True, **hands.state})

    app.add_routes(routes)

    async def on_stop(_app):
        hands.close()
    app.on_cleanup.append(on_stop)
