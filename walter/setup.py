"""First-time setup: connect Walter's brain to Claude without a terminal.

Walter never handles Claude credentials. Anthropic's terms don't allow a
third-party app to collect, relay or store Claude sign-in codes or tokens, so:

* **Sign in with Claude** (the official way): Walter opens a sign-in window
  on his own screen running Claude Code's own `claude auth login`. The
  customer signs in on Anthropic's own page with a keyboard plugged into
  Walter; the code (if any) is typed straight into Claude Code's terminal.
  Walter only starts that window: he doesn't pipe its input or read its
  output, and he never reads ~/.claude.
* **Verify** only asks the CLI: `claude auth status --json` ("loggedIn").
* **Or your own Anthropic API key** (billed to the customer's own Anthropic
  Console account): typed into the same private phone page, saved to
  ~/.walter/secrets/anthropic.key (mode 600, folder 700) and passed to Claude
  Code only as ANTHROPIC_API_KEY in the environment of the processes Walter
  starts. Never logged. Claude sign-in codes and sk-ant-oat tokens are refused.

The phone page (QR on Walter's screen; served on the local network, protected
by a random key only shown on the screen) also sends the phone's time zone and
can set an optional admin password and the full-admin opt-in.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import socket
import time
from pathlib import Path
from typing import Callable

from aiohttp import web

from .config import REPO_DIR, Config, anthropic_key_path, claude_env, save_secret

log = logging.getLogger("walter.setup")

ADMIN_REQUEST_DIR = Path("/var/lib/walter-setup")  # a root-run path unit picks requests up
TERMINALS = ("foot", "xterm")
API_KEY_RE = re.compile(r"^sk-ant-api[0-9]{2}-[A-Za-z0-9_\-]{20,300}$")
PROMISE = "Walter uses your own Claude account or API key; Walter never sees your Claude password or login code."
REFUSE_MSG = ("Walter doesn't accept Claude sign-in codes or tokens. Sign in on Walter's screen with "
              "Claude's own sign-in window, or paste your own Anthropic API key (sk-ant-api…).")
SIGNIN_OPEN_MSG = ("Claude's sign-in window is open on Walter's screen. Sign in there with a keyboard "
                   "plugged into Walter, then press Verify.")
NO_SCREEN_MSG = ("Couldn't open a sign-in window on Walter's screen. On the Walter computer, open a "
                 "terminal (or SSH in as walter) and run: claude auth login. Then press Verify.")
TZ_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+\-]*(/[A-Za-z0-9_+\-]+){0,2}$")


def lan_ip() -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # no packets are sent; just picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def qr_svg(data: str) -> str:
    import qrcode
    import qrcode.image.svg
    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    return img.to_string(encoding="unicode")


class Setup:
    def __init__(self, cfg: Config, send: Callable[[dict], None], on_done: Callable[[], None]):
        self.cfg = cfg
        self.send = send
        self.on_done = on_done
        self.key = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(8))
        self.needed = False
        self.error: str | None = None
        self.signin_proc: asyncio.subprocess.Process | None = None
        self._site: web.TCPSite | None = None
        self._runner: web.AppRunner | None = None

    # ------------------------------------------------------------ status
    async def logged_in(self, strict: bool = False) -> bool:
        """`claude auth status --json` only; never reads credential files.
        strict: unknown CLI output counts as not signed in (the Verify button)."""
        exe = shutil.which(self.cfg.brain.claude_bin, path=claude_env(self.cfg)["PATH"])
        if not exe:
            self.error = "Claude Code isn't installed yet."
            return False
        try:
            p = await asyncio.create_subprocess_exec(
                exe, "auth", "status", "--json", env=claude_env(self.cfg),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(), 30)
            data = json.loads(out.decode() or "{}")
            return bool(data.get("loggedIn"))
        except Exception as e:
            log.warning("auth status failed: %s", type(e).__name__)
            # Unknown CLI output: at boot assume it's fine (don't lock out a working
            # brain); the Verify button wants a clear yes.
            return not strict

    def phone_url(self) -> str:
        host = lan_ip() or "walter.local"
        return f"http://{host}:{self.cfg.server.setup_port}/setup?k={self.key}"

    def announce(self):
        self.send({"t": "setup", "needed": self.needed, "phone_url": self.phone_url() if self.needed else None,
                   "error": self.error})

    async def check_and_start(self) -> bool:
        """Returns True if setup is needed (and starts the LAN setup page)."""
        self.needed = not await self.logged_in()
        if not self.needed and not Path("/var/lib/walter/admin-password-handled").exists():
            self.request_admin_password("")  # never set up via the phone: close the door
        if self.needed:
            await self._start_site()
            log.info("setup needed; phone page: %s", self.phone_url())
        self.announce()
        return self.needed

    # ------------------------------------------------------------- sign-in
    def _wayland_env(self) -> dict[str, str] | None:
        env = claude_env(self.cfg)
        if not env.get("WAYLAND_DISPLAY"):
            run = Path(env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
            socks = sorted(p.name for p in run.glob("wayland-[0-9]*") if not p.name.endswith(".lock"))
            if not socks:
                return None
            env["XDG_RUNTIME_DIR"] = str(run)
            env["WAYLAND_DISPLAY"] = socks[0]
        return env

    async def open_signin(self, auto_verify: bool = False) -> tuple[bool, str]:
        """Open Claude Code's own sign-in in a terminal window on Walter's screen.

        The terminal owns its keyboard and screen: nothing is piped to or from
        Walter. With auto_verify (started from the wall), Walter checks the CLI
        status when the window closes and finishes setup if it's signed in."""
        if self.signin_proc and self.signin_proc.returncode is None:
            return True, SIGNIN_OPEN_MSG
        env = self._wayland_env()
        term = next((t for t in TERMINALS if shutil.which(t, path=(env or {}).get("PATH"))), None)
        if not env or not term:
            return False, NO_SCREEN_MSG
        env["BROWSER"] = str(REPO_DIR / "deploy" / "signin-browser.sh")
        claude = self.cfg.brain.claude_bin
        script = (f"echo 'Sign in to Claude. Walter never sees your Claude password or login code.'; echo; "
                  f"{shlex.quote(claude)} auth login; echo; read -r -p 'Press Enter to go back to Walter. ' _")
        argv = [term, "-e", "bash", "-lc", script] if term == "xterm" else [term, "bash", "-lc", script]
        self.signin_proc = await asyncio.create_subprocess_exec(
            *argv, env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        log.info("opened the Claude sign-in window (%s)", term)
        if auto_verify:
            asyncio.create_task(self._verify_when_closed(self.signin_proc))
        return True, SIGNIN_OPEN_MSG

    async def _verify_when_closed(self, proc):
        await proc.wait()
        if self.needed and await self.logged_in():
            await self.finish()
        else:
            self.error = "Not signed in yet. Press Enter to try again."
            self.announce()

    async def verify(self) -> tuple[bool, str]:
        """Only asks the Claude Code CLI whether it's signed in."""
        if await self.logged_in(strict=True):
            return True, "Signed in. Waking Walter up."
        return False, self.error or "Claude Code isn't signed in yet. Finish signing in on Walter's screen, then press Verify."

    async def save_api_key(self, key: str) -> tuple[bool, str]:
        """The customer's own Anthropic API key (their own billing)."""
        key = (key or "").strip()
        if not key:
            return False, "Paste your Anthropic API key."
        if key.startswith("sk-ant-oat") or not API_KEY_RE.match(key):
            return False, REFUSE_MSG
        save_secret(anthropic_key_path(), key)
        log.info("saved the owner's Anthropic API key")  # never the key itself
        if await self.logged_in(strict=True):
            return True, "Key saved. Walter will use your own Anthropic API key and billing."
        return False, "Key saved, but Claude Code still says it can't sign in with it. Check the key."

    # ----------------------------------------------------- house-keeping
    async def set_timezone(self, tz: str) -> bool:
        if not TZ_RE.match(tz or "") or not Path("/usr/share/zoneinfo", tz).exists():
            return False
        os.environ["TZ"] = tz
        time.tzset()
        try:
            p = await asyncio.create_subprocess_exec("sudo", "-n", "timedatectl", "set-timezone", tz,
                                                     stdout=asyncio.subprocess.DEVNULL,
                                                     stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(p.wait(), 20)
            log.info("time zone set to %s", tz)
        except Exception as e:
            log.warning("couldn't set system time zone: %s", e)
        return True

    def request_admin_password(self, password: str, full_admin: bool = False):
        """Ask the root helper (deploy/admin-password.sh) to set the laptop's admin
        password and, if the owner ticked it, give Walter full admin rights
        (passwordless sudo). Applied once, during setup; any request, even an
        empty one, closes the door so it can't be used later."""
        if not ADMIN_REQUEST_DIR.is_dir():
            return

        def put(name: str, text: str):
            # write + rename, so the root helper never sees a half-written file
            tmp = ADMIN_REQUEST_DIR / f".{name}.tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(text)
            os.replace(tmp, ADMIN_REQUEST_DIR / name)

        if full_admin:
            put("full-admin", "yes\n")
        else:
            (ADMIN_REQUEST_DIR / "full-admin").unlink(missing_ok=True)
        put("admin-password", password)  # last: this one wakes the root helper

    async def finish(self, admin_password: str = "", full_admin: bool = False):
        self.request_admin_password(admin_password, full_admin)
        self.needed = False
        self.error = None
        self.announce()
        await self._stop_site()
        self.on_done()

    # ------------------------------------------------- the phone's page
    async def _start_site(self):
        if self._site:
            return
        app = web.Application()
        app.router.add_get("/setup", self._phone_page)
        app.router.add_get("/setup/ping", lambda r: web.json_response({"setup": self.needed}))
        app.router.add_post("/setup/api/{action}", self._phone_api)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", self.cfg.server.setup_port)
        await self._site.start()

    async def _stop_site(self):
        if self._runner:
            await asyncio.sleep(1)  # let the phone get its last answer
            await self._runner.cleanup()
        self._site = self._runner = None

    async def _phone_page(self, request):
        if request.query.get("k") != self.key or not self.needed:
            return web.Response(status=403, text="Scan the QR code on Walter's screen.")
        html = (REPO_DIR / "face" / "setup-phone.html").read_text()
        return web.Response(text=html.replace("{{KEY}}", self.key), content_type="text/html",
                            headers={"Cache-Control": "no-store"})

    async def _phone_api(self, request):
        try:
            d = await request.json()
        except Exception:
            d = {}
        if d.get("k") != self.key or not self.needed:
            raise web.HTTPForbidden()
        return await self.handle(request.match_info["action"], d)

    async def handle(self, action: str, d: dict):
        """Shared by the phone page and the on-screen keyboard fallback."""
        if action == "tz":
            return web.json_response({"ok": await self.set_timezone(d.get("tz", ""))})
        if action == "signin":
            ok, msg = await self.open_signin(auto_verify=d.get("from") == "wall")
            return web.json_response({"ok": ok, "message": msg})
        if action == "apikey":
            ok, msg = await self.save_api_key(d.get("key", ""))
            return web.json_response({"ok": ok, "message": msg})
        if action == "verify":
            ok, msg = await self.verify()
            if ok:
                asyncio.create_task(self.finish(d.get("admin_password", "") or "",
                                                full_admin=d.get("full_admin") is True))
            return web.json_response({"ok": ok, "message": msg})
        if action in ("code", "token", "login"):
            # The old flow relayed Claude's sign-in code / accepted pasted tokens. Gone for good.
            return web.json_response({"ok": False, "message": REFUSE_MSG}, status=410)
        raise web.HTTPNotFound()
