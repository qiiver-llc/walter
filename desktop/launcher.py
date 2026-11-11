"""walter-desktop: turn this laptop into a Walter face (docs/DESKTOP.md).

  walter-desktop pair http://WALL:8765 123456 --name Office [--room office]
  walter-desktop run [--kiosk|--window|--fullscreen] [--no-screen] [--no-ears] [--no-speaker] [--no-hands] [--no-tray]
  walter-desktop tray                     same as run, with the tray icon (needs pystray + Pillow)
  walter-desktop autostart enable|disable|show
  walter-desktop doctor                   is this computer ready? (plain-language fixes)
  walter-desktop status | open | forget
  walter-desktop hands status|enable|disable|stop|start|log
  walter-desktop hands allow-folder PATH | remove-folder PATH | allow-app NAME [COMMAND] | remove-app NAME
  walter-desktop hands commands on|off | allow-command "exact command"

The face page runs in a browser that's already installed (Chromium/Chrome/Edge in app or
kiosk mode with its own profile); the mic + speaker agent is walter/face_agent.py, run in
this process. Settings live in the per-OS folder from desktop/paths.py.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.parse
from pathlib import Path

from . import autostart as AS
from . import browser as B
from . import doctor as D
from . import paths as P

log = logging.getLogger("walter.desktop")
DEFAULTS = {"mode": "app", "screen": True, "ears": True, "speaker": True, "tray": True, "hands": True}


def load_settings(cfg_dir: Path) -> dict:
    try:
        d = json.loads(P.config_file(cfg_dir).read_text())
        return {**DEFAULTS, **{k: v for k, v in d.items() if k in DEFAULTS}}
    except (OSError, ValueError, AttributeError):
        return dict(DEFAULTS)


def save_settings(cfg_dir: Path, d: dict):
    P.ensure_dir(cfg_dir)
    P.config_file(cfg_dir).write_text(json.dumps({k: d[k] for k in DEFAULTS if k in d}, indent=1))


def use_dir(cfg_dir: Path):
    """The face agent keeps its token here too (face.json, mode 600)."""
    os.environ["WALTER_FACE_HOME"] = str(cfg_dir)


def self_argv(*extra: str) -> list[str]:
    """How to start this program again (for autostart)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *extra]
    return [sys.executable, "-m", "desktop", *extra]


# ------------------------------------------------------------------ the running face
class Face:
    def __init__(self, cfg_dir: Path, osn: str, settings: dict, brain: str, state: dict):
        self.cfg_dir, self.osn, self.settings = cfg_dir, osn, settings
        self.brain, self.state = brain, state
        self.agent = None
        self.hands = None
        self.browser = None
        self.stop = threading.Event()

    def page_url(self) -> str:
        # the token is in this address once; the brain swaps it for a cookie in the face's own profile
        return f"{self.brain}/face/?token={urllib.parse.quote(self.state['token'])}"

    def open_screen(self):
        self.browser = B.open_face(self.osn, self.page_url(), self.cfg_dir / "browser-profile",
                                   self.settings.get("mode", "app"))

    def start_agent(self):
        from walter import face_agent as A

        def go():
            while not self.stop.is_set():
                self.agent = A.Agent(self.brain, self.state["token"], ears=self.settings["ears"],
                                     speaker=self.settings["speaker"])
                try:
                    asyncio.run(self.agent.run())
                except Exception as e:
                    log.warning("face agent stopped (%s); restarting in 10 s", e)
                if not getattr(self.agent, "running", True) and not self.stop.is_set():
                    log.warning("this face was forgotten on the brain; pair again")
                    self.stop.set()
                    return
                self.stop.wait(10)

        threading.Thread(target=go, name="face-agent", daemon=True).start()

    def start_hands(self) -> bool:
        from walter import face_agent as A
        from walter import hands as H

        from .hands_agent import HandsAgent
        pol = H.load_policy(P.hands_file(self.cfg_dir))
        if not (self.settings.get("hands") and pol.enabled):
            return False
        self.hands = HandsAgent(A.Http(self.brain, self.state["token"], timeout=45), self.cfg_dir, self.osn, pol)
        if self.hands.start_hotkey():
            log.info("hands: kill-switch hotkey %s", pol.hotkey)
        threading.Thread(target=self.hands.run_forever, name="hands", daemon=True).start()
        log.info("hands: on (%d allowed folder(s), %d app(s))", len(pol.allowed_folders), len(pol.apps))
        return True

    def set_muted(self, on: bool):
        from walter import face_agent as A
        try:
            A.Http(self.brain, self.state["token"]).json("POST", "/api/face/settings", {"muted": bool(on)})
        except Exception as e:
            log.warning("couldn't change mute: %s", e)

    def quit(self):
        self.stop.set()
        if self.agent is not None:
            self.agent.running = False
        if self.hands is not None:
            self.hands.running = False
        if self.browser is not None:
            try:
                self.browser.terminate()
            except Exception:
                pass


def tray_icon_image():
    """A little glowing blob with two eyes (Pillow)."""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((4, 6, 60, 60), fill=(88, 196, 255, 255))
    d.ellipse((18, 22, 28, 36), fill=(255, 255, 255, 255))
    d.ellipse((36, 22, 46, 36), fill=(255, 255, 255, 255))
    return im


def run_tray(face: Face) -> bool:
    try:
        import pystray
    except Exception:
        return False
    muted = {"on": False}

    def toggle_mute(icon, item):
        muted["on"] = not muted["on"]
        face.set_muted(muted["on"])

    def hands_toggle(icon, item):
        from .hands_agent import stop_file
        f = stop_file(face.cfg_dir)
        if f.exists():
            f.unlink()
        elif face.hands is not None:
            face.hands.kill("tray")
        else:
            f.touch()

    def hands_label(item):
        from .hands_agent import stop_file
        return "Start hands" if stop_file(face.cfg_dir).exists() else "Stop hands (kill switch)"

    def settings(icon, item):
        open_folder(face.osn, face.cfg_dir)

    def quit_(icon, item):
        face.quit()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open face", lambda i, it: face.open_screen(), default=True),
        pystray.MenuItem("Mute microphone", toggle_mute, checked=lambda it: muted["on"]),
        pystray.MenuItem(hands_label, hands_toggle),
        pystray.MenuItem("Settings folder", settings),
        pystray.MenuItem("Quit", quit_))
    pystray.Icon("walter", tray_icon_image(), "Walter face", menu).run()
    return True


def open_folder(osn: str, path: Path):
    cmd = {"mac": ["open", str(path)], "windows": ["explorer", str(path)]}.get(osn, ["xdg-open", str(path)])
    try:
        subprocess.Popen(cmd)
    except OSError:
        print(path)


# ------------------------------------------------------------------ hands settings (on this laptop)
def hands_cmd(cfg_dir: Path, osn: str, args: list[str]) -> int:
    from walter import hands as H

    from .hands_agent import stop_file
    path = P.hands_file(cfg_dir)
    pol = H.load_policy(path)
    sub = args[0] if args else "status"
    rest = args[1:]
    if sub == "status":
        tools = H.detect_backend(osn)
        print(f"hands: {'ON' if pol.enabled else 'off'}{' (STOPPED by the kill switch)' if stop_file(cfg_dir).exists() else ''}")
        print("allowed folders: " + (", ".join(pol.allowed_folders) or "none"))
        print("apps: " + (", ".join(f"{k} ({v})" for k, v in sorted(pol.apps.items())) or "none"))
        print("approved sites: " + (", ".join(pol.approved_domains) or "none"))
        print(f"run_command: {'on, ' + str(len(pol.commands)) + ' exact command(s)' if pol.run_command else 'off'}")
        print("tools: " + ", ".join(f"{k}={v}" for k, v in sorted(tools.items()) if v))
        missing = [k for k in ("type", "click", "screenshot") if not tools.get(k)]
        if missing:
            print("missing: " + ", ".join(missing) + " -> " + H.install_hint(osn, tools))
        return 0
    if sub == "enable":
        if not pol.allowed_folders:
            for f in H.default_folders(osn):
                os.makedirs(f, exist_ok=True)
                pol.allowed_folders.append(os.path.realpath(f))
        pol.enabled = True
        msg = (f"hands ON. Allowed folder(s): {', '.join(pol.allowed_folders)}. "
               "Restart walter-desktop run to start them.")
    elif sub == "disable":
        pol.enabled = False
        msg = "hands off"
    elif sub == "stop":
        P.ensure_dir(cfg_dir)
        stop_file(cfg_dir).touch()
        H.audit(cfg_dir / "hands-audit.log", "stopped", reason="walter-desktop hands stop")
        print("hands STOPPED (kill switch). walter-desktop hands start to undo.")
        return 0
    elif sub == "start":
        try:
            stop_file(cfg_dir).unlink()
        except OSError:
            pass
        print("kill switch released" + ("" if pol.enabled else " (hands are still off: walter-desktop hands enable)"))
        return 0
    elif sub == "log":
        for e in H.read_audit(cfg_dir / "hands-audit.log", int(rest[0]) if rest else 30):
            print(json.dumps(e))
        return 0
    elif sub == "allow-folder" and rest:
        real = os.path.realpath(os.path.expanduser(rest[0]))
        if not os.path.isdir(real):
            print(f"no such folder: {real}", file=sys.stderr)
            return 1
        if H.is_secret_path(real) or real in ("/", os.path.realpath(os.path.expanduser("~"))):
            print("that folder is too broad or private; pick a project folder", file=sys.stderr)
            return 1
        if real not in pol.allowed_folders:
            pol.allowed_folders.append(real)
        msg = f"allowed: {real}"
    elif sub == "remove-folder" and rest:
        real = os.path.realpath(os.path.expanduser(rest[0]))
        pol.allowed_folders = [f for f in pol.allowed_folders if os.path.realpath(f) != real]
        msg = f"removed: {real}"
    elif sub == "allow-app" and rest:
        name = rest[0].strip().lower()
        pol.apps[name] = " ".join(rest[1:]) or name
        msg = f"app allowed: {name} -> {pol.apps[name]}"
    elif sub == "remove-app" and rest:
        pol.apps.pop(rest[0].strip().lower(), None)
        msg = f"app removed: {rest[0]}"
    elif sub == "commands" and rest and rest[0] in ("on", "off"):
        pol.run_command = rest[0] == "on"
        msg = f"run_command {rest[0]} (exact commands only: walter-desktop hands allow-command \"...\")"
    elif sub == "allow-command" and rest:
        cmd = " ".join(rest).strip()
        if cmd not in pol.commands:
            pol.commands.append(cmd)
        msg = f"command allowed (exactly): {cmd}"
    else:
        print(__doc__.strip())
        return 1
    H.save_policy(pol, path)
    print(msg)
    return 0


# ------------------------------------------------------------------ cli
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="walter-desktop", description="Turn this computer into a Walter face.")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("pair")
    p.add_argument("brain")
    p.add_argument("code")
    p.add_argument("--name", default="")
    p.add_argument("--room", default="")
    for name in ("run", "tray"):
        r = sub.add_parser(name)
        r.add_argument("--brain", default="")
        g = r.add_mutually_exclusive_group()
        for m in ("kiosk", "window", "fullscreen"):
            g.add_argument(f"--{m}", dest="mode", action="store_const", const={"window": "app"}.get(m, m))
        for flag in ("screen", "ears", "speaker", "hands", "tray"):
            r.add_argument(f"--no-{flag}", dest=flag, action="store_false", default=None)
        r.add_argument("--save", action="store_true", help="remember these choices")
    a_ = sub.add_parser("autostart")
    a_.add_argument("what", choices=["enable", "disable", "show"])
    sub.add_parser("doctor")
    sub.add_parser("status")
    sub.add_parser("open")
    sub.add_parser("forget")
    h = sub.add_parser("hands")
    h.add_argument("args", nargs="*")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    osn = P.os_name()
    cfg_dir = P.ensure_dir(P.config_dir())
    use_dir(cfg_dir)
    from walter import face_agent as A
    state = A.load_state()
    settings = load_settings(cfg_dir)

    if a.cmd == "pair":
        rc = A.main(["pair", a.brain, a.code, "--name", a.name, "--room", a.room, "--kind", "laptop"])
        if rc == 0:
            print("next: walter-desktop run   (and walter-desktop autostart enable to start it at login)")
        return rc
    if a.cmd == "doctor":
        text, rc = D.report(D.run_all(osn, state))
        print(text)
        return rc
    if a.cmd == "status":
        return A.main(["status"])
    if a.cmd == "forget":
        return A.main(["forget"])
    if a.cmd == "hands":
        return hands_cmd(cfg_dir, osn, a.args)
    if a.cmd == "autostart":
        argv_ = self_argv("run")
        if a.what == "show":
            print(f"# {AS.target(osn, str(Path.home()), systemd=AS.has_systemd_user() if osn == 'linux' else True)}")
            print(AS.render(osn, argv_, str(P.log_dir(cfg_dir)), systemd=AS.has_systemd_user() if osn == "linux" else True,
                            env={"PYTHONPATH": str(P.repo_root())} if not getattr(sys, "frozen", False) else None))
            return 0
        if a.what == "enable":
            P.log_dir(cfg_dir).mkdir(parents=True, exist_ok=True)
            env = {"PYTHONPATH": str(P.repo_root())} if not getattr(sys, "frozen", False) else None
            print(AS.enable(osn, argv_, str(P.log_dir(cfg_dir)), env=env))
        else:
            print(AS.disable(osn))
        return 0
    if a.cmd in ("run", "tray", "open"):
        if not state.get("token"):
            print("walter-desktop: pair first: walter-desktop pair http://<wall-ip>:8765 <code> --name Office",
                  file=sys.stderr)
            return 1
        brain = A.clean_brain(a.brain) if getattr(a, "brain", "") else (state.get("brain") or A.discover() or "")
        if not brain:
            print("walter-desktop: no brain address (use --brain http://<wall-ip>:8765)", file=sys.stderr)
            return 1
        if a.cmd != "open":
            for k in ("mode", "screen", "ears", "speaker", "hands", "tray"):
                v = getattr(a, k, None)
                if v is not None:
                    settings[k] = v
            if a.cmd == "tray":
                settings["tray"] = True
            if a.save:
                save_settings(cfg_dir, settings)
        face = Face(cfg_dir, osn, settings, brain, state)
        if a.cmd == "open":
            face.open_screen()
            return 0
        if settings["screen"]:
            face.open_screen()
        face.start_agent()
        face.start_hands()
        if settings["tray"] and run_tray(face):
            return 0
        if a.cmd == "tray":
            log.info("no tray here (pip install pystray pillow); running without it")
        try:
            while not face.stop.wait(1):
                pass
        except KeyboardInterrupt:
            pass
        face.quit()
        time.sleep(0.2)
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
