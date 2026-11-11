"""Cloud Link: reach this Walter's companion app from anywhere (docs/CLOUD-LINK.md).

The owner turns it on with a beta invite code:

    walterctl cloud-link enable CODE [--replace]
    walterctl cloud-link status | rotate | disable

(or the first-run wizard's "Reach Walter from anywhere" step, or the app's
Settings > Cloud Link card, both through the same functions here).

What happens on this box:

  * an install identity is made on first use: a random install_id (uuid4) and an
    Ed25519 keypair, in ~/.walter/secrets/cloud-link.key (mode 600);
  * the code is claimed at the Cloud Link service ([cloud_link] api_base, default
    https://api.wallter.app), which creates a private tunnel and a random address
    like <slug>.wallter.app for this household and hands back a tunnel token;
  * the token goes to ~/.walter/secrets/cloud-link.tunnel (mode 600, as
    TUNNEL_TOKEN=... for systemd's EnvironmentFile: never on a command line);
  * a systemd user unit walter-cloud-link.service runs cloudflared with it;
  * [app] public_url becomes https://<host> (the old value is kept and put back
    on disable), and the address is checked (GET https://<host>/app/, 60 s).

Every later call (status, heartbeat, rotate, release) is signed with the install
key: headers X-Walter-Install, X-Walter-Ts and X-Walter-Sig = base64url Ed25519
signature over "<METHOD>\\n<PATH>\\n<TS>\\n<sha256 hex of body>" (contract v1).
Only /app, /app/* and /api/app/* go through the tunnel (the service's ingress
rules); the rest of Walter stays on the home network.

A heartbeat is sent every [cloud_link] heartbeat_h hours (6) while it is on
(heartbeat_loop, started by __main__.py).

Safety: an install that already runs its own named tunnel for the app (a
cloudflared unit, or an https public_url that isn't ours) is refused unless
replace=True, and that existing tunnel unit is never touched.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger("walter.cloud_link")

UNIT = "walter-cloud-link.service"
DEFAULT_API = "https://api.wallter.app"
TIMEOUT_S = 20.0
HEALTH_S = 60.0
CODE_RE = re.compile(r"^[a-z0-9]{6,40}$")
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9+/=_.\-]{20,4096}$")


class CloudLinkError(Exception):
    """Something the owner should read (plain language). code: short machine reason."""

    def __init__(self, message: str, code: str = "error", status: int = 0):
        super().__init__(message)
        self.code = code
        self.status = status


# ------------------------------------------------------------ paths
def data_dir() -> Path:
    return Path(os.environ.get("WALTER_DATA", Path.home() / ".walter"))


def unit_dir() -> Path:
    return Path(os.environ.get("WALTER_SYSTEMD_USER_DIR", Path.home() / ".config" / "systemd" / "user"))


@dataclass
class Paths:
    data: Path = field(default_factory=data_dir)
    units: Path = field(default_factory=unit_dir)
    system_units: Path = Path("/etc/systemd/system")

    @property
    def key(self) -> Path:
        return self.data / "secrets" / "cloud-link.key"

    @property
    def tunnel(self) -> Path:
        return self.data / "secrets" / "cloud-link.tunnel"

    @property
    def state(self) -> Path:
        return self.data / "cloud-link.json"

    @property
    def config(self) -> Path:
        return self.data / "config.toml"

    @property
    def unit(self) -> Path:
        return self.units / UNIT


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64u_dec(s: str) -> bytes:
    s = str(s or "")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _write_private(path: Path, text: str):
    """Mode 600 file in a mode 700 folder, atomic."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name("." + path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def walter_version() -> str:
    from .config import REPO_DIR
    for p in (REPO_DIR / "VERSION",):
        try:
            v = p.read_text().strip()
            if v:
                return v[:40]
        except OSError:
            pass
    return "dev"


def clean_code(code) -> str:
    """Invite codes are case-insensitive; spaces and dashes are ignored."""
    return re.sub(r"[\s\-_]+", "", str(code or "")).lower()


# ------------------------------------------------------------ identity + signing
class Identity:
    """install_id + Ed25519 key, in secrets/cloud-link.key (JSON, mode 600)."""

    def __init__(self, install_id: str, private_raw: bytes):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        self.install_id = install_id
        self._key = Ed25519PrivateKey.from_private_bytes(private_raw)

    @classmethod
    def load_or_create(cls, path: Path) -> "Identity":
        try:
            d = json.loads(path.read_text())
            return cls(str(uuid.UUID(d["install_id"])), b64u_dec(d["private_key"]))
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError) as e:
            raise CloudLinkError(f"The Cloud Link key file is damaged ({path}); move it away and try again.",
                                 "bad_key") from e
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        k = Ed25519PrivateKey.generate()
        raw = k.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                              serialization.NoEncryption())
        ident = cls(str(uuid.uuid4()), raw)
        _write_private(path, json.dumps({"install_id": ident.install_id, "private_key": b64u(raw),
                                         "created": int(time.time())}) + "\n")
        log.info("cloud link: new install identity %s", ident.install_id)
        return ident

    @property
    def public_b64u(self) -> str:
        from cryptography.hazmat.primitives import serialization
        return b64u(self._key.public_key().public_bytes(serialization.Encoding.Raw,
                                                        serialization.PublicFormat.Raw))

    def sign(self, method: str, path: str, body: bytes, ts: int | None = None) -> dict[str, str]:
        ts = int(time.time()) if ts is None else int(ts)
        msg = signing_message(method, path, ts, body)
        return {"X-Walter-Install": self.install_id, "X-Walter-Ts": str(ts),
                "X-Walter-Sig": b64u(self._key.sign(msg))}


def signing_message(method: str, path: str, ts: int, body: bytes) -> bytes:
    return f"{method.upper()}\n{path}\n{int(ts)}\n{hashlib.sha256(body or b'').hexdigest()}".encode()


# ------------------------------------------------------------ the service API
class Api:
    """Signed JSON calls to the Cloud Link service (contract v1)."""

    def __init__(self, base: str, ident: Identity, timeout: float = TIMEOUT_S):
        base = (base or DEFAULT_API).strip().rstrip("/")
        if not base.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise CloudLinkError("[cloud_link] api_base must be an https:// address.", "bad_config")
        self.base, self.ident, self.timeout = base, ident, timeout

    async def call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        import aiohttp
        from urllib.parse import urlsplit
        url = self.base + path
        raw = json.dumps(body, separators=(",", ":")).encode() if body is not None else b""
        headers = {"Accept": "application/json", "User-Agent": f"walter/{walter_version()}",
                   **self.ident.sign(method, urlsplit(url).path, raw)}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as s:
                async with s.request(method, url, data=raw if body is not None else None,
                                     headers=headers, allow_redirects=False) as r:
                    text = await r.text()
                    try:
                        data = json.loads(text) if text else {}
                    except ValueError:
                        data = {}
                    return r.status, data if isinstance(data, dict) else {}
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise CloudLinkError(f"Couldn't reach the Cloud Link service ({type(e).__name__}). "
                                 "Check the internet connection and try again.", "offline") from e

    async def claim(self, code: str) -> tuple[int, dict]:
        return await self.call("POST", "/v1/claim", {"invite_code": code, "install_id": self.ident.install_id,
                                                     "install_pubkey": self.ident.public_b64u,
                                                     "walter_version": walter_version()})

    async def status(self) -> tuple[int, dict]:
        return await self.call("GET", "/v1/status")

    async def heartbeat(self, healthy: bool) -> tuple[int, dict]:
        return await self.call("POST", "/v1/heartbeat", {"version": walter_version(), "healthy": bool(healthy)})

    async def rotate(self) -> tuple[int, dict]:
        return await self.call("POST", "/v1/rotate", {})

    async def release(self) -> tuple[int, dict]:
        return await self.call("POST", "/v1/release", {})


CLAIM_ERRORS = {
    400: ("That doesn't look like an invite code. Check it and try again.", "bad_code"),
    403: ("That invite code isn't valid: it may be mistyped, already used, or expired.", "invalid_code"),
    409: ("This Walter already has a Cloud Link, but the service wouldn't hand it back.", "already_claimed"),
    429: ("Too many tries just now. Wait a few minutes and try again.", "rate_limited"),
}


def _household(d: dict) -> tuple[str, str]:
    hh = d.get("household") if isinstance(d.get("household"), dict) else {}
    slug, host = str(hh.get("slug") or ""), str(hh.get("host") or "").lower().rstrip(".")
    if not slug or not HOST_RE.match(host):
        raise CloudLinkError("The Cloud Link service sent an answer I don't understand.", "bad_reply")
    return slug, host


def _token(d: dict) -> str:
    t = str(d.get("tunnel_token") or "")
    if not TOKEN_RE.match(t):
        raise CloudLinkError("The Cloud Link service didn't send a usable tunnel token.", "bad_reply")
    return t


# ------------------------------------------------------------ systemd + cloudflared
def find_cloudflared() -> str:
    for p in (shutil.which("cloudflared"), "/usr/bin/cloudflared", "/usr/local/bin/cloudflared",
              str(Path.home() / ".local" / "bin" / "cloudflared")):
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return ""


def render_unit(cloudflared: str, token_file: Path) -> str:
    """The user unit. The token only ever travels via EnvironmentFile (TUNNEL_TOKEN)."""
    if not cloudflared or any(c in cloudflared for c in " \n\"'"):
        raise CloudLinkError("cloudflared's path looks odd; install it with the Walter installer.", "no_cloudflared")
    return (
        "# Written by Walter (walter/cloud_link.py). `walterctl cloud-link disable` removes it.\n"
        "[Unit]\n"
        "Description=Walter Cloud Link (private tunnel for the companion app)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n"
        "StartLimitIntervalSec=0\n"
        "\n"
        "[Service]\n"
        f"EnvironmentFile={token_file}\n"
        f"ExecStart={cloudflared} tunnel --no-autoupdate run\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "NoNewPrivileges=yes\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def systemctl(*args: str, timeout: float = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def existing_tunnels(paths: Paths, public_url: str, our_host: str = "") -> list[str]:
    """Reasons this install already has its own tunnel for the app ([] = none)."""
    found = []
    for d in (paths.units, paths.system_units):
        try:
            files = sorted(d.glob("*.service"))
        except OSError:
            continue
        for f in files:
            if f.name == UNIT:
                continue
            try:
                txt = f.read_text(errors="replace")
            except OSError:
                continue
            if re.search(r"^\s*ExecStart=.*cloudflared.*\btunnel\b", txt, re.M):
                found.append(f"the {f.name} tunnel unit ({d})")
    pub = (public_url or "").strip().rstrip("/").lower()
    # a Cloud Link household address (12-char random slug) is ours from an earlier enable
    ours = bool(re.fullmatch(r"https://[bcdfghjkmnpqrstvwxz][bcdfghjkmnpqrstvwxz2-9]{11}\.wallter\.app", pub))
    if pub.startswith("https://") and not ours and (not our_host or pub != f"https://{our_host}"):
        found.append(f"the app address is already set to {public_url}")
    return found


# ------------------------------------------------------------ config ([app] public_url)
def read_public_url(paths: Paths) -> str:
    import tomllib
    try:
        with open(paths.config, "rb") as f:
            return str(tomllib.load(f).get("app", {}).get("public_url") or "")
    except (OSError, ValueError):
        return ""


def file_set_public_url(paths: Paths, url: str) -> str:
    """Write [app] public_url to config.toml, then ask the running Walter to reload it
    (best effort; [app] is live). Returns a note for the owner."""
    from . import live
    live.set_in_file(paths.config, "app.public_url", url)
    try:
        import urllib.request
        from .config import load
        cfg = load(paths.config)
        req = urllib.request.Request(f"http://127.0.0.1:{cfg.server.port}/api/config/reload", method="POST",
                                     data=b"{}", headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        return ""
    except Exception:
        return "Walter isn't running; the new address applies when he starts."


# ------------------------------------------------------------ the manager
class CloudLink:
    def __init__(self, api_base: str = DEFAULT_API, *, paths: Paths | None = None,
                 run: Callable[..., tuple[int, str]] = systemctl,
                 set_public_url: Callable[[str], object] | None = None,
                 get_public_url: Callable[[], str] | None = None,
                 cloudflared: Callable[[], str] = find_cloudflared,
                 health: Callable | None = None, health_s: float = HEALTH_S):
        self.paths = paths or Paths()
        self.api_base = api_base or DEFAULT_API
        self.run = run
        self._set_url = set_public_url or (lambda url: file_set_public_url(self.paths, url))
        self._get_url = get_public_url or (lambda: read_public_url(self.paths))
        self.find_cloudflared = cloudflared
        self.health = health or check_health
        self.health_s = health_s

    @classmethod
    def from_config(cls, cfg, **kw) -> "CloudLink":
        cl = getattr(cfg, "cloud_link", None)
        return cls(getattr(cl, "api_base", DEFAULT_API), health_s=float(getattr(cl, "health_timeout_s", HEALTH_S)), **kw)

    # -- local state (not secret)
    def state(self) -> dict:
        try:
            d = json.loads(self.paths.state.read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_state(self, d: dict):
        _write_private(self.paths.state, json.dumps(d, indent=1) + "\n")

    def identity(self) -> Identity:
        return Identity.load_or_create(self.paths.key)

    def api(self) -> Api:
        return Api(self.api_base, self.identity())

    @property
    def enabled(self) -> bool:
        return bool(self.state().get("enabled"))

    def unit_active(self) -> bool:
        rc, out = self.run("is-active", UNIT)
        return rc == 0 and out.strip().splitlines()[-1:] == ["active"]

    # -- pieces
    def write_token(self, token: str):
        _write_private(self.paths.tunnel, f"TUNNEL_TOKEN={token}\n")

    def install_unit(self):
        exe = self.find_cloudflared()
        if not exe:
            raise CloudLinkError("cloudflared isn't installed. Re-run the Walter installer (it adds it), "
                                 "or install Cloudflare's cloudflared package.", "no_cloudflared")
        self.paths.units.mkdir(parents=True, exist_ok=True)
        tmp = self.paths.unit.with_name("." + UNIT + ".tmp")
        tmp.write_text(render_unit(exe, self.paths.tunnel))
        os.replace(tmp, self.paths.unit)

    def start_unit(self) -> str:
        self.run("daemon-reload")
        rc, out = self.run("enable", "--now", UNIT)
        if rc != 0:
            raise CloudLinkError(f"Couldn't start the Cloud Link connector: {out[-300:]}", "unit_failed")
        return out

    def stop_unit(self):
        self.run("disable", "--now", UNIT)
        try:
            self.paths.unit.unlink()
        except FileNotFoundError:
            pass
        self.run("daemon-reload")

    # -- actions
    async def enable(self, code: str, replace: bool = False) -> dict:
        code = clean_code(code)
        if not CODE_RE.match(code):
            raise CloudLinkError("That doesn't look like an invite code (letters and numbers only).", "bad_code")
        st = self.state()
        if st.get("enabled"):
            raise CloudLinkError(f"Cloud Link is already on at https://{st.get('host')}.", "already_on")
        current_url = self._get_url()
        others = existing_tunnels(self.paths, current_url, st.get("host") or st.get("last_host") or "")
        if others and not replace:
            raise CloudLinkError("This Walter already has its own tunnel for the app: " + "; ".join(others) +
                                 ". Cloud Link would replace that address. To go ahead anyway, use --replace "
                                 "(your existing tunnel is left running and untouched).", "existing_tunnel")
        if not self.find_cloudflared():
            raise CloudLinkError("cloudflared isn't installed. Re-run the Walter installer (it adds it), "
                                 "or install Cloudflare's cloudflared package.", "no_cloudflared")
        api = self.api()
        status, d = await api.claim(code)
        if status == 409 and d.get("tunnel_token"):
            status = 200          # same install, same key: the household and a fresh token come back
        if status != 200:
            msg, why = CLAIM_ERRORS.get(status, (f"The Cloud Link service said no (HTTP {status}).", "refused"))
            raise CloudLinkError(msg, why, status)
        slug, host = _household(d)
        token = _token(d)
        try:
            self.write_token(token)
            self.install_unit()
            await asyncio.to_thread(self.start_unit)
        except Exception:
            # the household stays claimed by this install's key: enabling again with the
            # same code gets it back (409 + fresh token), so just tidy up here
            await asyncio.to_thread(self.stop_unit)
            self.paths.tunnel.unlink(missing_ok=True)
            raise
        if st.get("host") == host:
            prev = st.get("previous_public_url") or ""
        else:   # never "restore" to the link's own address later
            prev = "" if current_url.strip().rstrip("/").lower() == f"https://{host}" else current_url
        new = {"enabled": True, "slug": slug, "host": host, "install_id": api.ident.install_id,
               "enabled_at": int(time.time()), "previous_public_url": prev or "",
               "replaced": others if replace else [], "remote_status": d.get("status") or "active",
               "last_heartbeat": 0}
        self.save_state(new)
        note = self._set_url(f"https://{host}") or ""
        ok = await self.health(host, self.health_s)
        new["healthy"] = bool(ok)
        self.save_state(new)
        log.info("cloud link: on at %s (healthy=%s)", host, ok)
        return {"ok": True, "host": host, "slug": slug, "url": f"https://{host}", "healthy": bool(ok),
                "note": note if isinstance(note, str) else "", "replaced": new["replaced"]}

    async def disable(self) -> dict:
        st = self.state()
        released, warn = False, ""
        if st.get("enabled") or self.paths.unit.exists() or self.paths.tunnel.exists():
            try:
                status, _ = await self.api().release()
                released = status == 200
                if not released:
                    warn = f"The service didn't confirm the release (HTTP {status}); it's switched off here anyway."
            except CloudLinkError as e:
                warn = f"{e} It's switched off here anyway; the address stops working once this box is offline."
        else:
            return {"ok": True, "was_on": False, "released": False, "warning": ""}
        await asyncio.to_thread(self.stop_unit)
        try:
            self.paths.tunnel.unlink()
        except FileNotFoundError:
            pass
        restored = ""
        host = st.get("host") or ""
        if host and self._get_url().strip().rstrip("/").lower() == f"https://{host}":
            restored = st.get("previous_public_url") or ""
            self._set_url(restored)
        self.save_state({"enabled": False, "released_at": int(time.time()), "last_host": host,
                         "install_id": st.get("install_id", "")})
        log.info("cloud link: off (released=%s)", released)
        return {"ok": True, "was_on": True, "released": released, "warning": warn, "restored_url": restored}

    async def rotate(self) -> dict:
        st = self.state()
        if not st.get("enabled"):
            raise CloudLinkError("Cloud Link is off.", "off")
        status, d = await self.api().rotate()
        if status != 200:
            raise CloudLinkError(f"The Cloud Link service couldn't rotate the token (HTTP {status}).", "refused", status)
        self.write_token(_token(d))
        rc, out = await asyncio.to_thread(self.run, "restart", UNIT)
        if rc != 0:
            raise CloudLinkError(f"New token saved, but the connector didn't restart: {out[-300:]}", "unit_failed")
        st["rotated_at"] = int(time.time())
        self.save_state(st)
        return {"ok": True, "host": st.get("host")}

    async def status(self, remote: bool = True) -> dict:
        st = self.state()
        out = {"enabled": bool(st.get("enabled")), "host": st.get("host") or "",
               "url": f"https://{st['host']}" if st.get("host") else "",
               "connector": "running" if st.get("enabled") and self.unit_active() else "stopped",
               "last_heartbeat": st.get("last_heartbeat") or 0, "remote_status": st.get("remote_status") or "",
               "healthy": st.get("healthy"), "api_base": self.api_base, "cloudflared": bool(self.find_cloudflared())}
        if not st.get("enabled"):
            others = existing_tunnels(self.paths, self._get_url())
            out["existing_tunnel"] = others
        if remote and st.get("enabled"):
            try:
                code, d = await self.api().status()
                if code == 200:
                    out["remote_status"] = str(d.get("status") or "")
                    out["last_seen"] = d.get("last_seen")
                    st["remote_status"] = out["remote_status"]
                    self.save_state(st)
                else:
                    out["remote_error"] = f"HTTP {code}"
            except CloudLinkError as e:
                out["remote_error"] = str(e)
        return out

    async def heartbeat(self) -> dict | None:
        st = self.state()
        if not st.get("enabled"):
            return None
        healthy = self.unit_active()
        code, d = await self.api().heartbeat(healthy)
        st["last_heartbeat"] = int(time.time())
        if code == 200 and d.get("status"):
            st["remote_status"] = str(d["status"])
            if d["status"] == "revoked":
                log.warning("cloud link: the service says this household is revoked")
        self.save_state(st)
        return {"code": code, **d}


async def check_health(host: str, within_s: float = HEALTH_S) -> bool:
    """GET https://<host>/app/ until it answers 200, for up to within_s seconds."""
    import aiohttp
    deadline = time.monotonic() + max(0.0, within_s)
    while True:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get(f"https://{host}/app/", allow_redirects=False) as r:
                    if r.status == 200:
                        return True
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            pass
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(3)


async def heartbeat_loop(cfg, first_delay: float = 120.0):
    """Signed heartbeat every [cloud_link] heartbeat_h hours while Cloud Link is on."""
    await asyncio.sleep(first_delay)
    while True:
        every = max(0.25, float(getattr(getattr(cfg, "cloud_link", None), "heartbeat_h", 6.0))) * 3600
        try:
            cl = CloudLink.from_config(cfg)
            if cl.enabled:
                st = cl.state()
                if time.time() - float(st.get("last_heartbeat") or 0) >= every - 60:
                    r = await cl.heartbeat()
                    log.info("cloud link heartbeat: %s", (r or {}).get("status") or (r or {}).get("code"))
        except CloudLinkError as e:
            log.info("cloud link heartbeat failed: %s", e)
        except Exception:
            log.exception("cloud link heartbeat")
        await asyncio.sleep(min(every, 3600))


# ------------------------------------------------------------ CLI
USAGE = """walterctl cloud-link enable CODE [--replace]   turn on with an invite code
walterctl cloud-link status                    is it on, the address, is it connected
walterctl cloud-link rotate                    get a fresh tunnel token
walterctl cloud-link disable                   turn it off and give the address back"""


def _when(ts) -> str:
    if not ts:
        return "never"
    return time.strftime("%b %d, %I:%M %p", time.localtime(float(ts)))


def cli(argv: list[str], out=print, cfg=None, cl: CloudLink | None = None) -> int:
    if cl is None:
        from .config import load
        cl = CloudLink.from_config(cfg or load())
    sub = argv[0] if argv else "status"
    try:
        if sub == "enable":
            args = [a for a in argv[1:] if a != "--replace"]
            if len(args) != 1:
                out("usage: walterctl cloud-link enable CODE [--replace]")
                return 2
            out("Claiming the invite code and setting up the private link...")
            r = asyncio.run(cl.enable(args[0], replace="--replace" in argv))
            out(f"Cloud Link is ON. The app now works from anywhere at {r['url']}/app")
            if r.get("replaced"):
                out("Your existing tunnel was left running and untouched; you can stop it yourself if you like.")
            if not r["healthy"]:
                out("The address isn't answering yet; the first connection can take a minute or two. "
                    "Check again with: walterctl cloud-link status")
            if r.get("note"):
                out(r["note"])
            out("Re-open the app on your phone through the new address (or pair it again there).")
            return 0
        if sub == "disable":
            r = asyncio.run(cl.disable())
            if not r["was_on"]:
                out("Cloud Link was already off.")
                return 0
            out("Cloud Link is OFF. The app works on your home network only"
                + (f" (and at {r['restored_url']} again)." if r.get("restored_url") else "."))
            if r.get("warning"):
                out(r["warning"])
            return 0
        if sub == "rotate":
            r = asyncio.run(cl.rotate())
            out(f"New tunnel token in place; the connector restarted. Same address: https://{r['host']}")
            return 0
        if sub == "status":
            s = asyncio.run(cl.status())
            if not s["enabled"]:
                out("Cloud Link is OFF.")
                if s.get("existing_tunnel"):
                    out("This Walter has its own tunnel: " + "; ".join(s["existing_tunnel"]))
                if not s["cloudflared"]:
                    out("cloudflared isn't installed yet (the installer adds it).")
                out("Turn it on with: walterctl cloud-link enable CODE")
                return 0
            out(f"Cloud Link is ON at {s['url']}/app")
            out(f"Connector on this box: {s['connector']}")
            if s.get("remote_status"):
                out(f"Service says: {s['remote_status']}" + (f" (last seen {s['last_seen']})" if s.get("last_seen") else ""))
            if s.get("remote_error"):
                out(f"Couldn't ask the service: {s['remote_error']}")
            out(f"Last heartbeat: {_when(s['last_heartbeat'])}")
            return 0
        out(USAGE)
        return 0 if sub in ("help", "-h", "--help") else 2
    except CloudLinkError as e:
        out(f"Cloud Link: {e}")
        return 3 if e.code == "existing_tunnel" else 1


if __name__ == "__main__":
    import sys
    sys.exit(cli(sys.argv[1:]))
