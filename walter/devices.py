"""Companion app devices: pairing, tokens, rate limits and who-is-asking.

The companion app (app/, served at /app) can control Walter, so only paired
devices may use it:

  * `walterctl pair` starts a pairing window: a 6-digit code (shown on the face
    with a QR link) that is valid for a few minutes and a handful of tries;
  * the phone sends the code plus a device name and gets a long random token
    back, once; only its SHA-256 is stored, in ~/.walter/devices.json (mode 600,
    never in git);
  * every /api/app/* call and the app websocket present the token;
  * `walterctl devices` lists devices, `walterctl revoke NAME` deletes one.

Wrong codes and tokens are rate-limited per client address, and globally.
Requests through a reverse proxy on this machine (Tailscale serve, cloudflared)
arrive from loopback: X-Forwarded-* headers are believed only from loopback,
and a proxied request is never treated as the kiosk.

Stdlib only: imported by the server and by walterctl.
"""
from __future__ import annotations

import contextlib
try:
    import fcntl
except ImportError:   # Windows (a desktop face only reads mic_score/clean_name; no stores)
    fcntl = None
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import time
from pathlib import Path

PAIR_TTL_S = 300          # a pairing code is valid this long...
PAIR_MAX_TRIES = 5        # ...for this many wrong guesses (then it is burned)
MAX_DEVICES = 20
NAME_MAX = 40
TOKEN_BYTES = 32          # 256-bit device tokens


def data_dir() -> Path:
    return Path(os.environ.get("WALTER_DATA", Path.home() / ".walter"))


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def clean_name(s, limit: int = NAME_MAX) -> str:
    s = re.sub(r"[\x00-\x1f\x7f<>\"'`\\]+", " ", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit].strip()


class DeviceStore:
    """~/.walter/devices.json: {"devices": [...], "pairing": {...}}.

    Read from disk on every check (cached by mtime) so walterctl can pair and
    revoke while the server runs, without talking to it."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else data_dir() / "devices.json"
        self._cache: tuple[float, dict] | None = None

    # ------------------------------------------------------------ storage
    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock")
        with open(lock, "a") as f:
            if fcntl is None:
                yield
                return
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _read(self) -> dict:
        try:
            mt = self.path.stat().st_mtime_ns
        except OSError:
            return {"devices": [], "pairing": None}
        if self._cache and self._cache[0] == mt:
            return json.loads(json.dumps(self._cache[1]))
        try:
            d = json.loads(self.path.read_text())
            if not isinstance(d, dict):
                raise ValueError
        except (OSError, ValueError):
            d = {}
        d.setdefault("devices", [])
        d.setdefault("pairing", None)
        self._cache = (mt, d)
        return json.loads(json.dumps(d))

    def _write(self, d: dict):
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)
        self._cache = None

    # ------------------------------------------------------------ pairing
    def start_pairing(self, now: float | None = None, ttl: float = PAIR_TTL_S) -> dict:
        """New 6-digit code (replaces any open one). Returns {"code", "expires"}."""
        now = time.time() if now is None else now
        code = f"{secrets.randbelow(1_000_000):06d}"
        with self._locked():
            d = self._read()
            d["pairing"] = {"code_sha256": token_hash(code), "expires": now + ttl, "tries": 0}
            self._write(d)
        return {"code": code, "expires": now + ttl}

    def pairing_open(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        p = self._read().get("pairing")
        return bool(p and p.get("expires", 0) > now and p.get("tries", 0) < PAIR_MAX_TRIES)

    def cancel_pairing(self):
        with self._locked():
            d = self._read()
            d["pairing"] = None
            self._write(d)

    def pair(self, code: str, name: str, owner: str = "", now: float | None = None) -> tuple[dict, str]:
        """Check the code; on success add the device and return (device, token).
        Raises PermissionError (wrong/expired code) or ValueError (bad name)."""
        now = time.time() if now is None else now
        name, owner = clean_name(name), clean_name(owner)
        if not name:
            raise ValueError("give this device a name")
        code = re.sub(r"\D", "", str(code or ""))
        with self._locked():
            d = self._read()
            p = d.get("pairing")
            if not p or p.get("expires", 0) <= now or p.get("tries", 0) >= PAIR_MAX_TRIES:
                raise PermissionError("no pairing code is open: run 'walterctl pair' (or ask Walter) first")
            if len(code) != 6 or not hmac.compare_digest(token_hash(code), p.get("code_sha256", "")):
                p["tries"] = p.get("tries", 0) + 1
                if p["tries"] >= PAIR_MAX_TRIES:
                    d["pairing"] = None   # burned: start a new one
                self._write(d)
                raise PermissionError("wrong code")
            if len(d["devices"]) >= MAX_DEVICES:
                raise ValueError("too many devices: revoke one first")
            base, n = name, 2
            taken = {x["name"].lower() for x in d["devices"]}
            while name.lower() in taken:
                name = f"{base[:NAME_MAX - 3]} {n}"
                n += 1
            token = secrets.token_urlsafe(TOKEN_BYTES)
            dev = {"id": secrets.token_hex(4), "name": name, "owner": owner,
                   "token_sha256": token_hash(token), "created": now, "last_seen": now, "last_ip": ""}
            d["devices"].append(dev)
            d["pairing"] = None   # one device per code
            self._write(d)
        return public(dev), token

    # ------------------------------------------------------------ devices
    def check(self, token: str, now: float | None = None) -> dict | None:
        """The device this token belongs to, or None (also None once it has been idle
        longer than the store's idle_days policy: an idle logout)."""
        if not token or not isinstance(token, str) or len(token) > 200:
            return None
        h = token_hash(token)
        d = self._read()
        for dev in d["devices"]:
            if hmac.compare_digest(dev.get("token_sha256", ""), h):
                if self._expired(dev, d, now):
                    return None
                return public(dev)
        return None

    # ------------------------------------------------------------ idle logout
    @staticmethod
    def _expired(dev: dict, d: dict, now: float | None = None) -> bool:
        days = (d.get("policy") or {}).get("idle_days") or 0
        if not days:
            return False
        now = time.time() if now is None else now
        last = dev.get("last_seen") or dev.get("created") or 0
        return now - last > float(days) * 86400

    def idle_days(self) -> float:
        return float((self._read().get("policy") or {}).get("idle_days") or 0)

    def set_idle_days(self, days: float):
        """Devices unused this many days must pair again (0 = never). Live, no restart."""
        days = float(days)
        if days < 0 or days > 3650:
            raise ValueError("idle days must be 0 to 3650")
        with self._locked():
            d = self._read()
            d.setdefault("policy", {})["idle_days"] = days
            self._write(d)

    def prune_idle(self, now: float | None = None) -> list[str]:
        """Remove devices past the idle limit. Returns their names."""
        with self._locked():
            d = self._read()
            gone = [x for x in d["devices"] if self._expired(x, d, now)]
            if gone:
                d["devices"] = [x for x in d["devices"] if x not in gone]
                self._write(d)
        return [x["name"] for x in gone]

    def touch(self, dev_id: str, ip: str = "", now: float | None = None, min_gap: float = 300):
        """Note when a device was last seen (written at most every `min_gap` s)."""
        now = time.time() if now is None else now
        d = self._read()
        dev = next((x for x in d["devices"] if x["id"] == dev_id), None)
        if not dev or (now - dev.get("last_seen", 0) < min_gap and dev.get("last_ip") == ip):
            return
        with self._locked():
            d = self._read()
            for x in d["devices"]:
                if x["id"] == dev_id:
                    x["last_seen"], x["last_ip"] = now, ip
            self._write(d)

    def list(self) -> list[dict]:
        return [public(x) for x in self._read()["devices"]]

    def revoke(self, ref: str) -> list[str]:
        """Remove devices by name (case-insensitive), id, or "all". Returns removed names."""
        ref = str(ref or "").strip()
        with self._locked():
            d = self._read()
            gone = [x for x in d["devices"]
                    if ref.lower() == "all" or x["name"].lower() == ref.lower() or x["id"] == ref]
            if gone:
                d["devices"] = [x for x in d["devices"] if x not in gone]
                self._write(d)
        return [x["name"] for x in gone]

    def ids(self) -> set[str]:
        return {x["id"] for x in self._read()["devices"]}


def public(dev: dict) -> dict:
    return {k: v for k, v in dev.items() if k != "token_sha256"}


# ---------------------------------------------------------------- rate limits
class RateLimiter:
    """Failures per key in a sliding window; too many = blocked for `block_s`.
    Also a global budget so many addresses together can't guess either."""

    def __init__(self, max_fails: int = 5, window_s: float = 600, block_s: float = 900,
                 global_max: int = 30):
        self.max_fails, self.window_s, self.block_s, self.global_max = max_fails, window_s, block_s, global_max
        self.fails: dict[str, list[float]] = {}
        self.blocked: dict[str, float] = {}
        self.all_fails: list[float] = []

    def retry_after(self, key: str, now: float | None = None) -> float:
        """Seconds until `key` may try again (0 = now)."""
        now = time.time() if now is None else now
        until = self.blocked.get(key, 0)
        if until > now:
            return until - now
        self.all_fails = [t for t in self.all_fails if now - t < self.window_s]
        if len(self.all_fails) >= self.global_max:
            return self.window_s - (now - self.all_fails[0])
        return 0.0

    def fail(self, key: str, now: float | None = None):
        now = time.time() if now is None else now
        lst = [t for t in self.fails.get(key, []) if now - t < self.window_s] + [now]
        self.fails[key] = lst
        self.all_fails.append(now)
        if len(lst) >= self.max_fails:
            self.blocked[key] = now + self.block_s
            self.fails[key] = []
        if len(self.fails) > 1000:   # forget the oldest keys
            for k in sorted(self.fails, key=lambda k: self.fails[k][-1] if self.fails[k] else 0)[:500]:
                self.fails.pop(k, None)

    def ok(self, key: str):
        self.fails.pop(key, None)


# ---------------------------------------------------------------- who's asking
PROXY_HEADERS = ("X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "Forwarded",
                 "X-Real-IP", "CF-Connecting-IP", "CF-Ray", "Tailscale-User-Login")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}
_TAILNET = ipaddress.ip_network("100.64.0.0/10")


def _ip(addr):
    try:
        ip = ipaddress.ip_address(str(addr or "").strip().strip("[]").split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def is_loopback(addr) -> bool:
    ip = _ip(addr)
    return bool(ip and ip.is_loopback)


def is_tailnet(addr) -> bool:
    ip = _ip(addr)
    return bool(ip and ip.version == 4 and ip in _TAILNET)


def host_name(host: str) -> str:
    host = (host or "").strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def proxied(peer, headers) -> bool:
    """A request relayed by a reverse proxy on this machine (or pretending to be)."""
    if not is_loopback(peer):
        return False
    if any(h in headers for h in PROXY_HEADERS):
        return True
    # the kiosk always asks for localhost / 127.0.0.1; a proxy passes its own name on
    return host_name(headers.get("Host", "")) not in LOCAL_HOSTS


def is_kiosk(peer, headers) -> bool:
    """This machine's own browser (or walterctl): unrestricted."""
    return is_loopback(peer) and not proxied(peer, headers)


def client_ip(peer, headers) -> str:
    """The real client address. Forwarding headers are believed only from
    loopback (a proxy on this machine), never from the network."""
    if is_loopback(peer) and proxied(peer, headers):
        for h in ("CF-Connecting-IP", "X-Real-IP"):
            if _ip(headers.get(h)):
                return str(_ip(headers.get(h)))
        xff = [p.strip() for p in (headers.get("X-Forwarded-For") or "").split(",") if p.strip()]
        if xff and _ip(xff[-1]):
            return str(_ip(xff[-1]))   # the hop our proxy added (left ones can be forged)
        return "proxy"
    return str(_ip(peer) or peer or "unknown")


# ---------------------------------------------------------------- app version
APP_DIR = Path(__file__).resolve().parent.parent / "app"


def read_version(app_dir: Path | None = None) -> str:
    try:
        return ((app_dir or APP_DIR) / "VERSION").read_text().strip() or "0"
    except OSError:
        return "0"


def bump_version(v: str) -> str:
    """1.4.2 -> 1.4.3 ; 7 -> 8 ; anything else -> v + '.1'."""
    parts = v.strip().split(".")
    if parts and all(p.isdigit() for p in parts):
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    return (v.strip() or "1.0") + ".1"
