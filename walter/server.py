"""Local web server: serves the face, talks to it over a websocket, and
exposes a small HTTP API used by `walterctl` (and therefore by Walter himself).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import weakref
from pathlib import Path

from aiohttp import WSMsgType, web

from . import dashboard as dash
from . import devices as devmod
from .config import REPO_DIR, Config
from .tags import TagStream

log = logging.getLogger("walter.server")
FACE_DIR = REPO_DIR / "face"


def _face_data(rel) -> bool:
    """Data the face polls (cards, canvas, usage, temp files, the owner's own
    pictures), not face code: changes to these never need a page reload."""
    return (rel.name.startswith("usage.json") or rel.suffix == ".tmp"
            or (rel.name.startswith(("display-", "card-")) and rel.suffix == ".json")   # per-face files
            or (rel.name.startswith("dashboard-") and rel.suffix == ".json")
            or rel.parts[:2] == ("backgrounds", "custom")
            or rel.parts[:1] == ("podcast-art",))


def face_snapshot(face_dir=None) -> dict[str, float]:
    """{relative path: mtime} of the face's code files."""
    face_dir = face_dir or FACE_DIR
    snap = {}
    for p in face_dir.rglob("*"):
        try:
            rel = p.relative_to(face_dir)
            if p.is_file() and not _face_data(rel):
                snap[rel.as_posix()] = p.stat().st_mtime
        except OSError:
            continue  # vanished mid-scan
    return snap


def face_version(snap: dict[str, float]) -> str:
    """Short hash of the face's code. The page remembers the version it was
    loaded with and only reloads when this differs, so a brain restart never
    blanks the screen."""
    h = hashlib.sha1()
    for k in sorted(snap):
        h.update(f"{k}\0{snap[k]:.3f}\n".encode())
    return h.hexdigest()[:12]


DASH_PATHS = ("/dashboard", "/dashboard/add", "/api/dashboard")
DASH_PAGE = Path(__file__).resolve().parent / "dashboard_page.html"
DASH_MAX_BODY = 4096


def lan_dashboard(cfg: Config) -> bool:
    """Listen on the LAN just for the phone page? (server.host is loopback and phone_page on)"""
    return bool(cfg.dashboard.phone_page) and dash.is_loopback(cfg.server.host)


def lan_app(cfg: Config) -> bool:
    """Listen on the LAN for the companion app too? (server.host is loopback, app on, allow_lan)"""
    app = getattr(cfg, "app", None)
    return bool(app and app.enabled and app.allow_lan) and dash.is_loopback(cfg.server.host)


def lan_faces(cfg: Config) -> bool:
    """Listen on the LAN for paired faces too? ([faces] enabled and, like the companion app's
    paired devices, [app] enabled + allow_lan; server.host is loopback)"""
    faces, app = getattr(cfg, "faces", None), getattr(cfg, "app", None)
    return (bool(faces and faces.enabled and app is not None and app.enabled and app.allow_lan)
            and dash.is_loopback(cfg.server.host))


def lan_open(cfg: Config) -> bool:
    return lan_dashboard(cfg) or lan_app(cfg) or lan_faces(cfg)


def bind_host(cfg: Config) -> str:
    return "0.0.0.0" if lan_open(cfg) else cfg.server.host


def lan_gate_ok(path: str, peer) -> bool:
    """With the server opened to the LAN for the phone page only: this machine may use
    everything; home-network devices only the dashboard page/API; nobody else anything."""
    if dash.is_loopback(peer):
        return True
    return path in DASH_PATHS and dash.is_private_ip(peer)


def clean_path(path: str) -> bool:
    """No "." / ".." segments, empty segments or backslashes (request.path is already
    percent-decoded, so "%2e%2e" and "..%2f" arrive here as "..")."""
    if "\\" in path or "\x00" in path or "//" in path:
        return False
    return not any(seg in (".", "..") for seg in path.split("/"))


def gate_ok(cfg: Config, path: str, peer, headers) -> bool:
    """Who may reach what. The kiosk (this machine, not via a proxy): everything.
    The companion app (/app, /api/app, /app/ws; token checked by each handler): from a
    reverse proxy on this machine, and from home-network / tailnet addresses if
    allow_lan. The dashboard phone page: home network only. A proxied request never
    reaches the rest of the API."""
    from .app_server import app_paths
    if devmod.is_kiosk(peer, headers):
        return True
    if not clean_path(path):
        # "/app/../walter.js" passes the /app prefix test but aiohttp's "/" static
        # folder would serve the face (and its data files) for it: refuse dot segments.
        return False
    via_proxy = devmod.proxied(peer, headers)
    from .cluster import cluster_paths
    if cluster_paths(path):
        # other full installs (cluster.py; every request is HMAC-signed, checked by each
        # handler): home network and tailnet only, never through the public tunnel
        cl, app = getattr(cfg, "cluster", None), getattr(cfg, "app", None)
        if not (cl and cl.enabled and app is not None and app.enabled and app.allow_lan) or via_proxy:
            return False
        return dash.is_private_ip(peer) or devmod.is_tailnet(peer)
    from .face_server import face_paths
    if face_paths(path):
        # faces (face_server.py; token/cookie checked by each handler): home network and
        # tailnet only, never through the public tunnel
        faces, app = getattr(cfg, "faces", None), getattr(cfg, "app", None)
        if not (faces and faces.enabled and app is not None and app.enabled and app.allow_lan) or via_proxy:
            return False
        return dash.is_private_ip(peer) or devmod.is_tailnet(peer)
    app = getattr(cfg, "app", None)
    if app_paths(path) and app is not None and app.enabled:
        if via_proxy:
            return True
        return bool(app.allow_lan) and (dash.is_private_ip(peer) or devmod.is_tailnet(peer))
    if via_proxy:
        return False
    if lan_open(cfg):
        return lan_dashboard(cfg) and lan_gate_ok(path, peer)
    return True   # server.host deliberately set to a LAN address: no gate (as before)


def workspace_switch(peer, headers, host: str, length, body, store=None) -> tuple[int, dict]:
    """POST /api/workspace/switch {"id": "acme"}: a tap on a workspace pill in the build cockpit.
    This machine's own face (the kiosk) or a home-network / tailnet address, never through a proxy or the
    public tunnel (the server's gate already refuses everything else); JSON from the same origin only;
    workspace ids only (walter/workspaces.switch_request). Switches the wall face, like
    `walterctl workspace switch` does."""
    from . import workspaces as WS
    if devmod.proxied(peer, headers) or not (devmod.is_kiosk(peer, headers) or dash.is_private_ip(peer)
                                              or devmod.is_tailnet(peer)):
        return 403, {"error": "local only"}
    code, why = dash_check_request(headers, host, length)
    if code:
        return code, {"error": why}
    status, out = WS.switch_request(body, WS.WALL, store)
    if status == 200:
        WS.refresh_build_view()
        log.info("workspace: tapped -> %s", out["id"])
    return status, out


class Hub:
    """All connected faces (normally just the kiosk browser)."""

    def __init__(self):
        self.clients: weakref.WeakSet[web.WebSocketResponse] = weakref.WeakSet()
        self.last: dict[str, dict] = {}  # last state/emotion, replayed to new clients
        self.face_snap: dict[str, float] = face_snapshot()
        self.face_version = face_version(self.face_snap)
        self.app = None   # app_server.AppHub: the companion apps get state/mute/workers too
        self.faces = None  # face_server.FaceHub: other faces' pages mirror state/mood/captions
        self.cluster = None  # cluster.ClusterNode: other full installs (docs/CLUSTER.md)
        self.wall = None  # wall_state.WallState: what the wall face says it is showing

    def send(self, msg: dict):
        if self.app is not None:
            try:
                self.app.from_face(msg)
            except Exception:
                log.exception("app forward failed")
        if self.faces is not None:
            try:
                self.faces.from_wall(msg)
            except Exception:
                log.exception("faces forward failed")
        if msg.get("t") in ("state", "config", "jobs", "setup", "firstrun", "net", "mute", "camera", "cloud"):
            self.last[msg["t"]] = msg
        data = json.dumps(msg)
        for ws in list(self.clients):
            if not ws.closed:
                asyncio.ensure_future(self._send(ws, data))

    @staticmethod
    async def _send(ws, data):
        try:
            await ws.send_str(data)
        except Exception:
            pass


def build_app(cfg: Config, hub: Hub, conductor, reminders, brain, tasks, setup, net,
              apphub=None, facehub=None, cluster=None) -> web.Application:
    app = web.Application()
    routes = web.RouteTableDef()
    faces_cfg = getattr(cfg, "faces", None)
    if faces_cfg is not None and faces_cfg.enabled:
        from . import face_server
        if facehub is None:
            facehub = face_server.FaceHub(cfg, conductor, hub)
        facehub.hub = hub
        hub.faces = facehub
        try:
            conductor.faces = facehub
        except AttributeError:
            pass
        facehub.reminders = reminders
        face_server.add_routes(app, cfg, hub, facehub, FACE_DIR)
        # laptop hands (hands_server.py, docs/HANDS.md): computer use on a paired laptop face
        from . import hands_server
        facehub.hands = hands_server.HandsHub(facehub.store, conductor, card=dash.show_card)
        hands_server.add_routes(app, facehub.hands, facehub,
                                lambda r: devmod.is_kiosk(r.remote, r.headers))
    cl_cfg = getattr(cfg, "cluster", None)
    if cl_cfg is not None and cl_cfg.enabled:
        from . import cluster as cluster_mod
        if cluster is None:
            cluster = cluster_mod.ClusterNode(cfg, tasks=tasks, on_finish=getattr(tasks, "on_finish", None))
        cluster.tasks = tasks
        cluster.facehub = facehub
        hub.cluster = cluster
        if facehub is not None:
            facehub.cluster = cluster
        try:
            reminders.may_fire = cluster.may_run_schedules   # routines fire on the lead only
        except AttributeError:
            pass
        cluster_mod.add_routes(app, cfg, cluster)
    app_cfg = getattr(cfg, "app", None)
    if app_cfg is not None and app_cfg.enabled:
        from . import app_server
        if apphub is None:
            apphub = app_server.AppHub(cfg, conductor)
        hub.app = apphub
        try:
            conductor.app = apphub
        except AttributeError:
            pass
        app_server.add_routes(app, cfg, hub, apphub, conductor, reminders, tasks)

    # what the wall is really showing, reported by the face (wall_state.py; face/wallstate.js)
    from . import wall_state
    hub.wall = wall_state.WallState()
    wall_state.add_routes(routes, hub.wall, apphub if app_cfg is not None and app_cfg.enabled else None)

    @routes.get("/")
    async def index(_):
        return web.FileResponse(FACE_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @routes.get("/ws")
    async def ws_handler(request):
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        hub.clients.add(ws)
        # first, so a page with stale face code reloads (and an up-to-date one doesn't)
        await ws.send_str(json.dumps({"t": "version", "v": hub.face_version}))
        await ws.send_str(json.dumps({"t": "config", "captions": cfg.life.captions,
                                       "live": cfg.ears.live_partials}))
        await ws.send_str(json.dumps({"t": "mute", "on": conductor.user_muted}))
        await ws.send_str(json.dumps({"t": "hwmute", "on": bool(getattr(conductor, "hw_muted", False))}))
        await ws.send_str(json.dumps({"t": "followup", "until": conductor.followup_until}))
        for msg in hub.last.values():
            await ws.send_str(json.dumps(msg))
        async for m in ws:
            if m.type != WSMsgType.TEXT:
                continue
            try:
                data = json.loads(m.data)
            except json.JSONDecodeError:
                continue
            if data.get("t") == "text" and data.get("text", "").strip() and not setup.needed:
                conductor.submit("text", data["text"].strip())
            elif data.get("t") == "mute":
                conductor.set_user_mute(data["on"] if isinstance(data.get("on"), bool) else None)
            elif data.get("t") == "sleep":
                conductor.go_to_sleep()
        return ws

    async def body(request) -> dict:
        try:
            return await request.json()
        except Exception:
            return {}

    @routes.post("/api/ask")
    async def api_ask(request):
        d = await body(request)
        if not d.get("text"):
            raise web.HTTPBadRequest(text="text required")
        conductor.submit(d.get("source", "ask"), d["text"])
        return web.json_response({"ok": True})

    @routes.post("/api/say")
    async def api_say(request):
        """Speak text directly (tags allowed), without going through the brain."""
        d = await body(request)
        ts = TagStream()
        events = ts.feed(d.get("text", "")) + ts.flush()
        conductor.wake_up()
        for kind, val in events:
            if kind == "say":
                conductor.voice.say(val)
            elif kind in ("emotion", "action"):
                conductor.voice.put(kind, val)
        return web.json_response({"ok": True})

    @routes.post("/api/face")
    async def api_face(request):
        d = await body(request)
        if d.get("emotion"):
            hub.send({"t": "emotion", "name": d["emotion"], "hold": d.get("hold", 30)})
        if d.get("action"):
            hub.send({"t": "action", "name": d["action"]})
        return web.json_response({"ok": True})

    @routes.post("/api/workspace/switch")
    async def api_workspace_switch(request):
        """A tap on a workspace pill (face/cockpit.js); see workspace_switch()."""
        d = await body(request)
        status, out = workspace_switch(request.remote, request.headers, request.host, request.content_length, d)
        return web.json_response(out, status=status)

    @routes.post("/api/stop")
    async def api_stop(_):
        conductor.stop()
        return web.json_response({"ok": True})

    @routes.post("/api/volume")
    async def api_volume(request):
        """{"step": "up"|"down"|"mute"|"unmute"} or {"set": 0..100}; runs bin/walter-volume."""
        try:
            data = await request.json()
        except Exception:
            data = {}
        args = (["set", str(int(data["set"]))] if "set" in data
                else [data.get("step", "get")] if data.get("step") in ("up", "down", "mute", "unmute", "get") else ["get"])
        proc = await asyncio.create_subprocess_exec(str(REPO_DIR / "bin" / "walter-volume"), *args,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        return web.json_response({"ok": proc.returncode == 0, "status": out.decode().strip()})

    @routes.post("/api/sleep")
    async def api_sleep(_):
        conductor.go_to_sleep()
        return web.json_response({"sleeping": True})

    @routes.post("/api/mute")
    async def api_mute(request):
        """{"on": true|false} to mute/unmute the microphone; omit "on" to toggle."""
        d = await body(request)
        on = d.get("on")
        muted = conductor.set_user_mute(on if isinstance(on, bool) else None)
        if not muted:
            conductor.welcome_back("unmuted")   # what they missed, as a card
        return web.json_response({"muted": muted})

    @routes.post("/api/remind")
    async def api_remind(request):
        d = await body(request)
        if not d.get("text") or ("due" not in d and not d.get("repeat")):
            raise web.HTTPBadRequest(text="text and due (epoch seconds) or repeat required")
        ws = d.get("workspace") or None
        if ws is not None:   # an optional project: exactly one of the workspace ids (whitelist)
            from . import workspaces as workspaces_mod
            if workspaces_mod.WorkspaceStore().valid_id(ws) is None:
                raise web.HTTPBadRequest(text=f"no workspace '{str(ws)[:40]}'")
        try:
            item = reminders.add(float(d["due"]) if "due" in d else None, d["text"],
                                 d.get("kind", "remind"), d.get("repeat"), workspace=ws)
        except (ValueError, KeyError) as e:
            raise web.HTTPBadRequest(text=str(e))
        return web.json_response(item)

    @routes.get("/api/reminders")
    async def api_reminders(_):
        return web.json_response(reminders.items)

    @routes.post("/api/reminders/{rid}")
    async def api_reminder_enable(request):
        """Pause / resume a routine: {"enabled": bool} (walterctl pause|resume; the app's Schedule screen)."""
        d = await body(request)
        if not isinstance(d.get("enabled"), bool):
            raise web.HTTPBadRequest(text="enabled (true/false) required")
        try:
            it = reminders.set_enabled(int(request.match_info["rid"]), d["enabled"])
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        if it is None:
            raise web.HTTPNotFound(text="no such reminder or routine")
        return web.json_response(it)

    @routes.delete("/api/reminders/{rid}")
    async def api_cancel(request):
        return web.json_response({"ok": reminders.cancel(int(request.match_info["rid"]))})

    @routes.post("/api/tasks")
    async def api_task(request):
        d = await body(request)
        if not d.get("prompt"):
            raise web.HTTPBadRequest(text="prompt required")
        title, ws = d.get("title") or d["prompt"][:60], d.get("workspace") or None
        cl = getattr(hub, "cluster", None)
        on = str(d.get("on") or "").strip()
        if on and cl is not None and cl.store.has_peers():
            # walterctl task --on NAME|best: another install in the cluster (docs/CLUSTER.md)
            try:
                return web.json_response(await cl.submit(title, d["prompt"], ws, on))
            except KeyError as e:
                raise web.HTTPNotFound(text=str(e.args[0]))
            except PermissionError as e:
                raise web.HTTPForbidden(text=str(e))
            except RuntimeError as e:
                raise web.HTTPConflict(text=str(e))
        try:
            item = await tasks.start(title, d["prompt"], workspace=ws)
        except RuntimeError as e:
            if cl is not None and cl.store.has_peers() and getattr(cfg.cluster, "jobs", True):
                try:   # every local slot is busy: another install with room takes it
                    r = await cl.submit(title, d["prompt"], ws, "best", include_self=False)
                    if r.get("node") != cl.me["id"]:
                        return web.json_response(r)
                except Exception as e2:
                    log.info("cluster: no other install could take %r (%s)", title, e2)
            raise web.HTTPConflict(text=str(e))
        return web.json_response(item)

    @routes.get("/api/tasks")
    async def api_tasks(_):
        return web.json_response(tasks.items)

    @routes.delete("/api/tasks/{tid}")
    async def api_task_cancel(request):
        return web.json_response({"ok": tasks.cancel(int(request.match_info["tid"]))})

    # ---- dashboard mode: the person's list (face type box + phone page on the home Wi-Fi)
    dash_store = dash.ListStore(title=cfg.dashboard.list_title)

    def dash_guard(request):
        if not dash.is_private_ip(request.remote):
            raise web.HTTPForbidden(text="home network only")

    def dash_list() -> dict:
        return {"title": dash_store.title, "sections": list(dash.SECTIONS), "items": dash_store.items()}

    @routes.get("/api/focus")
    async def api_focus(_):
        """Focus mode: the running box and the Now/Next lines (walterctl focus status)."""
        from . import focus
        fs = conductor._focus()
        nn = focus.now_next(fs, dash_store.items())
        return web.json_response({"status": fs.status(), "now_next": nn, "strip": list(focus.strip_text(nn)),
                                  "loop": True})

    @routes.get("/dashboard")
    async def dash_root(request):
        dash_guard(request)
        raise web.HTTPFound("/dashboard/add")

    @routes.get("/dashboard/add")
    async def dash_page(request):
        dash_guard(request)
        try:
            html = DASH_PAGE.read_text()
        except OSError:
            raise web.HTTPNotFound()
        return web.Response(text=html, content_type="text/html", headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                       "script-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:",
            "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer"})

    @routes.get("/api/dashboard")
    async def dash_get(request):
        dash_guard(request)
        return web.json_response(dash_list())

    @routes.post("/api/dashboard")
    async def dash_post(request):
        """{action: "add", text, section?, due?, quick?} or {action: "done"|"undone"|"remove", id}"""
        dash_guard(request)
        status, msg = dash_check_request(request.headers, request.host, request.content_length)
        if status:
            return web.json_response({"error": msg}, status=status)
        raw = await request.content.read(DASH_MAX_BODY + 1)
        if len(raw) > DASH_MAX_BODY:
            return web.json_response({"error": "too big"}, status=413)
        try:
            d = json.loads(raw)
            if not isinstance(d, dict):
                raise ValueError
        except ValueError:
            return web.json_response({"error": "send a JSON object"}, status=400)
        cmd = dash_command(conductor, d)
        if cmd:   # "wake up", "sleep", "mute"... typed: do it, don't put it on the list
            return web.json_response({"ok": True, "command": cmd, **dash_list()})
        try:
            item = dash_apply(dash_store, d)
        except (ValueError, KeyError, TypeError) as e:
            return web.json_response({"error": str(e.args[0] if e.args else e)}, status=400)
        return web.json_response({"ok": True, "item": item, **dash_list()})

    # ---- first-time setup (wall keyboard: open Claude's own sign-in window; QR image)
    @routes.get("/api/setup/qr.svg")
    async def api_setup_qr(request):
        from .setup import qr_svg
        if not setup.needed:
            raise web.HTTPNotFound()
        # Only ever the phone page: Walter no longer relays Claude's sign-in link or code.
        return web.Response(text=qr_svg(setup.phone_url()), content_type="image/svg+xml")

    @routes.post("/api/setup/{action}")
    async def api_setup(request):
        if not setup.needed:
            return web.json_response({"ok": False, "message": "Already set up."})
        return await setup.handle(request.match_info["action"], await body(request))

    # ---- Wi-Fi (the face's network panel; local only)
    @routes.get("/api/net/status")
    async def api_net_status(_):
        return web.json_response(net.status())

    @routes.get("/api/net/wifi")
    async def api_net_scan(_):
        return web.json_response(await net.scan())

    @routes.post("/api/net/connect")
    async def api_net_connect(request):
        d = await body(request)
        return web.json_response(await net.connect(d.get("ssid", ""), d.get("password", "")))

    @routes.post("/api/new-session")
    async def api_new_session(_):
        asyncio.create_task(brain.new_session())
        return web.json_response({"ok": True})

    @routes.post("/api/reload-face")
    async def api_reload(_):
        hub.send({"t": "reload", "force": True})
        return web.json_response({"ok": True})

    @routes.post("/api/captions")
    async def api_captions(request):
        d = await body(request)
        cfg.life.captions = bool(d.get("on", not cfg.life.captions))
        hub.send({"t": "config", "captions": cfg.life.captions})
        return web.json_response({"captions": cfg.life.captions})

    @routes.post("/api/camera")
    async def api_camera(request):
        """Turn the presence camera on/off until restart (config: [senses] camera)."""
        d = await body(request)
        p = conductor.presence
        if p is None:
            return web.json_response({"error": "presence not available"}, status=503)
        if "debug" in d:
            p.set_debug(bool(d["debug"]))
            return web.json_response({"camera": p.enabled, "debug": bool(cfg.senses.debug)})
        p.set_enabled(bool(d.get("on", not p.enabled)))
        return web.json_response({"camera": p.enabled, "debug": bool(cfg.senses.debug)})

    # ---- who is there (people.py): enrolment and the enrolled list
    @routes.post("/api/enroll")
    async def api_enroll(request):
        """{"kind": "face"|"voice", "name": "Alex"}: guided enrolment (takes 15-75 s)."""
        from .people import Screen
        d = await body(request)
        kind, name = d.get("kind"), (d.get("name") or "").strip()
        if kind not in ("face", "voice") or not name:
            raise web.HTTPBadRequest(text='kind ("face" or "voice") and name required')
        if conductor.people is None:
            return web.json_response({"ok": False, "error": "recognition isn't set up"}, status=503)
        try:
            if kind == "face":
                if conductor.presence is None:
                    return web.json_response({"ok": False, "error": "no camera"}, status=503)
                res = await conductor.presence.enroll_face(name, Screen())
            else:
                res = await conductor.enroll_voice(name, Screen())
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        return web.json_response(res)

    @routes.get("/api/people")
    async def api_people(_):
        p = conductor.people
        if p is None:
            return web.json_response({"people": [], "recognise_faces": False, "recognise_voices": False})
        return web.json_response({"people": p.list(), "recognise_faces": bool(cfg.senses.recognise_faces),
                                  "recognise_voices": bool(cfg.senses.recognise_voices),
                                  "voice_model_loaded": bool(conductor.ears and conductor.ears.voice_id),
                                  "store": str(p.store.root)})

    @routes.delete("/api/people/{name}")
    async def api_forget(request):
        p = conductor.people
        if p is None:
            return web.json_response({"removed": []})
        kind = request.query.get("kind") or None
        if kind not in (None, "face", "voice"):
            raise web.HTTPBadRequest(text="kind must be face or voice")
        try:
            removed = p.forget(request.match_info["name"], kind)
        except ValueError as e:
            raise web.HTTPBadRequest(text=str(e))
        return web.json_response({"removed": removed})

    def brain_status() -> dict:
        return {"model": brain.model or "default",
                "fast_model": brain.fast_model or "default",
                "deep_model": brain.deep_model or "default",
                "switches": brain.switches,
                "last_switch_s": brain.last_switch_s,
                **conductor.router.status()}

    @routes.get("/api/brain")
    async def api_brain(_):
        return web.json_response(brain_status())

    @routes.post("/api/brain")
    async def api_brain_mode(request):
        d = await body(request)
        try:
            conductor.router.set_mode(str(d.get("mode", "")))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(brain_status())

    # ------------------------------------------------ local / cloud speech
    DEMO_SENTENCE = "Hello there! This is how I sound with this voice. Rather nice, isn't it?"
    DEMO_LOCAL_SENTENCE = ("Right then, it's about forty degrees out this afternoon, "
                           "so grab a jacket. Honestly, I'd skip the long walk.")

    def speech_status() -> dict:
        sp = getattr(conductor, "speech", None)
        if sp is None:
            return {"tts": "local", "stt": "local", "available": False}
        hub.send(sp.indicator())   # e.g. a key was just dropped in
        rt = getattr(conductor, "realtime", None)
        out = {**sp.status(), "available": True}
        if rt is not None:
            out["realtime"] = {**out.get("realtime", {}), "session": rt.status()}
        return out

    @routes.get("/api/speech")
    async def api_speech(_):
        return web.json_response(speech_status())

    @routes.post("/api/speech")
    async def api_speech_set(request):
        """{which: "tts"|"voice"|"stt"|"ears", provider: "local"|"openai"}
        or {which: "mode", provider: "classic"|"realtime"}"""
        d = await body(request)
        sp = getattr(conductor, "speech", None)
        if sp is None:
            return web.json_response({"error": "speech switching unavailable"}, status=503)
        try:
            sp.set(str(d.get("which", "")), str(d.get("provider", "")))
        except ValueError as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response(speech_status())

    @routes.post("/api/speech/demo")
    async def api_speech_demo(_):
        """A/B: the same sentence with the local voice, then the OpenAI one."""
        sp = getattr(conductor, "speech", None)
        conductor.wake_up()
        conductor.voice.say(DEMO_SENTENCE, "local")
        cloud = bool(sp is not None and sp.has_key())
        if cloud:
            conductor.voice.say(DEMO_SENTENCE, "openai")
        return web.json_response({"ok": True, "local": True, "openai": cloud})

    @routes.post("/api/speech/demo-local")
    async def api_speech_demo_local(request):
        """Local voices side by side: piper, then each Kokoro voice (trial), all
        saying the same sentence. {voices: [...]} picks the Kokoro voices."""
        from .kokoro import DEMO_VOICES
        d = await body(request)
        voices = [str(v) for v in (d.get("voices") or DEMO_VOICES)][:6]
        conductor.wake_up()
        conductor.voice.say("Piper, my voice now. " + DEMO_LOCAL_SENTENCE, "piper")
        for v in voices:
            name = v.split("_", 1)[-1].capitalize()
            conductor.voice.say(f"Kokoro {name}. " + DEMO_LOCAL_SENTENCE, "kokoro:" + v)
        return web.json_response({"ok": True, "piper": True, "kokoro": voices})

    from .live import add_routes as live_routes
    live_routes(routes, conductor)   # /api/config: walterctl config get|set|reload

    @routes.get("/api/status")
    async def api_status(_):
        return web.json_response({
            "state": conductor.state,
            "busy": conductor.busy,
            "sleeping": conductor.sleeping,
            "faces_connected": len(hub.clients),
            "session_id": brain.session_id,
            "brain": brain_status(),
            "tts": conductor.voice.synth.kind,
            "speech": ({"tts": conductor.speech.tts, "stt": conductor.speech.stt,
                        "mode": conductor.speech.mode}
                       if getattr(conductor, "speech", None) else None),
            "ears": bool(conductor.ears and conductor.ears.model),
            "mic_muted": conductor.user_muted,
            "hw_muted": bool(getattr(conductor, "hw_muted", False)),
            "reminders": len(reminders.items),
            "tasks_running": tasks.running,
            "cancelled_stale": conductor.cancelled_stale() if hasattr(conductor, "cancelled_stale") else None,
            "self_echo_dropped": (conductor.self_echo_dropped()
                                  if hasattr(conductor, "self_echo_dropped") else None),
            "online": net.online,
            "presence": conductor.presence.status() if conductor.presence else {"camera": "disabled"},
            "time": time.time(),
        })

    app.add_routes(routes)
    app.router.add_static("/", FACE_DIR, show_index=False, append_version=False)

    @web.middleware
    async def no_cache(request, handler):
        resp = await handler(request)
        resp.headers["Cache-Control"] = "no-store"
        if request.path == "/app" or request.path.startswith("/app/"):
            from .app_server import csp_headers
            for k, v in csp_headers(request).items():
                resp.headers.setdefault(k, v)
        return resp

    app.middlewares.append(no_cache)

    @web.middleware
    async def gate(request, handler):
        if not gate_ok(cfg, request.path, request.remote, request.headers):
            raise web.HTTPForbidden(text="not from here")
        return await handler(request)
    app.middlewares.insert(0, gate)
    return app


def dash_check_request(headers, host: str, length) -> tuple[int, str]:
    """Cheap checks before reading a list change: JSON only (a plain web form on some
    other site can't send that without a CORS preflight we never answer), same origin,
    small. Returns (0, "") when fine, else (HTTP status, reason)."""
    if (headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
        return 415, "JSON only"
    origin = headers.get("Origin")
    if origin and origin != "null":
        from urllib.parse import urlsplit
        if urlsplit(origin).netloc != host:
            return 403, "wrong origin"
    elif origin == "null":
        return 403, "wrong origin"
    if length is not None and length > DASH_MAX_BODY:
        return 413, "too big"
    return 0, ""


def dash_command(conductor, d: dict) -> str | None:
    """Typed on the dashboard / phone page / app list: 'wake up', 'sleep', 'mute',
    'unmute' are commands, not to-dos (5 Nov audit). Returns the command done, or None."""
    if not isinstance(d, dict) or d.get("action") != "add" or conductor is None \
            or d.get("section") not in (None, "", "tasks"):
        return None
    cmd = dash.parse_command(d.get("text"))
    if cmd == "sleep":
        conductor.go_to_sleep()
    elif cmd in ("wake", "unmute"):
        conductor.set_user_mute(False)   # unmuting wakes me too
        conductor.wake_up()
        conductor.welcome_back("woken from the dashboard")
    elif cmd == "mute":
        conductor.set_user_mute(True)
    return cmd


def dash_apply(store, d: dict):
    action = d.get("action")
    if action == "add":
        text, section = d.get("text"), d.get("section")
        if not isinstance(text, str):
            raise ValueError("text required")
        if d.get("quick") or not section:
            qs, text = dash.parse_quick(text)
            section = section or qs
        due = d.get("due") or ""
        if not isinstance(due, str) or not isinstance(section, str):
            raise ValueError("bad section or due")
        return store.add(text, section, due)
    if action in ("done", "undone", "remove"):
        iid = d.get("id")
        if not isinstance(iid, int) or isinstance(iid, bool):
            raise ValueError("id (a number) required")
        if action == "remove":
            return store.remove({"id": iid})
        return store.done({"id": iid}, on=action == "done")
    raise ValueError("action must be add, done, undone or remove")


async def watch_face(hub: Hub):
    """Reload the kiosk page when Walter (or you) edits his face."""
    while True:
        await asyncio.sleep(2)
        try:
            now = await asyncio.get_running_loop().run_in_executor(None, face_snapshot)
        except OSError:
            continue
        if now != hub.face_snap:
            hub.face_snap = now
            hub.face_version = face_version(now)
            log.info("face changed; reloading")
            hub.send({"t": "reload", "v": hub.face_version})
