"""Companion app mode switcher: put the wall into a mode with one tap (the owner, 9 Nov 20:03).

GET  /api/app/mode   {mode, modes, capabilities, build: {on, phase, zoom}, focus: {...}|null,
                      sleeping, layout, chat_view}
POST /api/app/mode   {mode, phase?, zoom?, task?, minutes?, end_focus?}

Modes (same code paths as the voice / walterctl commands, so phone and voice never disagree):
  board         awake, portrait layout "large" (me small over the always-on info board)
                = walterctl display layout large (+ leaves sticky build / chat / an app-opened conversation)
  face          awake, portrait layout "compact" (just me, big)  = walterctl display layout compact
  build         sticky build cockpit = walterctl display build on [+ phase plan|build, zoom X|back]
  conversation  conversation view now, normal layout = display conversation view default + "conversation mode"
  chat          conversation view as chat bubbles = display conversation view chat + "show the chat"
                (only offered when face/chat.js exists: capabilities)
  focus         a time box = walterctl focus start MINUTES "task"
  sleep         walterctl sleep (dashboard, mic muted); any other mode wakes me first

A running focus box is never ended silently: switching away from it needs end_focus
done|park|stop (the app asks first), else 409 {code: "focus_running"}.

Everything is whitelisted; the only free text is the focus task (cleaned, 80 chars).
"""
from __future__ import annotations

import logging
import math
import re
import time

from aiohttp import web

from . import dashboard as dash
from . import display_settings as disp
from . import focus as focus_mod
from .config import REPO_DIR

log = logging.getLogger("walter.app.mode")

MODES = ("board", "face", "build", "conversation", "chat", "focus", "sleep")
ZOOMS = ("diff", "tests", "board", "stream", "graph", "plan", "projects", "back")   # bin/walterctl ZOOMS (+ back)
PHASES = ("plan", "build")
END_FOCUS = ("done", "park", "stop")
FOCUS_MIN, FOCUS_MAX = 1, 240
TASK_MAX = 80
CONVO_HOLD_S = 60   # the face closes a voice/app-opened conversation after a quiet minute (convgate.js)


# Typed build-mode phrases in the app (T47, the owner 9 Nov: he types "dev mode" in the app). The wall's
# face/buildgate.js only hears voice captions, so app turns flip display.json here. Same phrases as
# buildgate.js ON / OFF (OFF first, so "exit dev mode" never reads as "dev mode"); ON only for a short
# message so a long chat that merely mentions build mode doesn't switch the wall.
_MODEW = r"(?:build|dev|developer|development)"
BUILD_OFF = re.compile(
    r"\b((?:exit|leave|end|close|quit|stop)(?: the)? " + _MODEW + r" mode|(?:done|finished) with(?: the)? "
    + _MODEW + r" mode|" + _MODEW + r" mode off|(?:turn|switch) off(?: the)? " + _MODEW + r" mode|out of "
    + _MODEW + r" mode|(?:done|stop|finished) building)\b")
BUILD_ON = re.compile(
    r"\b(build mode|build view|dev mode|developer mode|development mode|let s build|lets build|let us build"
    r"|show me the build|show the build)\b")
BUILD_ON_MAX_WORDS = 8


def build_phrase(text: str) -> str | None:
    """"on" / "off" when a typed app message asks for build (dev) mode, else None."""
    t = " " + re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", re.sub(r"[\u2018\u2019'`]", " ",
                                                                 str(text or "").lower()))).strip() + " "
    if BUILD_OFF.search(t):
        return "off"
    if BUILD_ON.search(t) and len(t.split()) <= BUILD_ON_MAX_WORDS:
        return "on"
    return None


class ModeError(ValueError):
    def __init__(self, msg: str, status: int = 400, code: str = "bad_request"):
        super().__init__(msg)
        self.status, self.code = status, code


def capabilities(repo=None) -> list[str]:
    """Modes this wall can do. Chat needs the conversation chat view (face/chat.js)."""
    repo = repo or REPO_DIR
    return [m for m in MODES if m != "chat" or (repo / "face" / "chat.js").exists()]


def parse(d) -> dict:
    """Strict validation of a POST body. Returns the cleaned request or raises ModeError."""
    if not isinstance(d, dict):
        raise ModeError("send a JSON object")
    mode = d.get("mode")
    if not isinstance(mode, str) or mode not in MODES:
        raise ModeError("mode must be one of " + ", ".join(MODES))
    out = {"mode": mode}
    end = d.get("end_focus")
    if end is not None:
        if end not in END_FOCUS:
            raise ModeError("end_focus must be done, park or stop")
        out["end_focus"] = end
    if mode == "build":
        for key, allowed in (("phase", PHASES), ("zoom", ZOOMS)):
            v = d.get(key)
            if v is None:
                continue
            if not isinstance(v, str) or v not in allowed:
                raise ModeError(f"{key} must be one of " + ", ".join(allowed))
            out[key] = v
    elif mode == "focus":
        task = d.get("task")
        if not isinstance(task, str) or not task.strip():
            raise ModeError("what's the box for? (a short task name)")
        if len(task) > TASK_MAX:
            raise ModeError(f"task name: {TASK_MAX} characters at most")
        task = dash.clean_text(task, TASK_MAX)
        if not task:
            raise ModeError("what's the box for? (a short task name)")
        m = d.get("minutes")
        if (isinstance(m, bool) or not isinstance(m, (int, float)) or not math.isfinite(m)
                or m != int(m) or not FOCUS_MIN <= m <= FOCUS_MAX):
            raise ModeError(f"minutes must be a whole number {FOCUS_MIN} to {FOCUS_MAX}")
        out["task"], out["minutes"] = task, int(m)
    return out


class ModeSwitcher:
    def __init__(self, conductor, hub, focus_store=None, repo=None, now=time.time):
        self.conductor, self.hub = conductor, hub
        self.focus_store = focus_store or focus_mod.FocusStore
        self.repo = repo
        self.now = now
        self.convo_until = 0.0   # an app-opened conversation shows until about then (face holds it a minute)

    # ------------------------------------------------------------ state
    def _sleeping(self) -> bool:
        c = self.conductor
        return getattr(c, "sleeping", False) is True or getattr(c, "state", "") == "sleeping"

    def _focus(self) -> dict | None:
        try:
            st = self.focus_store().status()
        except Exception:  # noqa: BLE001
            log.exception("app mode: couldn't read the focus box")
            return None
        box = st.get("box")
        if not box:
            return None
        return {"task": box.get("task"), "end": box.get("end"), "status": box.get("status"),
                "left_min": round(st.get("left_min", 0), 1)}

    def state(self) -> dict:
        d = disp.read_display()
        build_on = str(d.get("build_mode", "auto")).lower() == "on"
        chat = str(d.get("chat_view", "default")).lower() == "chat"
        layout = str(d.get("layout") or "auto")
        focus = self._focus()
        sleeping = self._sleeping()
        caps = capabilities(self.repo)
        if sleeping:
            mode = "sleep"
        elif focus:
            mode = "focus"
        elif build_on:
            mode = "build"
        elif chat and "chat" in caps:
            mode = "chat"
        elif self.now() < self.convo_until:
            mode = "conversation"
        elif layout == "compact":
            mode = "face"
        else:
            mode = "board"
        return {"mode": mode, "modes": list(MODES), "capabilities": caps, "sleeping": sleeping,
                "layout": layout, "chat_view": "chat" if chat else "default", "focus": focus,
                "build": {"on": build_on, "phase": str(d.get("build_phase") or "build"),
                          "zoom": str(d.get("build_zoom") or "")},
                "zooms": list(ZOOMS), "phases": list(PHASES)}

    # ------------------------------------------------------------ changes (walterctl's code paths)
    def _build_write(self, **changes):
        d = disp.read_display()
        disp.write_display(build_seq=int(d.get("build_seq", 0) or 0) + 1, **changes)

    def _leave_build(self):
        """walterctl display build auto (only if it's stuck on: auto is what it normally is)."""
        if str(disp.read_display().get("build_mode", "auto")).lower() == "on":
            self._build_write(build_mode="auto", build_cmd="mode auto")

    def _convo(self, open_: bool):
        """Same as saying "conversation mode" / "close the chat" (face/converse.js 'convo' message)."""
        self.convo_until = self.now() + CONVO_HOLD_S if open_ else 0.0
        try:
            self.hub.send({"t": "convo", "open": bool(open_)})
        except Exception:  # noqa: BLE001
            log.debug("app mode: couldn't tell the face about the conversation view")

    def _wake(self):
        c = self.conductor
        if getattr(c, "user_muted", False):
            c.set_user_mute(False)
        c.wake_up()
        if hasattr(c, "welcome_back"):
            c.welcome_back("woken from the app")

    def apply(self, req: dict) -> dict:
        mode = req["mode"]
        focus = self._focus()
        if focus and mode != "sleep" and (mode != "focus" or req.get("end_focus")):
            end = req.get("end_focus")
            if not end and mode != "focus":
                raise ModeError(f"a focus box is running: {focus['task']}", 409, "focus_running")
            if end:
                self._end_focus(end)
        if mode == "sleep":
            self._convo(False)
            if not self._sleeping():
                self.conductor.go_to_sleep()
            return self.state()
        if self._sleeping():
            self._wake()
        if mode in ("board", "face"):
            self._leave_build()
            if disp.read_display().get("chat_view") == "chat":
                disp.write_display(chat_view="default")
            disp.write_display(layout="large" if mode == "board" else "compact")
            self._convo(False)
        elif mode == "build":
            if str(disp.read_display().get("build_mode", "auto")).lower() != "on":
                self._build_write(build_mode="on", build_cmd="mode on")
            if req.get("phase"):
                self._build_write(build_phase=req["phase"], build_cmd=f"phase {req['phase']}")
            z = req.get("zoom")
            if z == "back":
                self._build_write(build_zoom="", build_cmd="zoom back")
            elif z:
                self._build_write(build_zoom=z, build_cmd=f"zoom {z}")
        elif mode in ("conversation", "chat"):
            if mode == "chat" and "chat" not in capabilities(self.repo):
                raise ModeError("the chat view isn't on this wall yet", 409, "unsupported")
            self._leave_build()
            disp.write_display(chat_view="chat" if mode == "chat" else "default")
            self._convo(True)
        elif mode == "focus":
            box = self.focus_store().start(req["task"], req["minutes"])
            self._convo(False)
            try:
                dash.show_card("Focus", f"{box['task']}\n{focus_mod.mins_text(req['minutes'])},"
                               f" until {focus_mod.hm(box['end'])}", 5)
            except Exception:  # noqa: BLE001
                pass
        return self.state()

    def typed(self, text: str) -> str | None:
        """An app chat message like "dev mode" / "exit dev mode": switch the wall's build cockpit.
        Returns "on" / "off" when it did something. Never raises (the turn still goes to the brain)."""
        want = build_phrase(text)
        try:
            if want == "on":
                self.apply({"mode": "build"})
            elif want == "off":
                self._leave_build()
        except ModeError as e:   # e.g. a focus box is running: leave it to the brain to ask
            log.info("app mode: typed %s not applied: %s", want, e)
            return None
        except Exception:  # noqa: BLE001
            log.exception("app mode: typed build phrase failed")
            return None
        return want

    def _end_focus(self, end: str):
        """walterctl focus done|park|stop."""
        outcome = {"done": "done", "park": "parked", "stop": "stopped"}[end]
        e = self.focus_store().finish(outcome)
        try:
            store = dash.ListStore()
            if outcome == "done":
                focus_mod.tick_off(store, e["task"])
            elif outcome == "parked":
                focus_mod.park_on_list(store, e["task"])
        except Exception:  # noqa: BLE001
            log.exception("app mode: couldn't update the list after ending the focus box")


def add_routes(routes, apphub, json_body, conductor, hub) -> ModeSwitcher:
    sw = ModeSwitcher(conductor, hub)

    @routes.get("/api/app/mode")
    async def app_mode(request):
        apphub.auth(request)
        return web.json_response(sw.state())

    @routes.post("/api/app/mode")
    async def app_mode_set(request):
        dev = apphub.auth(request)
        d = await json_body(request, 1024)
        try:
            req = parse(d)
            out = sw.apply(req)
        except ModeError as e:
            return web.json_response({"error": str(e), "code": e.code, **sw.state()}, status=e.status)
        except ValueError as e:   # focus store refusals
            return web.json_response({"error": str(e), "code": "bad_request", **sw.state()}, status=400)
        except OSError as e:
            log.warning("app mode: couldn't save: %s", e)
            return web.json_response({"error": "couldn't save it"}, status=500)
        log.info("app: %s switched the wall to %s", dev.get("name"), req["mode"])
        return web.json_response({"ok": True, **out})

    return sw
