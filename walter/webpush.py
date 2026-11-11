"""Web Push for the companion app: reminders and "while you were away" notices
reach a paired phone even when the app is closed.

  * VAPID keys: ~/.walter/secrets/vapid_private.pem (mode 600), made on first use.
  * Subscriptions: ~/.walter/push_subs.json (mode 600), one or more per paired
    device id; a revoked device's subscriptions are dropped, and so is any the
    push service reports gone (404/410).
  * Sending uses pywebpush (requirements.txt). Without it, or without any
    subscription, send() quietly does nothing: the away feed and in-app toasts
    still work.

Browsers only allow push subscriptions on https (or localhost), so on the wall's
plain-http LAN address this degrades to the feed + toasts; with
[app] public_url = "https://..." (Tailscale) it works on phones.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

log = logging.getLogger("walter.webpush")

MAX_SUBS = 20
TTL_S = 6 * 3600   # a reminder older than this isn't worth delivering


def data_dir() -> Path:
    return Path(os.environ.get("WALTER_DATA", Path.home() / ".walter"))


def available() -> bool:
    try:
        import pywebpush  # noqa: F401
        return True
    except Exception:
        return False


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class WebPush:
    DEFAULT_BASE: Path | None = None   # tests: never the real ~/.walter

    def __init__(self, base: Path | None = None, subject: str = "", sender=None):
        base = Path(base) if base else (self.DEFAULT_BASE or data_dir())
        self.key_path = base / "secrets" / "vapid_private.pem"
        self.subs_path = base / "push_subs.json"
        self.subject = subject or "mailto:walter@localhost"
        self._sender = sender            # tests inject fn(sub, payload) -> status code
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="webpush")
        self.stats = {"sent": 0, "failed": 0, "gone": 0}

    # ------------------------------------------------------------ keys
    def _private_key(self):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        with self._lock:
            if self.key_path.exists():
                return serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
            key = ec.generate_private_key(ec.SECP256R1())
            pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption())
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.key_path.parent, 0o700)
            tmp = self.key_path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(pem)
            os.replace(tmp, self.key_path)
            log.info("web push: made new VAPID keys")
            return key

    def public_key(self) -> str:
        """applicationServerKey for the browser: base64url of the raw P-256 point."""
        from cryptography.hazmat.primitives import serialization
        pub = self._private_key().public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        return _b64url(pub)

    # ------------------------------------------------------------ subscriptions
    def _read(self) -> list[dict]:
        try:
            d = json.loads(self.subs_path.read_text())
            return d if isinstance(d, list) else []
        except (OSError, ValueError):
            return []

    def _write(self, subs: list[dict]):
        self.subs_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.subs_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(subs, f, indent=1)
        os.replace(tmp, self.subs_path)

    @staticmethod
    def valid(sub) -> bool:
        if not isinstance(sub, dict):
            return False
        ep, keys = sub.get("endpoint"), sub.get("keys")
        return (isinstance(ep, str) and ep.startswith("https://") and len(ep) < 1000
                and isinstance(keys, dict) and isinstance(keys.get("p256dh"), str)
                and isinstance(keys.get("auth"), str) and len(keys["p256dh"]) < 200 and len(keys["auth"]) < 100)

    def subscribe(self, device_id: str, sub: dict) -> None:
        if not self.valid(sub):
            raise ValueError("not a push subscription")
        clean = {"endpoint": sub["endpoint"], "keys": {"p256dh": sub["keys"]["p256dh"], "auth": sub["keys"]["auth"]}}
        with self._lock:
            subs = [s for s in self._read() if s["sub"]["endpoint"] != clean["endpoint"]]
            subs.append({"device": device_id, "sub": clean, "created": round(time.time())})
            self._write(subs[-MAX_SUBS:])

    def unsubscribe(self, device_id: str, endpoint: str | None = None) -> int:
        with self._lock:
            subs = self._read()
            keep = [s for s in subs if not (s["device"] == device_id and (endpoint is None or s["sub"]["endpoint"] == endpoint))]
            self._write(keep)
            return len(subs) - len(keep)

    def prune(self, device_ids) -> int:
        """Drop subscriptions of devices that are no longer paired."""
        ids = set(device_ids)
        with self._lock:
            subs = self._read()
            keep = [s for s in subs if s["device"] in ids]
            if len(keep) != len(subs):
                self._write(keep)
            return len(subs) - len(keep)

    def count(self, device_id: str | None = None) -> int:
        return sum(1 for s in self._read() if device_id is None or s["device"] == device_id)

    # ------------------------------------------------------------ sending
    def _send_one(self, sub: dict, payload: str) -> int:
        if self._sender is not None:
            return self._sender(sub, payload)
        from pywebpush import WebPushException, webpush
        from py_vapid import Vapid02
        vapid = Vapid02.from_pem(self.key_path.read_bytes())
        try:
            r = webpush(sub, payload, vapid_private_key=vapid, vapid_claims={"sub": self.subject},
                        ttl=TTL_S, timeout=10, headers={"Urgency": "high"})
            return getattr(r, "status_code", 201)
        except WebPushException as e:
            return getattr(e.response, "status_code", 0) or 0

    def send_now(self, title: str, body: str = "", tag: str = "", url: str = "/app/") -> int:
        """Send to every subscription (blocking); returns how many were delivered."""
        subs = self._read()
        if not subs or (self._sender is None and not available()):
            return 0
        self._private_key()  # make sure keys exist
        payload = json.dumps({"title": str(title)[:80], "body": str(body)[:300], "tag": str(tag)[:40],
                              "url": url, "t": time.time()})
        ok, gone = 0, []
        for s in subs:
            try:
                code = self._send_one(s["sub"], payload)
            except Exception as e:  # network trouble: try again next time
                log.warning("web push failed: %s", e)
                code = 0
            if code in (200, 201, 202):
                ok += 1
            elif code in (404, 410):
                gone.append(s["sub"]["endpoint"])
            else:
                self.stats["failed"] += 1
                log.warning("web push: push service said %s", code)
        if gone:
            with self._lock:
                self._write([s for s in self._read() if s["sub"]["endpoint"] not in gone])
            self.stats["gone"] += len(gone)
            log.info("web push: dropped %d expired subscription(s)", len(gone))
        self.stats["sent"] += ok
        return ok

    def send(self, title: str, body: str = "", tag: str = "", url: str = "/app/"):
        """Fire and forget, off the event loop."""
        if not self.subs_path.exists():
            return None
        return self._pool.submit(self.send_now, title, body, tag, url)
