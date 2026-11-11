"""Companion app "Remote" tab: drive the wall browser from a paired phone.

Builds on the live browser daemon (home/skills/browse_live.py + browse_input.py) and
adds no port: everything goes through the app's own /api/app/remote/* routes (paired
device token required, see devices.py), and this module talks to the daemon on this
machine only:
  * the daemon's private unix socket (~/.walter/browser-live.sock) for open / go to /
    back / forward / reload / hide and for typing text ("pad"); requests carry
    "person": true so they never queue behind the person's own input;
  * the same socket's "input" command (browse_input.py batches) for raw mouse / wheel /
    key events, exactly what the wall's own mouse/keyboard sends;
  * the daemon's localhost HTTP port (token from face/browser.json) for the screencast
    (GET /stream.mjpg), read here only while an app has the Remote tab open (no app
    watching = no relay, so no screencast CPU).

While the person uses the remote (or the wall's own mouse/keyboard), Walter's own
browser commands wait in the daemon until ~10 s after the last input (browse_input).

PRIVACY: text typed on the phone (including passwords) is passed straight to the
daemon and dropped. It is never logged, printed, stored, spoken, put in the chat or
returned: replies only say how many characters went in and which box (by label).
Exceptions here are logged without request data.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

from aiohttp import web

from .config import DATA_DIR, REPO_DIR

log = logging.getLogger("walter.app.remote")

SOCK = Path(os.environ.get("WALTER_BROWSE_SOCK", DATA_DIR / "browser-live.sock"))
CTRL = Path(os.environ.get("WALTER_BROWSE_CTRL", REPO_DIR / "face" / "browser.json"))
WANT_S = 6.0               # keep the screencast relay up this long after the last frame request
FRAME_WAIT_S = 2.5         # long-poll: wait this long for a newer frame
HUMAN_IDLE_S = 10          # matches browse_input.HUMAN_IDLE_S
MAX_TEXT = 500             # matches the daemon's pad limit
KEYS = {"Enter", "Tab", "Shift+Tab", "Backspace", "Delete", "Escape", "ArrowUp", "ArrowDown",
        "ArrowLeft", "ArrowRight", "PageUp", "PageDown", "Home", "End", " "}
EVENTS = {"move", "down", "up", "wheel", "release"}
DEFAULT_URL = "https://www.google.com/"


def skills_dir(home: str | None = None) -> Path:
    """Walter's live skills (~/.walter/home/skills) if installed there, else the repo copy."""
    for d in ([Path(home) / "skills"] if home else []) + [DATA_DIR / "home" / "skills", REPO_DIR / "home" / "skills"]:
        if (d / "walter-browse").exists():
            return d
    return REPO_DIR / "home" / "skills"


def to_url(text: str) -> str:
    """What the URL bar means: an address (https:// added) or else a web search."""
    s = (text or "").strip()
    if not s:
        return ""
    if re.match(r"^https?://", s, re.I):
        return s
    if re.match(r"^[a-z][a-z0-9+.-]*:", s, re.I) and not re.match(r"^[\w.-]+:\d+(/|$)", s):
        raise ValueError("only web (http/https) addresses")
    if " " not in s and re.match(r"^[\w-]+(\.[\w-]+)+(:\d+)?(/.*)?$", s):
        # a bare IP (a printer, a router...) rarely has https; names get https
        return ("http://" if re.match(r"^\d{1,3}(\.\d{1,3}){3}(:|/|$)", s) else "https://") + s
    return "https://www.google.com/search?q=" + quote_plus(s)


def local_host(url: str) -> bool:
    """Addresses the remote won't open: this machine (the face/app server lives there)."""
    import ipaddress
    try:
        host = (urlsplit(url).hostname or "").strip("[]").lower()
    except ValueError:
        return True
    if not host or host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified or ip.is_link_local


def short_url(url: str) -> str:
    """host + path only (queries can carry tokens)."""
    try:
        u = urlsplit(url or "")
    except ValueError:
        return ""
    out = (u.hostname or "") + (u.path if u.path not in ("", "/") else "")
    return out[:120]


def clean_events(evs, w: int, h: int) -> list[dict]:
    """Mouse/wheel events from the app, checked and clamped (page CSS pixels)."""
    out = []
    if not isinstance(evs, list):
        return out

    def num(v, lo, hi):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return 0.0
        return max(lo, min(hi, v)) if v == v else 0.0

    for ev in evs[:60]:
        if not isinstance(ev, dict) or ev.get("t") not in EVENTS:
            continue
        t = ev["t"]
        if t == "release":
            out.append({"t": t})
            continue
        e = {"t": t, "x": round(num(ev.get("x"), 0, w - 1)), "y": round(num(ev.get("y"), 0, h - 1))}
        if t in ("down", "up"):
            e["b"] = 2 if ev.get("b") == 2 else 0
            e["n"] = int(num(ev.get("n") or 1, 1, 3))
        elif t == "wheel":
            e["dx"], e["dy"] = num(ev.get("dx"), -3000, 3000), num(ev.get("dy"), -3000, 3000)
        out.append(e)
    return out


def key_events(key: str) -> list[dict]:
    if key == "Shift+Tab":
        return [{"t": "kd", "k": "Shift"}, {"t": "kd", "k": "Tab"}, {"t": "ku", "k": "Tab"}, {"t": "ku", "k": "Shift"}]
    return [{"t": "kd", "k": key}, {"t": "ku", "k": key}]


class Remote:
    def __init__(self, home: str | None = None, sock: Path | None = None, ctrl: Path | None = None):
        self.sock = sock or SOCK
        self.ctrl = ctrl or CTRL
        self.script = skills_dir(home) / "walter-browse"
        self.frame: bytes | None = None
        self.seq = 0
        self.evt = asyncio.Event()
        self.wanted = 0.0
        self.relay: asyncio.Task | None = None
        self.last_input = 0.0
        self.last_url = ""        # the last page opened from the app (memory only)

    # ---- daemon state
    def panel(self) -> dict:
        try:
            d = json.loads(self.ctrl.read_text())
        except Exception:
            return {}
        return d if isinstance(d, dict) else {}

    def live_panel(self) -> dict:
        """The control file, only if the panel is open and its daemon is alive."""
        d = self.panel()
        if d.get("open") and time.time() - float(d.get("alive") or 0) < 20 and d.get("token") and d.get("port"):
            return d
        return {}

    async def cmd(self, req: dict, timeout: float = 60) -> dict:
        """One command over the daemon's unix socket. Errors come back as a plain message."""
        req = {**req, "person": True}
        try:
            r, w = await asyncio.wait_for(asyncio.open_unix_connection(str(self.sock), limit=1 << 20), 3)
        except Exception:
            return {"ok": False, "error": "The wall browser isn't running."}
        try:
            w.write((json.dumps(req) + "\n").encode())
            req = None
            await w.drain()
            line = await asyncio.wait_for(r.readline(), timeout)
            return json.loads(line.decode() or "{}")
        except asyncio.TimeoutError:
            return {"ok": False, "error": "The wall browser took too long."}
        except Exception:
            return {"ok": False, "error": "Lost touch with the wall browser."}
        finally:
            w.close()

    async def alive(self) -> bool:
        if not self.sock.exists():
            return False
        return bool((await self.cmd({"cmd": "ping"}, timeout=3)).get("ok"))

    async def http(self, d: dict, method: str, path: str, body: bytes = b"", timeout: float = 15):
        """(reader, writer, status) for a request to the daemon's localhost port."""
        port = int(d["port"])
        r, w = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 3)
        tok = quote_plus(str(d["token"]))
        head = (f"{method} {path}?t={tok} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n")
        w.write(head.encode() + body)
        body = None
        await w.drain()
        status = await asyncio.wait_for(r.readline(), timeout)
        parts = status.decode("latin-1").split(" ")
        code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        while True:                                   # skip the headers
            ln = await asyncio.wait_for(r.readline(), timeout)
            if ln in (b"\r\n", b"\n", b""):
                break
        return r, w, code

    async def send_input(self, evs: list[dict]) -> dict:
        """Raw events to the daemon (socket cmd "input" -> browse_input.handle). Keys are
        forwarded and dropped; the daemon marks the person as driving (Walter waits)."""
        if not self.live_panel():
            return {"ok": False, "error": "The browser isn't open on the wall."}
        self.last_input = time.time()
        req = {"cmd": "input", "args": {"ev": evs}}
        res = await self.cmd(req, timeout=15)
        evs.clear()
        req = None
        out = {"ok": bool(res.get("ok")), "edit": res.get("edit")}
        if not res.get("ok"):
            out["error"] = "The browser isn't open on the wall." if res.get("error") == "not open" \
                else (res.get("error") if str(res.get("error", "")).startswith("The ") else "That didn't work.")
        return out

    # ---- screencast relay (only while an app is watching)
    def want(self):
        self.wanted = time.time()
        if self.relay is None or self.relay.done():
            self.relay = asyncio.ensure_future(self._relay())

    async def _relay(self):
        while time.time() - self.wanted < WANT_S:
            d = self.live_panel()
            if not d:
                await asyncio.sleep(1)
                continue
            w = None
            try:
                r, w, code = await self.http(d, "GET", "/stream.mjpg")
                if code != 200:
                    raise ConnectionError("stream refused")
                while time.time() - self.wanted < WANT_S:
                    n = 0
                    while True:                       # part headers
                        ln = await asyncio.wait_for(r.readline(), 30)
                        if not ln:
                            raise ConnectionError("stream ended")
                        s = ln.decode("latin-1").strip()
                        if s.lower().startswith("content-length:"):
                            n = int(s.split(":", 1)[1])
                        elif not s and n:
                            break
                    jpg = await asyncio.wait_for(r.readexactly(n), 30)
                    await r.readline()                # trailing CRLF
                    self.frame, self.seq = jpg, self.seq + 1
                    self.evt.set()
                    self.evt = asyncio.Event()
            except asyncio.TimeoutError:
                pass                                   # a still page sends nothing: just reconnect
            except Exception:
                await asyncio.sleep(1)
            finally:
                if w is not None:
                    w.close()
        self.relay = None

    async def next_frame(self, after: int) -> tuple[int, bytes | None]:
        self.want()
        if self.frame is not None and self.seq != after:
            return self.seq, self.frame
        try:
            await asyncio.wait_for(self.evt.wait(), FRAME_WAIT_S)
        except asyncio.TimeoutError:
            pass
        if self.frame is not None and self.seq != after:
            return self.seq, self.frame
        return self.seq, None

    # ---- what the app asks for
    async def state(self, peek: bool = False) -> dict:
        d = self.panel()
        live = self.live_panel()
        out = {"open": bool(live), "title": (live.get("title") or "")[:140] if live else "",
               "url": (live.get("url") or "") if live else "", "w": int(d.get("w") or 1024),
               "h": int(d.get("h") or 810), "you": time.time() - self.last_input < HUMAN_IDLE_S}
        if live and peek:
            r = await self.cmd({"cmd": "pad", "args": {"action": "peek"}}, timeout=4)
            if r.get("ok"):
                out["field"] = r.get("field", "")
        return out

    async def nav(self, action: str, url_text: str = "") -> dict:
        if action in ("open", "goto"):
            try:
                url = to_url(url_text) or (self.last_url if action == "open" else "")
            except ValueError as e:
                return {"ok": False, "error": str(e).capitalize() + "."}
            if not url:
                url = DEFAULT_URL
            if local_host(url):
                return {"ok": False, "error": "The remote doesn't open addresses on Walter's own computer."}
            self.last_url = url
            if not await self.alive():
                ok = await self._start(url)
                return {"ok": ok, **({} if ok else {"error": "The wall browser didn't start."})}
            res = await self.cmd({"cmd": "show" if action == "open" or not self.live_panel() else "goto",
                                  "args": {"url": url}})
        elif action in ("back", "forward", "reload"):
            res = await self.cmd({"cmd": action})
        elif action == "close":
            if not await self.alive():
                return {"ok": True, "msg": "already closed"}
            res = await self.cmd({"cmd": "hide"})
        else:
            return {"ok": False, "error": "Unknown action."}
        self.last_input = time.time()
        return {"ok": bool(res.get("ok")), "title": (res.get("title") or "")[:140],
                "url": short_url(res.get("url", "")),
                **({} if res.get("ok") else {"error": res.get("error") or "That didn't work."})}

    async def _start(self, url: str) -> bool:
        """Start the daemon through the CLI (it knows how), showing the page."""
        try:
            proc = await asyncio.create_subprocess_exec(
                str(self.script), "show", url, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            rc = await asyncio.wait_for(proc.wait(), 80)
            return rc == 0
        except Exception:
            log.warning("remote: couldn't start the wall browser")
            return False

    async def type_text(self, text: str, kind: str, enter: bool) -> dict:
        """Type into the focused box on the wall. The text goes nowhere else."""
        if not self.live_panel():
            return {"ok": False, "error": "The browser isn't open on the wall."}
        self.last_input = time.time()
        req = {"cmd": "pad", "args": {"action": "type", "kind": kind, "text": text, "clear": False,
                                      "enter": bool(enter)}}
        text = None
        res = await self.cmd(req)
        req["args"]["text"] = None
        req = None
        self.last_input = time.time()
        if not res.get("ok") and "no box is selected" in str(res.get("error")):
            res["error"] = "No box is selected on the wall: tap the box in the picture first (or use Tab)."
        return {"ok": bool(res.get("ok")), "msg": res.get("msg") if res.get("ok") else "",
                "field": res.get("field", ""),
                **({} if res.get("ok") else {"error": res.get("error") or "That didn't work."})}


def add_routes(routes: web.RouteTableDef, apphub, json_body, home: str | None = None) -> Remote:
    rem = Remote(home)

    @routes.get("/api/app/remote/state")
    async def remote_state(request):
        apphub.auth(request)
        return web.json_response(await rem.state(peek=request.query.get("peek") == "1"),
                                 headers={"Cache-Control": "no-store"})

    @routes.get("/api/app/remote/frame")
    async def remote_frame(request):
        apphub.auth(request)
        try:
            after = int(request.query.get("after", "-1"))
        except ValueError:
            after = -1
        seq, jpg = await rem.next_frame(after)
        hdrs = {"Cache-Control": "no-store", "X-Frame-Seq": str(seq)}
        if jpg is None:
            return web.Response(status=204, headers=hdrs)
        return web.Response(body=jpg, content_type="image/jpeg", headers=hdrs)

    @routes.post("/api/app/remote/input")
    async def remote_input(request):
        apphub.auth(request)
        d = await json_body(request, 16384)
        st = rem.panel()
        evs = clean_events(d.get("ev"), int(st.get("w") or 1024), int(st.get("h") or 810))
        if not evs:
            return web.json_response({"ok": False, "error": "nothing to send"}, status=400)
        return web.json_response(await rem.send_input(evs))

    @routes.post("/api/app/remote/key")
    async def remote_key(request):
        apphub.auth(request)
        d = await json_body(request, 256)
        key = d.get("key")
        if key not in KEYS:
            return web.json_response({"ok": False, "error": "unknown key"}, status=400)
        return web.json_response(await rem.send_input(key_events(key)))

    @routes.post("/api/app/remote/type")
    async def remote_type(request):
        apphub.auth(request)
        d = await json_body(request, 4096)
        text, kind = d.pop("text", None), d.get("kind")
        if kind not in ("text", "password"):
            kind = "text"
        if not isinstance(text, str) or not text:
            return web.json_response({"ok": False, "error": "Nothing to type."}, status=400)
        if len(text) > MAX_TEXT:
            return web.json_response({"ok": False, "error": "That's too long (500 characters at a time)."},
                                     status=400)
        res = await rem.type_text(text, kind, bool(d.get("enter")))
        text = None
        return web.json_response(res, headers={"Cache-Control": "no-store"})

    @routes.post("/api/app/remote/nav")
    async def remote_nav(request):
        apphub.auth(request)
        d = await json_body(request, 4096)
        return web.json_response(await rem.nav(str(d.get("action") or ""), str(d.get("url") or "")))

    return rem
