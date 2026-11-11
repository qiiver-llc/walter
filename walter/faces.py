"""Faces: one Walter brain, many faces (wall, laptop, kitchen tablet...).

A face is a screen (and optionally a mic and speaker) that shares this brain,
its memory, list and calendar. Each face has a name ("Kitchen"), an optional
room, a kind (wall | laptop | tablet | other), a default workspace (read by the
workspaces track) and its own display settings (face/display-<id>.json).

Registry: ~/.walter/faces.json (mode 600, never in git):
  {"faces": [{id, name, room, kind, default_workspace, created, last_seen, last_ip,
              mic_quality, token_sha256, settings}], "pairing": {...}}
The wall itself is face "wall" (no token: it is this machine); its name comes from
[faces] wall_name (default "Walter") unless renamed here.

Pairing reuses the companion-app pattern (devices.py): `walterctl face pair [NAME]`
opens a 6-digit code (QR on the wall), valid a few minutes, single use, burned
after a few wrong tries. The face gets a long random token once; only its SHA-256
is stored. Tokens are never logged or printed.

Also here (pure, no I/O, unit-tested): who a sentence is addressed to ("Hey Kitchen,
...") and the best-mic arbiter that groups the same utterance heard by several faces
within ~1.2 s and picks the face that heard it best.

Stdlib only: imported by the server, walterctl and the face agent.
"""
from __future__ import annotations

import contextlib
import difflib
try:
    import fcntl
except ImportError:   # Windows (a desktop face only reads mic_score/clean_name; no stores)
    fcntl = None
import hmac
import json
import math
import os
import re
import secrets
import time
from pathlib import Path

from .devices import RateLimiter, clean_name, data_dir, token_hash  # noqa: F401 (re-exported)

PAIR_TTL_S = 300
PAIR_MAX_TRIES = 5
MAX_FACES = 20
TOKEN_BYTES = 32
KINDS = ("wall", "laptop", "tablet", "other")
WALL_ID = "wall"
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
ROOM_MAX = 40
WS_MAX = 60
# What a face may set on itself (beyond its display settings): nothing else, ever.
SETTING_KEYS = {"volume": (int, 0, 100), "muted": (bool,), "captions": (bool,), "ears": (bool,)}


def valid_id(face_id) -> bool:
    return isinstance(face_id, str) and bool(ID_RE.match(face_id))


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    return s[:32].strip("-") or "face"


def clean_room(room) -> str:
    return clean_name(room, ROOM_MAX)


def clean_workspace(ws) -> str | None:
    if ws is None:
        return None
    s = clean_name(ws, WS_MAX)
    return None if not s or s.lower() in ("none", "null", "-", "off") else s


def clean_settings(changes: dict) -> dict:
    """Validated subset of SETTING_KEYS; raises ValueError on a bad value or key."""
    if not isinstance(changes, dict):
        raise ValueError("settings must be an object")
    out = {}
    for k, v in changes.items():
        spec = SETTING_KEYS.get(k)
        if spec is None:
            raise ValueError(f"unknown setting {k!r}")
        if spec[0] is bool:
            if not isinstance(v, bool):
                raise ValueError(f"{k} must be true or false")
        else:
            if isinstance(v, bool) or not isinstance(v, int) or not spec[1] <= v <= spec[2]:
                raise ValueError(f"{k} must be a whole number {spec[1]} to {spec[2]}")
        out[k] = v
    return out


def public(face: dict) -> dict:
    return {k: v for k, v in face.items() if k != "token_sha256"}


class FaceStore:
    """~/.walter/faces.json. Read from disk on every call (cached by mtime) so
    walterctl can pair, rename and forget while the server runs."""

    def __init__(self, path: Path | None = None, wall_name: str = "Walter"):
        self.path = Path(path) if path else data_dir() / "faces.json"
        self.wall_name = clean_name(wall_name) or "Walter"
        self._cache: tuple[int, dict] | None = None

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
            return {"faces": [], "pairing": None}
        if self._cache and self._cache[0] == mt:
            return json.loads(json.dumps(self._cache[1]))
        try:
            d = json.loads(self.path.read_text())
            if not isinstance(d, dict):
                raise ValueError
        except (OSError, ValueError):
            d = {}
        if not isinstance(d.get("faces"), list):
            d["faces"] = []
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

    def _wall_default(self) -> dict:
        return {"id": WALL_ID, "name": self.wall_name, "room": "", "kind": "wall",
                "default_workspace": None, "created": 0, "last_seen": 0, "last_ip": "",
                "mic_quality": None, "settings": {}}

    def _all(self, d: dict) -> list[dict]:
        """Every face, the wall first (synthesised when it has never been changed)."""
        faces = [f for f in d["faces"] if isinstance(f, dict) and valid_id(f.get("id"))]
        if not any(f["id"] == WALL_ID for f in faces):
            faces.insert(0, self._wall_default())
        else:
            faces.sort(key=lambda f: f["id"] != WALL_ID)
        return faces

    # ------------------------------------------------------------ pairing
    def start_pairing(self, name: str = "", now: float | None = None, ttl: float = PAIR_TTL_S) -> dict:
        """New 6-digit code (replaces any open one). `name` = suggested face name."""
        now = time.time() if now is None else now
        code = f"{secrets.randbelow(1_000_000):06d}"
        with self._locked():
            d = self._read()
            d["pairing"] = {"code_sha256": token_hash(code), "expires": now + ttl, "tries": 0,
                            "name": clean_name(name)}
            self._write(d)
        return {"code": code, "expires": now + ttl, "name": clean_name(name)}

    def pairing_open(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        p = self._read().get("pairing")
        return bool(p and p.get("expires", 0) > now and p.get("tries", 0) < PAIR_MAX_TRIES)

    def pairing_name(self) -> str:
        p = self._read().get("pairing") or {}
        return p.get("name") or ""

    def cancel_pairing(self):
        with self._locked():
            d = self._read()
            d["pairing"] = None
            self._write(d)

    def pair(self, code: str, name: str = "", kind: str = "other", room: str = "",
             now: float | None = None) -> tuple[dict, str]:
        """Check the code; add the face; return (face, token). The token is shown
        to the caller once and stored only hashed. PermissionError on a wrong or
        expired code, ValueError on bad input."""
        now = time.time() if now is None else now
        kind = kind if kind in KINDS and kind != "wall" else "other"
        code = re.sub(r"\D", "", str(code or ""))
        with self._locked():
            d = self._read()
            p = d.get("pairing")
            if not p or p.get("expires", 0) <= now or p.get("tries", 0) >= PAIR_MAX_TRIES:
                raise PermissionError("no face pairing code is open: run 'walterctl face pair' first")
            if len(code) != 6 or not hmac.compare_digest(token_hash(code), p.get("code_sha256", "")):
                p["tries"] = p.get("tries", 0) + 1
                if p["tries"] >= PAIR_MAX_TRIES:
                    d["pairing"] = None
                self._write(d)
                raise PermissionError("wrong code")
            name = clean_name(name) or p.get("name") or ""
            if not name:
                raise ValueError("give this face a name (Kitchen, Garage, Office...)")
            faces = self._all(d)
            if len(faces) > MAX_FACES:
                raise ValueError("too many faces: forget one first")
            name = self._unique_name(faces, name)
            fid = self._unique_id(faces, slug(name))
            token = secrets.token_urlsafe(TOKEN_BYTES)
            face = {"id": fid, "name": name, "room": clean_room(room), "kind": kind,
                    "default_workspace": None, "created": now, "last_seen": now, "last_ip": "",
                    "mic_quality": None, "token_sha256": token_hash(token), "settings": {}}
            d["faces"] = [f for f in d["faces"] if isinstance(f, dict)] + [face]
            d["pairing"] = None   # single use
            self._write(d)
        return public(face), token

    @staticmethod
    def _unique_name(faces: list[dict], name: str, skip: str = "") -> str:
        taken = {f["name"].lower() for f in faces if f["id"] != skip}
        base, n = name, 2
        while name.lower() in taken:
            name = f"{base[:36]} {n}"
            n += 1
        return name

    @staticmethod
    def _unique_id(faces: list[dict], fid: str) -> str:
        taken = {f["id"] for f in faces} | {WALL_ID}
        base, n = fid, 2
        while fid in taken:
            fid = f"{base[:28]}-{n}"
            n += 1
        return fid

    # ------------------------------------------------------------ lookups
    def check(self, token) -> dict | None:
        """The face this token belongs to, or None. Constant-time compare."""
        if not token or not isinstance(token, str) or len(token) > 200:
            return None
        h = token_hash(token)
        found = None
        for f in self._read()["faces"]:
            if isinstance(f, dict) and hmac.compare_digest(str(f.get("token_sha256", "")), h):
                found = f
        return public(found) if found and found.get("id") != WALL_ID else None

    def list(self) -> list[dict]:
        return [public(f) for f in self._all(self._read())]

    def get(self, ref) -> dict | None:
        """By id or name (case-insensitive); "wall" is always the wall."""
        ref = str(ref or "").strip()
        if not ref:
            return None
        low = ref.lower()
        faces = self._all(self._read())
        for f in faces:
            if f["id"] == low:
                return public(f)
        for f in faces:
            if f["name"].lower() == low:
                return public(f)
        return None

    def ids(self) -> set[str]:
        return {f["id"] for f in self._all(self._read())}

    # ------------------------------------------------------------ changes
    def _update(self, ref, fn) -> dict:
        target = self.get(ref)
        if target is None:
            raise KeyError(f"no face called {ref!r} (walterctl faces)")
        with self._locked():
            d = self._read()
            faces = self._all(d)
            face = next(f for f in faces if f["id"] == target["id"])
            fn(face, faces)
            d["faces"] = faces
            self._write(d)
        return public(face)

    def rename(self, ref, new: str) -> dict:
        new = clean_name(new)
        if not new:
            raise ValueError("the new name is empty")

        def go(face, faces):
            if any(f["name"].lower() == new.lower() and f["id"] != face["id"] for f in faces):
                raise ValueError(f"another face is already called {new}")
            face["name"] = new
        return self._update(ref, go)

    def set_room(self, ref, room: str) -> dict:
        return self._update(ref, lambda f, _: f.__setitem__("room", clean_room(room)))

    def set_kind(self, ref, kind: str) -> dict:
        if kind not in KINDS:
            raise ValueError("kind must be " + ", ".join(KINDS))

        def go(face, _):
            if (face["id"] == WALL_ID) != (kind == "wall"):
                raise ValueError("only the wall itself is kind wall")
            face["kind"] = kind
        return self._update(ref, go)

    def set_workspace(self, ref, ws) -> dict:
        return self._update(ref, lambda f, _: f.__setitem__("default_workspace", clean_workspace(ws)))

    def set_settings(self, ref, changes: dict) -> dict:
        ok = clean_settings(changes)
        return self._update(ref, lambda f, _: f.setdefault("settings", {}).update(ok))

    def forget(self, ref) -> list[str]:
        """Remove a paired face (not the wall). Returns the names removed."""
        ref = str(ref or "").strip()
        with self._locked():
            d = self._read()
            gone = [f for f in d["faces"] if isinstance(f, dict) and f.get("id") != WALL_ID
                    and (ref.lower() == "all" or f.get("id") == ref.lower()
                         or str(f.get("name", "")).lower() == ref.lower())]
            if gone:
                d["faces"] = [f for f in d["faces"] if f not in gone]
                self._write(d)
        return [f["name"] for f in gone]

    def touch(self, face_id: str, ip: str = "", mic_quality: float | None = None,
              now: float | None = None, min_gap: float = 60):
        """Last seen (written at most every `min_gap` s unless the mic score moved)."""
        now = time.time() if now is None else now
        d = self._read()
        f = next((x for x in d["faces"] if isinstance(x, dict) and x.get("id") == face_id), None)
        if f is None or face_id == WALL_ID:
            return
        mq = None if mic_quality is None else round(float(mic_quality), 3)
        if (now - (f.get("last_seen") or 0) < min_gap and f.get("last_ip") == ip
                and (mq is None or mq == f.get("mic_quality"))):
            return
        with self._locked():
            d = self._read()
            for x in d["faces"]:
                if isinstance(x, dict) and x.get("id") == face_id:
                    x["last_seen"], x["last_ip"] = now, ip
                    if mq is not None:
                        x["mic_quality"] = mq
            self._write(d)


# ---------------------------------------------------------------- mic quality
def mic_score(levels, floor_rms: float = 0.0) -> float:
    """0..1: how well a mic heard one utterance. `levels` are the ears' per-frame
    speech levels (rms / 4000, as Ears.on_hear reports them, the same on the wall
    and on face agents), `floor_rms` the room's background rms. Loudness counts
    most, the signal-to-noise ratio the rest."""
    vals = [float(v) for v in (levels or []) if isinstance(v, (int, float)) and math.isfinite(v) and v >= 0]
    if not vals:
        return 0.0
    vals.sort()
    loud = sum(vals[len(vals) // 2:]) / max(1, len(vals) - len(vals) // 2)   # the louder half
    loud = min(1.0, loud)
    rms = loud * 4000
    snr_db = 20 * math.log10(max(rms, 1.0) / max(float(floor_rms or 0), 30.0))
    snr = min(1.0, max(0.0, snr_db / 30.0))
    return round(0.7 * loud + 0.3 * snr, 3)


def parse_score(v) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
        return 0.0
    return max(0.0, min(1.0, float(v)))


# ---------------------------------------------------------------- addressing
GREET = r"(?:hey|hi|hello|ok|okay|yo|oi)"


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", str(text or "").lower())


def similar(a: str, b: str) -> float:
    """0..1 fuzzy match of two transcripts of (maybe) the same utterance."""
    wa, wb = " ".join(words(a)), " ".join(words(b))
    if not wa or not wb:
        return 0.0
    return difflib.SequenceMatcher(None, wa, wb).ratio()


def _name_re(name: str) -> re.Pattern | None:
    w = words(name)
    if not w:
        return None
    body = r"\s+".join(re.escape(x) for x in w)
    # "hey kitchen ..." anywhere; "kitchen, ..." / "kitchen: ..." at the start
    return re.compile(rf"\b{GREET}[\s,]+(?:the\s+)?{body}\b|^\W*(?:the\s+)?{body}\s*[,:!]", re.I)


def name_target(text: str, faces: list[dict], wake_words=("walter",)) -> dict | None:
    """The face a sentence calls by name ("Hey Kitchen, ...", "Garage, ..."), or None.
    A face named like the generic wake word (the wall's default "Walter") is never a
    target: plain "Walter" goes to the best mic. The longest name wins ("kitchen 2")."""
    generic = {w.lower() for w in wake_words}
    best = None
    for f in faces:
        name = str(f.get("name") or "")
        if not name or " ".join(words(name)) in generic:
            continue
        rx = _name_re(name)
        if rx and rx.search(str(text or "")) and (best is None or len(name) > len(best["name"])):
            best = f
    return best


def strip_name(text: str, face: dict) -> str:
    """'Hey Kitchen, set a timer' -> 'set a timer' (for logs; the brain sees the original)."""
    rx = _name_re(str(face.get("name") or ""))
    return rx.sub("", text, count=1).strip(" ,:!") if rx else text


# ---------------------------------------------------------------- best mic wins
class Arbiter:
    """Groups the same utterance heard by several faces (and the wall) within
    `window_s` and fuzzy-matching text; each group is decided once its window
    closes. Pure: the server calls offer() as turns arrive and due() from a timer."""

    def __init__(self, window_s: float = 1.2, threshold: float = 0.6, clock=time.monotonic):
        self.window_s, self.threshold, self.clock = float(window_s), float(threshold), clock
        self.groups: list[dict] = []
        self._n = 0

    def offer(self, face_id: str, text: str, score: float, meta: dict | None = None,
              now: float | None = None) -> tuple[str, int]:
        """("new", group id) when it opens a group (schedule due() after window_s),
        ("joined", gid) when it is a copy of an open one, ("dup", gid) when the same
        face sent it twice (kept: the better score counts)."""
        now = self.clock() if now is None else now
        entry = {"face": face_id, "text": str(text or ""), "score": parse_score(score),
                 "meta": dict(meta or {}), "at": now}
        best, best_sim = None, 0.0
        for g in self.groups:
            if now - g["opened"] > self.window_s + 0.05:
                continue
            sim = max(similar(text, e["text"]) for e in g["entries"])
            if sim >= self.threshold and sim > best_sim:
                best, best_sim = g, sim
        if best is not None:
            same = next((e for e in best["entries"] if e["face"] == face_id), None)
            if same is not None:
                if entry["score"] > same["score"]:
                    best["entries"][best["entries"].index(same)] = entry
                return "dup", best["id"]
            best["entries"].append(entry)
            return "joined", best["id"]
        self._n += 1
        self.groups.append({"id": self._n, "opened": now, "entries": [entry]})
        return "new", self._n

    def due(self, now: float | None = None) -> list[dict]:
        """Groups whose window has closed (removed from the arbiter)."""
        now = self.clock() if now is None else now
        done = [g for g in self.groups if now - g["opened"] >= self.window_s]
        self.groups = [g for g in self.groups if g not in done]
        return done

    def close(self, gid: int) -> dict | None:
        g = next((g for g in self.groups if g["id"] == gid), None)
        if g is not None:
            self.groups.remove(g)
        return g


def decide(group: dict, faces: list[dict], wake_words=("walter",)) -> dict:
    """Who answers a closed group: {"face": id, "text", "score", "named": bool,
    "heard_by": [ids], "entry": the winning entry}. A face called by name wins
    (even if it didn't hear it itself); otherwise the best mic (earliest on a tie).
    The text is the best-scoring transcript."""
    entries = sorted(group["entries"], key=lambda e: (-e["score"], e["at"]))
    best = entries[0]
    heard_by = [e["face"] for e in sorted(group["entries"], key=lambda e: e["at"])]
    named = None
    for e in entries:
        named = name_target(e["text"], faces, wake_words)
        if named:
            break
    if named:
        mine = next((e for e in entries if e["face"] == named["id"]), None)
        return {"face": named["id"], "text": best["text"], "score": (mine or best)["score"],
                "named": True, "heard_by": heard_by, "entry": mine or best}
    return {"face": best["face"], "text": best["text"], "score": best["score"], "named": False,
            "heard_by": heard_by, "entry": best}


def get_face(ref) -> dict | None:
    """Module-level lookup (workspaces.default_workspace_for_face uses it): by id or name."""
    try:
        return FaceStore().get(ref)
    except Exception:
        return None
