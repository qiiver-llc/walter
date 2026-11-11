"""Presentation mode: a ~60-second scripted self-demo that takes over the wall (the owner, 10 Nov).

    walterctl present start | stop | status | rehearse
    GET  /api/app/present            status
    POST /api/app/present {"action": "start"|"rehearse"|"stop"}

The script is ~/.walter/home/presentation.md (written with DEFAULT_SCRIPT the first time; the owner
edits the words). Each "## step" has:
    say: text with [emotion] / [action:x] tags          (spoken by my normal voice, captions on)
    screen: [+SECONDS] COMMAND                           (at that offset into the step; default +0)
    hold: SECONDS                                        (stay on this step at least this long)
    timeout: SECONDS                                     (give up on this step after this long)
Screen commands (whitelisted): theme NAME|back, build on|off, zoom PANEL|back, layout auto|compact|large|back,
board SECONDS|off (the dashboard preview, demo data).

During the show (privacy: a public venue):
  - display.json "demo": true, so the face shows built-in sample data only (face/demo.js) and hides the usage
    meter, the Wi-Fi chip, the canvas, the browser and cards;
  - the ears are off (conductor.presenting: no listening at all) but the voice is not hushed, even if the
    mic was muted; the conductor holds everything else (worker reports, reminders, routines) until it ends;
  - it runs as a task in the server's event loop, so Stop (app, walterctl, a restart) always works.
At the end, on Stop, or after a crash/restart (state file ~/.walter/present-state.json): theme, layout, build
view, chat view and the demo flag go back to what they were; the mic follows the person's own mute setting
(never changed by the show), so it unmutes only if it was unmuted before.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from . import display_settings as disp

log = logging.getLogger("walter.present")

TOTAL_CAP_S = 240.0
STEP_TIMEOUT_S = 25.0
STEP_TIMEOUT_MAX = 40.0
HOLD_MAX = 30.0
TICK_S = 0.1
MAX_STEPS = 16
SAY_MAX = 600
BUILD_ZOOMS = ("diff", "tests", "board", "stream", "graph", "plan", "projects")
LAYOUTS = ("auto", "compact", "large")
# display.json keys the show may change; all of them are put back afterwards
KEYS = ("theme", "layout", "build_mode", "build_zoom", "build_phase", "chat_view", "demo", "demo_board_until", "demo_browser_until", "demo_canvas_until")

DEFAULT_SCRIPT = """\
# Walter's presentation (walterctl present start | stop | rehearse; or the app: Settings > Presentation)
#
# Each "## step" has: say: (what I say, with [emotion] and [action:x] tags), screen: [+seconds] command,
# hold: (minimum seconds on the step), timeout: (give up after). Screen commands: theme NAME|back,
# build on|off, zoom diff|tests|board|stream|graph|plan|back, layout auto|compact|large|back,
# board SECONDS|off (the info board, demo data only). The whole show stops after 90 seconds.
# Keep it to about 150 words so it fits in a minute.

## hello
screen: build off
screen: layout compact
say: [happy] [action:grow] Hi everyone, I'm Walter! I'm a personal AI who lives on the wall. My person built me, and these days I help build myself.
hold: 4

## expressions
say: [excited] [action:bounce] I don't have a mouth, so I talk with my eyes. [curious] [action:peek] I get curious, [mischievous] [wink] a little mischievous, [proud] proud of a good build, [laugh] and yes, I laugh at my own jokes.
hold: 6

## themes
screen: theme cream
screen: +3 theme sunset
screen: +6 theme forest
screen: +9 theme back
say: [happy] I can change outfits, too. Cream for cosy mornings, sunset for the evening, forest when I'm feeling outdoorsy. [wink] Then back to my usual look.
hold: 10

## cockpit
screen: build on
say: [proud] [action:scoot:left] This is my developer cockpit. While we chat, I hand bigger jobs to background workers, and they write and test my own code.
hold: 10

## board
screen: build off
screen: board 14
screen: layout large
say: [happy] Day to day, I run the family board: the calendar, the shopping list, reminders, and a nudge before soccer practice. [wink] Don't worry, this is a pretend family.
hold: 9

## faces
screen: board off
screen: layout back
say: [curious] [action:scoot:right] I can have more faces, like a kitchen tablet or a laptop, all sharing one brain. And there's a phone app, so I can tag along anywhere.
hold: 4

## goodbye
say: [excited] [action:bounce] Thanks for having me! [wink] If you've got questions, my person's right here.
hold: 3
"""


# ---------------------------------------------------------------- the script
class ScriptError(ValueError):
    pass


DEMO_FOCUS_TASK = "Finish the pitch deck"
BEAT_GAP_S = 0.8          # default silence for a bare [pause]
PAUSE_RE = re.compile(r"\[pause(?::(\d+(?:\.\d+)?))?\]", re.I)


def _portrait_transform() -> str:
    """Which way portrait goes on this wall (shared with the app's Rotate button: screen_rotate.py)."""
    from .screen_rotate import portrait_transform
    return portrait_transform()


def split_beats(text: str) -> list[tuple[str, float]]:
    """"Hi. [pause] Bye. [pause:1.5] Ok." -> [("Hi.", 0.8), ("Bye.", 1.5), ("Ok.", 0.0)]: each beat with the
    silence to leave after it (capped at 4 s)."""
    out, pos = [], 0
    for m in PAUSE_RE.finditer(text or ""):
        chunk = text[pos:m.start()].strip()
        gap = min(float(m.group(1)) if m.group(1) else BEAT_GAP_S, 4.0)
        if chunk:
            out.append((chunk, gap))
        elif out:
            out[-1] = (out[-1][0], out[-1][1] + gap)
        pos = m.end()
    tail = (text or "")[pos:].strip()
    if tail:
        out.append((tail, 0.0))
    return out
SCREEN_RE = re.compile(r"^(?:\+(\d+(?:\.\d+)?)\s+)?([a-z]+)(?:\s+([a-z0-9:.-]+))?$")


def parse_screen(cmd: str) -> tuple[float, str, str]:
    """"+3 theme sunset" -> (3.0, "theme", "sunset"). Only whitelisted commands; else ScriptError."""
    m = SCREEN_RE.match(" ".join(str(cmd or "").lower().split()))
    if not m:
        raise ScriptError(f"screen: can't read {cmd!r}")
    at, verb, arg = float(m[1] or 0), m[2], m[3] or ""
    if at > TOTAL_CAP_S:
        raise ScriptError(f"screen: +{at:g} is longer than the whole show")
    ok = ((verb == "theme" and (arg == "back" or arg in disp.THEMES))
          or (verb == "build" and arg in ("on", "off"))
          or (verb == "zoom" and (arg == "back" or arg in BUILD_ZOOMS))
          or (verb == "layout" and (arg == "back" or arg in LAYOUTS))
          or (verb == "board" and (arg == "off" or (re.fullmatch(r"\d+", arg) and 1 <= int(arg) <= 60)))
          or (verb == "focus" and (arg == "off" or (re.fullmatch(r"\d+", arg) and 1 <= int(arg) <= 60)))
          or (verb == "orient" and arg in ("landscape", "portrait", "flip", "back"))
          or (verb == "browser" and arg in ("site", "down", "up", "off"))
          or (verb == "slide" and arg in ("family", "off")))
    if not ok:
        raise ScriptError(f"screen: unknown command {cmd!r}")
    return at, verb, arg


def _num(v: str, lo: float, hi: float, what: str) -> float:
    try:
        f = float(v)
    except ValueError:
        raise ScriptError(f"{what}: not a number ({v!r})") from None
    if not lo <= f <= hi:
        raise ScriptError(f"{what}: {f:g} is outside {lo:g}..{hi:g}")
    return f


def parse_script(text: str) -> list[dict]:
    """presentation.md -> [{name, say, screen: [(at, verb, arg)], hold, timeout}]. Lines starting with "#"
    (but not "##") are comments. Raises ScriptError for anything it can't use."""
    steps: list[dict] = []
    cur = None
    for n, raw in enumerate(str(text or "").splitlines(), 1):
        line = raw.strip()
        if not line or (line.startswith("#") and not line.startswith("##")):
            continue
        if line.startswith("##"):
            name = line.lstrip("#").strip()[:40] or f"step {len(steps) + 1}"
            cur = {"name": name, "say": "", "screen": [], "hold": 0.0, "timeout": STEP_TIMEOUT_S}
            steps.append(cur)
            continue
        key, sep, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if not sep or key not in ("say", "screen", "hold", "timeout"):
            raise ScriptError(f"line {n}: expected say:, screen:, hold: or timeout: ({line[:40]!r})")
        if cur is None:
            raise ScriptError(f"line {n}: put a '## step name' line first")
        try:
            if key == "say":
                cur["say"] = (cur["say"] + " " + val).strip()[:SAY_MAX]
            elif key == "screen":
                cur["screen"].append(parse_screen(val))
            elif key == "hold":
                cur["hold"] = _num(val, 0, HOLD_MAX, "hold")
            else:
                cur["timeout"] = _num(val, 1, STEP_TIMEOUT_MAX, "timeout")
        except ScriptError as e:
            raise ScriptError(f"line {n}: {e}") from None
    if not steps:
        raise ScriptError("no steps (each starts with a '## name' line)")
    if len(steps) > MAX_STEPS:
        raise ScriptError(f"{len(steps)} steps: {MAX_STEPS} at most")
    for s in steps:
        s["screen"].sort(key=lambda c: c[0])
    return steps


def script_path(home: str | Path | None) -> Path:
    return Path(home or Path.home() / ".walter" / "home") / "presentation.md"


def load_script(home: str | Path | None) -> list[dict]:
    """The owner's script (written from DEFAULT_SCRIPT the first time). A broken file falls back to the
    default rather than leaving the room in silence."""
    p = script_path(home)
    try:
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(DEFAULT_SCRIPT)
        return parse_script(p.read_text())
    except (OSError, ScriptError) as e:
        log.warning("presentation: %s unusable (%s); using the built-in script", p, e)
        return parse_script(DEFAULT_SCRIPT)


# ---------------------------------------------------------------- crash-safe state
def state_path() -> Path:
    return Path(os.environ.get("WALTER_DATA", Path.home() / ".walter")) / "present-state.json"


def snapshot_display(path=None) -> dict:
    d = disp.read_display(path)
    return {k: d.get(k) for k in KEYS}


def restore_display(snap: dict, path=None) -> dict:
    """Put the display keys back (a key that wasn't there is removed). A build_mode change bumps build_seq
    so the face applies it like `walterctl display build ...`."""
    p = str(path or disp.display_path())
    d = disp.read_display(p)
    build_changed = (d.get("build_mode") != snap.get("build_mode") or d.get("build_zoom") != snap.get("build_zoom"))
    for k in KEYS:
        v = snap.get(k)
        if v is None:
            d.pop(k, None)
        else:
            d[k] = v
    if build_changed:
        d["build_seq"] = int(d.get("build_seq", 0) or 0) + 1
        d["build_cmd"] = f"mode {snap.get('build_mode') or 'auto'}"
    d["set"] = int(time.time())
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.utime(tmp, (disp._OLD_MTIME, disp._OLD_MTIME))
    os.replace(tmp, p)
    return d


def save_state(st: dict):
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    os.replace(tmp, p)


def read_state() -> dict | None:
    try:
        d = json.loads(state_path().read_text())
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def clear_state():
    try:
        state_path().unlink()
    except FileNotFoundError:
        pass


def recover(path=None) -> bool:
    """A show was interrupted (crash, restart, power): put the wall back. Also clears a stray demo flag.
    Safe to call any time no show is running (Walter's startup, `walterctl present stop` offline)."""
    st = read_state()
    did = False
    if st and isinstance(st.get("display"), dict):
        try:
            restore_display(st["display"], path)
            did = True
            log.info("presentation: restored the wall after an interrupted show")
        except OSError as e:
            log.warning("presentation: couldn't restore the display: %s", e)
            return False
    clear_state()
    try:
        d = disp.read_display(path)
        if d.get("demo") or "demo_board_until" in d:
            restore_display({**{k: d.get(k) for k in KEYS}, "demo": None, "demo_board_until": None, "demo_browser_until": None, "demo_canvas_until": None}, path)
            did = True
    except OSError:
        pass
    return did


# ---------------------------------------------------------------- the wall (real side effects)
class WallActions:
    """What the runner does to the real wall. Tests use a fake with the same methods."""

    def __init__(self, conductor, display_path=None):
        self.c = conductor
        self.path = display_path

    # speech
    def say(self, text: str):
        from .tags import TagStream
        ts = TagStream()
        voice = self.c.voice
        for kind, val in ts.feed(text) + ts.flush():
            if kind == "say":
                voice.say(val)
            elif kind in ("emotion", "action"):
                voice.put(kind, val)

    def speaking(self) -> bool:
        v = getattr(self.c, "voice", None)
        return bool(v is not None and (getattr(v, "busy", 0) > 0)) or getattr(self.c, "state", "") == "speaking"

    def hush(self):
        v = getattr(self.c, "voice", None)
        if v is not None:
            v.stop()

    # the show's setup / teardown
    def begin(self) -> dict:
        """Ears off, voice on, wake up, demo data on. Returns what to restore."""
        c = self.c
        was = {"sleeping": bool(getattr(c, "sleeping", False) or getattr(c, "state", "") == "sleeping")}
        c.presenting = True
        c.apply_user_mute()          # ears off, voice not hushed (conductor.apply_user_mute)
        if was["sleeping"]:
            c.wake_up()
        self.write(demo=True, chat_view="default")
        return was

    def end(self, was: dict, snap: dict):
        c = self.c
        self._orient_restore()
        try:
            self._browser_off()
            self._slide_off()
        except Exception:
            log.exception("present: couldn't close the browser panel / slide")
        if getattr(self, "_focus_on", False):
            try:
                c._focus().finish("stopped")
            except Exception:
                log.exception("present: couldn't stop the demo focus box")
            self._focus_on = False
        try:
            restore_display(snap, self.path)
        finally:
            c.presenting = False
            c.apply_user_mute()      # back to the person's own mute setting
            if was.get("sleeping"):
                c.go_to_sleep()

    def write(self, **changes):
        disp.write_display(self.path, **changes)

    # ---- the sample "family board" slide (canvas panel; sample data only)
    def _slide(self, arg: str):
        import subprocess
        exe = os.path.expanduser("~/walter/bin/walter-canvas")
        if arg == "family":
            f = os.path.expanduser("~/.walter/home/presentation/family-board.html")
            self.write(demo_canvas_until=int(time.time()) + 90)
            self._slide_on = True
            subprocess.run([exe, "show", f, "--title", "Family board", "--seconds", "40"], capture_output=True, timeout=20)
        else:
            self._slide_off()

    def _slide_off(self):
        import subprocess
        if getattr(self, "_slide_on", False):
            subprocess.run([os.path.expanduser("~/walter/bin/walter-canvas"), "hide"], capture_output=True, timeout=20)
            self._slide_on = False
        self.write(demo_canvas_until=0)

    # ---- the live browser panel on the product site (demo flag lets #browser through for this step only)
    BROWSER_URL = "https://wallter.app/"

    def _walter_browse(self, *args):
        import subprocess
        exe = os.path.expanduser("~/.walter/home/skills/walter-browse")
        try:
            subprocess.run([exe, *args], capture_output=True, timeout=40)
        except Exception:
            log.exception("present: walter-browse %s failed", args)

    def _browser(self, arg: str):
        if arg == "site":
            self.write(demo_browser_until=int(time.time()) + 60)
            self._browser_on = True
            self._walter_browse("show", self.BROWSER_URL)
        elif arg in ("down", "up"):
            self._walter_browse("scroll", arg)
        else:
            self._browser_off()

    def _browser_off(self):
        if getattr(self, "_browser_on", False):
            self._walter_browse("hide")
            self._browser_on = False
        self.write(demo_browser_until=0)

    # ---- real screen rotation (wlr-randr inside the kiosk's Wayland session)
    def _randr(self, *args) -> str:
        import subprocess
        env = dict(os.environ, WAYLAND_DISPLAY=os.environ.get("WALTER_WAYLAND", "wayland-0"),
                   XDG_RUNTIME_DIR=os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
        r = subprocess.run(["wlr-randr", *args], capture_output=True, text=True, timeout=10, env=env)
        return r.stdout

    def _outputs(self) -> dict:
        """{output name: transform} for the enabled outputs."""
        outs, name, enabled = {}, None, False
        for line in self._randr().splitlines():
            if line and not line[0].isspace():
                name, enabled = line.split()[0], False
            elif "Enabled:" in line:
                enabled = "yes" in line
            elif "Transform:" in line and name and enabled:
                outs[name] = line.split(":", 1)[1].strip()
        return outs

    def _orient(self, arg: str):
        outs = self._outputs()
        if not outs:
            log.info("present: no screen to turn (wlr-randr found none)")
            return
        if getattr(self, "_orient_orig", None) is None:
            self._orient_orig = dict(outs)
        for name in outs:
            if arg == "back":
                t = self._orient_orig.get(name, "normal")
            elif arg == "landscape":
                t = "normal"
            elif arg == "flip":   # to the other orientation from where the show started
                t = "normal" if self._orient_orig.get(name, "normal") != "normal" else _portrait_transform()
            else:   # portrait: the way the wall is mounted (WALTER_ROTATE in kiosk.env), else 270
                t = _portrait_transform()
            self._randr("--output", name, "--transform", t)

    def _orient_restore(self):
        if getattr(self, "_orient_orig", None):
            try:
                self._orient("back")
            except Exception:
                log.exception("present: couldn't turn the screen back")
            self._orient_orig = None

    def screen(self, verb: str, arg: str, snap: dict):
        d = disp.read_display(self.path)
        seq = int(d.get("build_seq", 0) or 0) + 1
        if verb == "theme":
            self.write(theme=(snap.get("theme") or disp.THEME_DEFAULT) if arg == "back" else arg)
        elif verb == "build":
            mode = "on" if arg == "on" else "off"
            self.write(build_mode=mode, build_zoom="", build_seq=seq, build_cmd=f"mode {mode}")
        elif verb == "zoom":
            z = "" if arg == "back" else arg
            self.write(build_zoom=z, build_seq=seq, build_cmd=f"zoom {arg}")
        elif verb == "layout":
            self.write(layout=(snap.get("layout") or "auto") if arg == "back" else arg)
        elif verb == "board":
            self.write(demo_board_until=0 if arg == "off" else int(time.time()) + int(arg))
        elif verb == "slide":    # a fixed sample slide in the canvas panel (landscape-friendly family board)
            self._slide(arg)
        elif verb == "browser":  # the live browser panel, public site only (the owner's own product site)
            self._browser(arg)
        elif verb == "orient":  # really turn the screen (wlr-randr, live; the face re-lays itself out)
            self._orient(arg)
        elif verb == "focus":   # a demo focus box (never the owner's real task); stopped again at the end
            try:
                fs = self.c._focus()
                if arg == "off":
                    if getattr(self, "_focus_on", False):
                        fs.finish("stopped")
                        self._focus_on = False
                elif not fs.box():
                    fs.start(DEMO_FOCUS_TASK, float(arg), demo=True)  # never logged (T52)
                    self._focus_on = True
            except Exception:
                log.exception("present: demo focus box failed")


# ---------------------------------------------------------------- the runner
class Presenter:
    def __init__(self, actions, home=None, now=time.monotonic, sleep=asyncio.sleep, display_path=None,
                 load=None):
        self.actions = actions
        self.home = home
        self.now = now
        self.sleep = sleep
        self.path = display_path
        self.load = load or (lambda: load_script(self.home))
        self.task: asyncio.Task | None = None
        self.steps: list[dict] = []
        self.step = -1
        self.started = 0.0
        self.rehearse = False
        self.snap: dict | None = None
        self.was: dict = {}
        self.last: dict | None = None    # {"result": done|stopped|failed, "at": epoch, "steps": n}
        self.failed_steps: list[str] = []

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def status(self) -> dict:
        run = self.running
        if not self.steps:
            try:
                self.steps = self.load()
            except Exception:  # noqa: BLE001
                log.exception("presentation: couldn't read the script")
        return {"running": run, "rehearse": self.rehearse if run else False,
                "step": self.step + 1 if run else 0, "steps": len(self.steps),
                "name": self.steps[self.step]["name"] if run and 0 <= self.step < len(self.steps) else "",
                "names": [s["name"] for s in self.steps],
                "elapsed": round(self.now() - self.started, 1) if run else 0.0,
                "cap_s": TOTAL_CAP_S, "last": self.last}

    def start(self, rehearse: bool = False) -> dict:
        if self.running:
            raise RuntimeError("a presentation is already running")
        self.steps = self.load()
        self.snap = snapshot_display(self.path)
        save_state({"display": self.snap, "started": time.time(), "rehearse": rehearse, "pid": os.getpid()})
        self.rehearse = rehearse
        self.step = -1
        self.failed_steps = []
        self.started = self.now()
        try:
            self.was = self.actions.begin()
        except Exception:  # noqa: BLE001
            log.exception("presentation: setup failed; putting the wall back")
            self._teardown("failed")
            raise
        self.task = asyncio.get_running_loop().create_task(self._run())
        log.info("presentation %s: %d steps", "rehearsal" if rehearse else "started", len(self.steps))
        return self.status()

    async def stop(self) -> dict:
        """Halt at once: speech stops, the wall goes back. Fine to call when nothing is running."""
        t = self.task
        if t is not None and not t.done():
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        elif read_state():
            recover(self.path)
        return self.status()

    async def _run(self):
        result = "failed"
        try:
            for i, step in enumerate(self.steps):
                if self.now() - self.started >= TOTAL_CAP_S:
                    log.warning("presentation: %.0f s cap reached; ending early", TOTAL_CAP_S)
                    break
                self.step = i
                try:
                    await self._run_step(step)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("presentation: step %r failed; skipping it", step["name"])
                    self.failed_steps.append(step["name"])
            result = "done"
        except asyncio.CancelledError:
            result = "stopped"
            raise
        finally:
            self._teardown(result)

    async def _run_step(self, step: dict):
        t0 = self.now()
        pending = list(step["screen"])
        cap_left = TOTAL_CAP_S - (t0 - self.started)
        timeout = min(step["timeout"], max(cap_left, 0.0))

        def due(t):
            while pending and pending[0][0] <= t:
                at, verb, arg = pending.pop(0)
                try:
                    self.actions.screen(verb, arg, self.snap or {})
                except Exception:  # noqa: BLE001
                    log.exception("presentation: screen %s %s failed", verb, arg)

        due(0.0)
        # breathing room (owner, 12:12): "[pause]" / "[pause:1.5]" in a say: line splits it into beats with
        # silence between them; a beat is spoken only after the previous one finished plus the gap
        beats = split_beats(step["say"]) if step["say"] else []
        gap_until = 0.0           # 0 = the next beat may start as soon as the voice is quiet
        while True:
            if beats and not self.actions.speaking():
                tnow = self.now()
                if gap_until is None:                 # previous beat just finished: start its silence
                    gap_until = tnow + last_gap
                if tnow >= gap_until:
                    text, last_gap = beats.pop(0)
                    try:
                        self.actions.say(text)
                    except Exception:  # noqa: BLE001
                        log.exception("presentation: couldn't say step %r", step["name"])
                    gap_until = None
                    await self.sleep(TICK_S)          # let the voice pick it up before checking speaking()
                    continue
            t = self.now() - t0
            due(t)
            if t >= timeout:
                if pending or self.actions.speaking():
                    log.info("presentation: step %r timed out after %.0f s", step["name"], t)
                    self.actions.hush()
                due(float("inf"))     # never leave a step's screen half-done (e.g. "theme back")
                return
            if not pending and not beats and t >= step["hold"] and not self.actions.speaking():
                return
            await self.sleep(TICK_S)

    def _teardown(self, result: str):
        if result != "done":
            try:
                self.actions.hush()
            except Exception:  # noqa: BLE001
                log.exception("presentation: couldn't stop the voice")
        try:
            self.actions.end(self.was, self.snap or {})
        except Exception:  # noqa: BLE001
            log.exception("presentation: restore failed; trying the state file")
            recover(self.path)
        clear_state()
        self.last = {"result": result, "at": time.time(), "steps": self.step + 1, "rehearse": self.rehearse,
                     "skipped": list(self.failed_steps)}
        log.info("presentation %s after %.0f s", result, self.now() - self.started)


# ---------------------------------------------------------------- /api/app/present
ACTIONS = ("start", "rehearse", "stop")


def parse_request(d) -> str:
    if not isinstance(d, dict) or d.get("action") not in ACTIONS:
        raise ValueError("action must be start, rehearse or stop")
    return d["action"]


def add_routes(routes, apphub, json_body, conductor, home=None) -> Presenter:
    from aiohttp import web

    recover()   # a show cut off by a crash/restart: put the wall back now
    pr = Presenter(WallActions(conductor), home=home)

    @routes.get("/api/app/present")
    async def present_status(request):
        apphub.auth(request)
        return web.json_response(pr.status())

    @routes.post("/api/app/present")
    async def present_set(request):
        dev = apphub.auth(request)
        d = await json_body(request, 256)
        try:
            action = parse_request(d)
        except ValueError as e:
            return web.json_response({"error": str(e), **pr.status()}, status=400)
        if action == "stop":
            out = await pr.stop()
        else:
            try:
                out = pr.start(rehearse=action == "rehearse")
            except RuntimeError as e:
                return web.json_response({"error": str(e), "code": "running", **pr.status()}, status=409)
            except OSError as e:
                log.warning("presentation: couldn't start: %s", e)
                return web.json_response({"error": "couldn't start it"}, status=500)
        log.info("app: %s presentation %s", dev.get("name"), action)
        return web.json_response({"ok": True, **out})

    return pr
