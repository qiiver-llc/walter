"""Cluster: several full Walter installs in one house acting as ONE brain (docs/CLUSTER.md).

Faces (walter/faces.py) are light clients of one brain. A cluster is several *full* installs
(each with its own Claude, ears and voice) that share state, elect a lead, split heavy jobs and
take over from each other, so a face never loses its brain.

With no peers (the normal case) nothing here does anything: role "solo", no network traffic,
every other module behaves exactly as before.

Pieces (the pure ones are unit-tested with fake clocks and fake peers):

  ClusterStore   ~/.walter/cluster.json (mode 600, never in git): this node's identity, its peers
                 and the pairwise HMAC keys, the lead's pairing code (hashed) and the election term.
  sign/Verifier  every peer request carries an HMAC-SHA256 over method + path + timestamp + nonce +
                 sha256(body) with the key shared by that pair of nodes; 60 s replay window plus a
                 nonce cache. Nothing on /api/cluster/* answers an unsigned request (except /join,
                 which needs the single-use 6-digit code).
  Election       heartbeat every 3 s; a lead is kept while it is alive (sticky); after 3 missed
                 heartbeats the live nodes pick the highest priority, then the lowest id, under a
                 higher term. A higher term always wins, so an old lead that comes back follows.
  SharedState    the replicated files (memory.md, backlog.md, the list, reminders/routines, faces,
                 workspaces, non-walled workspace notes): the lead keeps versions + hashes; followers
                 pull every 15 s (and when the lead pokes them) and push their own local edits to
                 the lead, which applies last-writer-wins and keeps a copy of whatever lost.
  pick_node      job placement by free capacity (cores minus load, free RAM, free worker slots).
  ClusterNode    the async side inside the running brain: loops, HTTP client, job dispatch.
  add_routes     /api/cluster/* (aiohttp). LAN / tailnet only, never through the public tunnel
                 (server.gate_ok), and only whitelisted operations: heartbeat, snapshot, write,
                 notify, job run/status (jobs only when signed by the current lead), leave,
                 transcribe/speak. Peers can never run arbitrary commands.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import hmac
import importlib.util
import json
import logging
import os
import re
import secrets
import shutil
import time
import urllib.parse
from pathlib import Path

from .devices import RateLimiter, clean_name, data_dir, token_hash

log = logging.getLogger("walter.cluster")

PAIR_TTL_S = 300
PAIR_MAX_TRIES = 5
KEY_BYTES = 32
MAX_PEERS = 8
REPLAY_WINDOW_S = 60
ID_RE = re.compile(r"^n-[0-9a-f]{12}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{40,64}$")
JOB_ID_RE = re.compile(r"^n-[0-9a-f]{12}:R\d{1,6}$")
MAX_FILE = 1024 * 1024            # a shared file bigger than this isn't replicated (logged)
MAX_BODY = 6 * 1024 * 1024
MAX_PROMPT = 40_000
REMOVED_KEEP_S = 7 * 86400
HEADERS = ("X-Walter-Node", "X-Walter-Time", "X-Walter-Nonce", "X-Walter-Sig")


def cluster_paths(path: str) -> bool:
    return path == "/api/cluster" or path.startswith("/api/cluster/")


def new_id() -> str:
    return "n-" + secrets.token_hex(6)


def valid_id(x) -> bool:
    return isinstance(x, str) and bool(ID_RE.match(x))


def clean_url(u) -> str:
    """http(s)://host[:port] only (no user, path, query); "" when it isn't one."""
    try:
        p = urllib.parse.urlsplit(str(u or "").strip())
    except ValueError:
        return ""
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        return ""
    if p.path not in ("", "/") or p.query or p.fragment:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]{1,120}", p.netloc):
        return ""
    try:
        p.port
    except ValueError:
        return ""
    return f"{p.scheme}://{p.netloc}"


def clamp_priority(v) -> int:
    try:
        return max(0, min(100, int(v)))
    except (TypeError, ValueError):
        return 50


def pair_key(a: str, b: str) -> str:
    return "|".join(sorted((a, b)))


# ---------------------------------------------------------------- signing
def body_hash(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def sign(key: str, method: str, path: str, ts: str, nonce: str, body: bytes) -> str:
    msg = "\n".join((method.upper(), path, ts, nonce, body_hash(body))).encode()
    return hmac.new(key.encode(), msg, hashlib.sha256).hexdigest()


def signed_headers(node_id: str, key: str, method: str, path: str, body: bytes = b"",
                   now: float | None = None) -> dict:
    ts = str(int(time.time() if now is None else now))
    nonce = secrets.token_hex(12)
    return {"X-Walter-Node": node_id, "X-Walter-Time": ts, "X-Walter-Nonce": nonce,
            "X-Walter-Sig": sign(key, method, path, ts, nonce, body)}


class Verifier:
    """Checks a signed peer request: known sender, fresh timestamp (60 s either way), unseen
    nonce, constant-time signature compare. Raises PermissionError with a short reason."""

    def __init__(self, window_s: float = REPLAY_WINDOW_S, clock=time.time):
        self.window_s, self.clock = window_s, clock
        self.seen: dict[str, float] = {}

    def verify(self, headers, method: str, path: str, body: bytes, key_for) -> str:
        node = str(headers.get("X-Walter-Node") or "")
        ts = str(headers.get("X-Walter-Time") or "")
        nonce = str(headers.get("X-Walter-Nonce") or "")
        sig = str(headers.get("X-Walter-Sig") or "")
        if not valid_id(node) or not re.fullmatch(r"\d{1,12}", ts) or not re.fullmatch(r"[0-9a-f]{16,64}", nonce) \
                or not re.fullmatch(r"[0-9a-f]{64}", sig):
            raise PermissionError("unsigned or malformed request")
        now = self.clock()
        if abs(now - int(ts)) > self.window_s:
            raise PermissionError("stale request (check the clocks)")
        key = key_for(node)
        if not key:
            raise PermissionError("unknown node")
        if not hmac.compare_digest(sign(key, method, path, ts, nonce, body), sig):
            raise PermissionError("bad signature")
        tag = node + ":" + nonce
        if tag in self.seen:
            raise PermissionError("replayed request")
        self.seen[tag] = now
        if len(self.seen) > 5000:
            self.seen = {k: t for k, t in self.seen.items() if now - t <= 2 * self.window_s}
        return node


# ---------------------------------------------------------------- registry
class ClusterStore:
    """~/.walter/cluster.json (mode 600). HMAC keys have to be readable to sign with, so they
    live here in plain form; they are never logged, printed or replicated. The pairing code is
    stored only as its SHA-256."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else data_dir() / "cluster.json"

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_name(self.path.name + ".lock"), "a") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                d = self._read()
                yield d
                self._write(d)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _read(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
            if not isinstance(d, dict):
                raise ValueError
        except (OSError, ValueError):
            d = {}
        d.setdefault("node", None)
        for k in ("peers", "pair_keys", "removed", "walled"):
            if not isinstance(d.get(k), dict):
                d[k] = {}
        d.setdefault("pairing", None)
        d.setdefault("term", 0)
        d.setdefault("leader", None)
        return d

    def _write(self, d: dict):
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(d, f, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def exists(self) -> bool:
        return self.path.exists()

    def read(self) -> dict:
        return self._read()

    # ------------------------------------------------------------ identity
    def peek_identity(self) -> dict | None:
        """This node, or None when it has never been in a cluster (nothing is written)."""
        n = self._read()["node"]
        return dict(n) if isinstance(n, dict) and valid_id(n.get("id")) else None

    def identity(self, name: str = "", priority: int | None = None, node_id: str | None = None) -> dict:
        """This node ({id, name, priority}); written on first use (pairing or joining)."""
        n = self.peek_identity()
        if n:
            return n
        with self._locked() as d:
            if not (d["node"] and valid_id(d["node"].get("id"))):
                d["node"] = {"id": node_id if valid_id(node_id) else new_id(), "name": clean_name(name) or "Walter",
                             "priority": clamp_priority(50 if priority is None else priority),
                             "created": time.time()}
            return dict(d["node"])

    def set_identity(self, **kw) -> dict:
        self.identity()
        with self._locked() as d:
            if "name" in kw:
                n = clean_name(kw["name"])
                if not n:
                    raise ValueError("the name is empty")
                d["node"]["name"] = n
            if "priority" in kw:
                d["node"]["priority"] = clamp_priority(kw["priority"])
            return dict(d["node"])

    # ------------------------------------------------------------ peers
    def peers(self) -> dict[str, dict]:
        return {k: v for k, v in self._read()["peers"].items() if valid_id(k) and isinstance(v, dict)}

    def public_peers(self) -> list[dict]:
        return [{k: v for k, v in p.items() if k != "key"} for p in self.peers().values()]

    def has_peers(self) -> bool:
        return bool(self.peers())

    def key_for(self, node_id: str) -> str | None:
        p = self.peers().get(node_id)
        k = p and p.get("key")
        return k if isinstance(k, str) and KEY_RE.match(k) else None

    def find(self, ref) -> dict | None:
        ref = str(ref or "").strip().lower()
        if not ref:
            return None
        me = self.peek_identity() or {"id": "", "name": ""}
        if ref in (me["id"], me["name"].lower(), "me", "self") and me["id"]:
            return {"id": me["id"], "name": me["name"], "priority": me["priority"], "self": True}
        for p in self.peers().values():
            if ref in (p["id"], str(p.get("name", "")).lower()):
                return {k: v for k, v in p.items() if k != "key"}
        return None

    def add_peer(self, peer: dict, key: str | None = None):
        if not valid_id(peer.get("id")):
            raise ValueError("bad node id")
        me = self.identity()
        if peer["id"] == me["id"]:
            return
        with self._locked() as d:
            old = d["peers"].get(peer["id"]) or {}
            if not old and len(d["peers"]) >= MAX_PEERS:
                raise ValueError("too many installs in this cluster")
            p = {"id": peer["id"], "name": clean_name(peer.get("name")) or old.get("name") or "Walter",
                 "url": clean_url(peer.get("url")) or old.get("url", ""),
                 "priority": clamp_priority(peer.get("priority", old.get("priority", 50))),
                 "added": old.get("added") or time.time(), "key": old.get("key")}
            if key is not None:
                if not KEY_RE.match(key):
                    raise ValueError("bad key")
                p["key"] = key
            d["peers"][peer["id"]] = p
            d["removed"].pop(peer["id"], None)

    def update_peer(self, node_id: str, **kw):
        """Name / url / priority as a peer reports them in its heartbeat (validated)."""
        p = self.peers().get(node_id)
        if not p:
            return
        new = {"name": clean_name(kw.get("name")) or p.get("name"),
               "url": clean_url(kw.get("url")) or p.get("url"),
               "priority": clamp_priority(kw.get("priority", p.get("priority")))}
        if all(p.get(k) == v for k, v in new.items()):
            return
        with self._locked() as d:
            if node_id in d["peers"]:
                d["peers"][node_id].update(new)

    def remove_peer(self, node_id: str, tombstone: bool = True) -> bool:
        with self._locked() as d:
            gone = d["peers"].pop(node_id, None) is not None
            d["pair_keys"] = {k: v for k, v in d["pair_keys"].items() if node_id not in k.split("|")}
            d["walled"].pop(node_id, None)
            if tombstone:
                d["removed"][node_id] = time.time()
            now = time.time()
            d["removed"] = {k: t for k, t in d["removed"].items() if now - t < REMOVED_KEEP_S}
            if d.get("leader") == node_id:
                d["leader"] = None
            return gone

    def leave(self):
        """Forget every peer and key (this node goes back to solo); identity is kept."""
        with self._locked() as d:
            d["peers"], d["pair_keys"], d["walled"], d["pairing"] = {}, {}, {}, None
            d["leader"] = None

    def removed(self) -> dict[str, float]:
        return dict(self._read()["removed"])

    # ------------------------------------------------------------ keys the lead hands out
    def ensure_pair_key(self, a: str, b: str) -> str:
        with self._locked() as d:
            k = d["pair_keys"].get(pair_key(a, b))
            if not k:
                k = d["pair_keys"][pair_key(a, b)] = secrets.token_urlsafe(KEY_BYTES)
            return k

    def pair_key_for(self, a: str, b: str) -> str | None:
        return self._read()["pair_keys"].get(pair_key(a, b))

    # ------------------------------------------------------------ walled workspaces per node
    def walled_for(self, node_id: str) -> list[str]:
        v = self._read()["walled"].get(node_id)
        return [str(x) for x in v] if isinstance(v, list) else []

    def all_walled(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self._read()["walled"].items() if isinstance(v, list)}

    def set_walled(self, node_id: str, ws_ids: list[str]):
        with self._locked() as d:
            clean = sorted({re.sub(r"[^a-z0-9-]", "", str(w).lower())[:40] for w in ws_ids} - {""})
            if clean:
                d["walled"][node_id] = clean
            else:
                d["walled"].pop(node_id, None)

    def replace_walled(self, mapping: dict):
        if not isinstance(mapping, dict):
            return
        clean = {k: sorted({re.sub(r"[^a-z0-9-]", "", str(w).lower())[:40] for w in v} - {""})
                 for k, v in mapping.items() if valid_id(k) and isinstance(v, list)}
        if clean != self.all_walled():
            with self._locked() as d:
                d["walled"] = {k: v for k, v in clean.items() if v}

    # ------------------------------------------------------------ pairing (on the lead)
    def start_pairing(self, now: float | None = None, ttl: float = PAIR_TTL_S) -> dict:
        now = time.time() if now is None else now
        self.identity()
        code = f"{secrets.randbelow(1_000_000):06d}"
        with self._locked() as d:
            d["pairing"] = {"code_sha256": token_hash(code), "expires": now + ttl, "tries": 0}
        return {"code": code, "expires": now + ttl}

    def cancel_pairing(self):
        with self._locked() as d:
            d["pairing"] = None

    def use_code(self, code: str, now: float | None = None):
        """Burn the code if right; PermissionError if wrong / expired / none open."""
        now = time.time() if now is None else now
        code = re.sub(r"\D", "", str(code or ""))
        problem = None
        with self._locked() as d:   # (written back even when the code is wrong: tries count)
            p = d.get("pairing")
            if not p or p.get("expires", 0) <= now or p.get("tries", 0) >= PAIR_MAX_TRIES:
                problem = "no cluster pairing code is open: run 'walterctl cluster pair' on the lead"
            elif len(code) != 6 or not hmac.compare_digest(token_hash(code), p.get("code_sha256", "")):
                p["tries"] = p.get("tries", 0) + 1
                if p["tries"] >= PAIR_MAX_TRIES:
                    d["pairing"] = None
                problem = "wrong code"
            else:
                d["pairing"] = None   # single use
        if problem:
            raise PermissionError(problem)

    # ------------------------------------------------------------ election state
    def save_term(self, term: int, leader: str | None):
        d = self._read()
        if d.get("term") == term and d.get("leader") == leader:
            return
        with self._locked() as d:
            d["term"], d["leader"] = int(term), leader


# ---------------------------------------------------------------- election
class Election:
    """Pure leader election (no I/O; the clock is passed in).

    * Heartbeats carry (term, leader). A higher term always wins; in the same term two
      different leaders are settled by rank (higher priority, then lower id).
    * A lead stays lead while it is alive (sticky: no flapping when a higher-priority node
      comes back). An old lead that returns sees the higher term and follows.
    * When the leader has missed `miss` heartbeats (and after a start-up grace of the same
      length, so a returning node listens first), a node bumps the term and names the best
      live node (itself included). Every node computes the same answer from the same view.
    * No quorum: during a network split each side may have its own lead for a while; when the
      split heals the higher term wins (see SharedState for what happens to writes)."""

    def __init__(self, me: str, priority: int = 50, hb_s: float = 3.0, miss: int = 3,
                 term: int = 0, now: float = 0.0):
        self.me, self.priority = me, clamp_priority(priority)
        self.hb_s, self.miss = float(hb_s), int(miss)
        self.term, self.leader = int(term or 0), None
        self.started = now
        self.seen: dict[str, dict] = {}

    @property
    def timeout(self) -> float:
        return self.hb_s * self.miss

    def prio(self, node: str) -> int:
        return self.priority if node == self.me else int((self.seen.get(node) or {}).get("priority", 50))

    def rank(self, node: str):
        return (-self.prio(node), node)

    def live(self, now: float) -> list[str]:
        return sorted(n for n, s in self.seen.items() if now - s["at"] < self.timeout)

    def is_live(self, node: str, now: float) -> bool:
        return node == self.me or node in self.live(now)

    def observe(self, sender: str, hb: dict, now: float):
        try:
            term = int(hb.get("term") or 0)
        except (TypeError, ValueError):
            term = 0
        leader = hb.get("leader") if valid_id(hb.get("leader")) or hb.get("leader") == self.me else None
        self.seen[sender] = {"at": now, "priority": clamp_priority(hb.get("priority", 50)),
                             "term": term, "leader": leader}
        if term > self.term:
            self.term, self.leader = term, leader
        elif term == self.term and leader and leader != self.leader:
            if self.leader is None or self.rank(leader) < self.rank(self.leader):
                self.leader = leader

    def forget(self, node: str):
        self.seen.pop(node, None)
        if self.leader == node:
            self.leader = None

    def promote(self, node: str):
        self.term += 1
        self.leader = node

    def tick(self, now: float, has_peers: bool = True) -> str:
        """"solo" | "lead" | "follower" (a follower may briefly have no known lead)."""
        if not has_peers:
            return "solo"
        if self.leader == self.me:
            return "lead"
        if self.leader and self.is_live(self.leader, now):
            return "follower"
        if now - self.started < self.timeout:
            return "follower"   # just started: listen before claiming anything
        best = min([self.me] + self.live(now), key=self.rank)
        self.term += 1
        self.leader = best
        return "lead" if best == self.me else "follower"


# ---------------------------------------------------------------- placement
def capacity(cfg=None, running: int = 0) -> dict:
    """What this node can take on (sent in every heartbeat)."""
    cores = os.cpu_count() or 1
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = 0.0
    free_mb = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                free_mb = int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    stt, tts = speech_caps()
    max_tasks = int(getattr(getattr(cfg, "brain", None), "max_tasks", 3) or 3)
    jobs = bool(getattr(getattr(cfg, "cluster", None), "jobs", True))
    return {"cores": cores, "load1": round(load1, 2), "mem_free_mb": free_mb, "max_tasks": max_tasks,
            "running": int(running), "stt": stt, "tts": tts, "jobs": jobs}


_SPEECH: tuple | None = None


def speech_caps() -> tuple[bool, bool]:
    """(has local speech-to-text, has local text-to-speech), checked once."""
    global _SPEECH
    if _SPEECH is None:
        stt = importlib.util.find_spec("faster_whisper") is not None
        tts = bool(shutil.which("piper")) or importlib.util.find_spec("piper") is not None
        _SPEECH = (stt, tts)
    return _SPEECH


def pick_node(nodes: list[dict], workspace: str | None = None, walled: bool = False,
              need: str | None = None, exclude: tuple = ()) -> str | None:
    """Best node for a job. `nodes`: [{"id", "caps", "walled": [ws ids]}] (live ones only).
    Needs a free worker slot and jobs on; a walled workspace only goes to nodes it is assigned
    to; `need` = "stt" / "tts" for speech. Most free CPU (cores - load) wins, then free RAM,
    then the lower id (deterministic)."""
    best, best_key = None, None
    for n in nodes:
        c = n.get("caps") or {}
        if n.get("id") in exclude or not c.get("jobs", True):
            continue
        if need in ("stt", "tts"):
            if not c.get(need):
                continue
        elif int(c.get("max_tasks", 0)) - int(c.get("running", 0)) <= 0:
            continue
        if walled and workspace not in (n.get("walled") or []) and not n.get("self"):
            continue
        cores = max(1, int(c.get("cores", 1)))
        free_cpu = cores - float(c.get("load1", 0))
        key = (-round(free_cpu, 1), -int(c.get("mem_free_mb", 0)) // 256, str(n.get("id")))
        if best_key is None or key < best_key:
            best, best_key = n.get("id"), key
    return best


# ---------------------------------------------------------------- shared state
SHARED_HOME = {"home/memory.md": "memory.md", "home/backlog.md": "backlog.md"}
SHARED_DATA = {"data/dashboard.json": "dashboard.json", "data/reminders.json": "reminders.json",
               "data/faces.json": "faces.json", "data/workspaces.json": "workspaces.json"}
WS_FILES = ("notes.md", "backlog.md")
SHARED_KEY_RE = re.compile(r"^(home/(memory|backlog)\.md|data/(dashboard|reminders|faces|workspaces)\.json"
                           r"|ws/[a-z0-9][a-z0-9-]{0,39}/(notes|backlog)\.md)$")


def sha(b: bytes | None) -> str | None:
    return None if b is None else hashlib.sha256(b).hexdigest()


class SharedState:
    """The replicated files. Keys are logical ("home/memory.md", "ws/acme/notes.md"); each node
    maps them onto its OWN folders, so installs with different users/paths still line up. A key
    from a peer is only ever looked up in this map, never used as a path.

    Lead: ledger {key: {v, sha, at, by}} in ~/.walter/cluster-state.json; v goes up whenever the
    file's hash changes (its own edits are noticed on each refresh).
    Follower: synced {key: sha} = the lead's copy it last took. A local edit (hash != synced) is
    pushed to the lead before pulling; anything the lead has that differs is pulled.
    Conflicts (someone else changed it since you last synced) are last-writer-wins on the lead,
    and the losing copy is saved in ~/.walter/cluster-conflicts/ and logged."""

    def __init__(self, me: str, data: Path, home: Path, state_path: Path | None = None,
                 conflicts: Path | None = None, workspaces=None, clock=time.time):
        self.me, self.data, self.home = me, Path(data), Path(home)
        self.state_path = Path(state_path) if state_path else self.data / "cluster-state.json"
        self.conflicts = Path(conflicts) if conflicts else self.data / "cluster-conflicts"
        self.workspaces = workspaces      # () -> [{"id", "privacy"}]
        self.clock = clock
        self.state = self._load()

    # ------------------------------------------------------------ persistence
    def _load(self) -> dict:
        try:
            d = json.loads(self.state_path.read_text())
            if not isinstance(d, dict):
                raise ValueError
        except (OSError, ValueError):
            d = {}
        for k in ("ledger", "synced"):
            if not isinstance(d.get(k), dict):
                d[k] = {}
        d.setdefault("stepped_down", False)
        return d

    def save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.state_path)

    # ------------------------------------------------------------ which files
    def _ws_list(self) -> list[dict]:
        try:
            return list(self.workspaces() if self.workspaces else [])
        except Exception:
            log.exception("cluster: couldn't read workspaces")
            return []

    def walled_ids(self) -> set[str]:
        return {w["id"] for w in self._ws_list() if w.get("privacy") == "walled"}

    def files(self, walled_ok=()) -> dict[str, Path]:
        """key -> this node's path. Walled workspaces only when listed in `walled_ok`."""
        out = {k: self.home / v for k, v in SHARED_HOME.items()}
        out.update({k: self.data / v for k, v in SHARED_DATA.items()})
        for w in self._ws_list():
            wid = str(w.get("id") or "")
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", wid):
                continue
            if w.get("privacy") == "walled" and wid not in set(walled_ok or ()):
                continue
            for name in WS_FILES:
                out[f"ws/{wid}/{name}"] = self.home / "workspaces" / wid / name
        return out

    def allowed(self, key: str, walled_ok=()) -> bool:
        """Is this key shareable with a node assigned `walled_ok`? (unknown workspaces: no
        if they might be walled; a ws key for a workspace we don't know yet is allowed only
        when it isn't walled here)."""
        if not isinstance(key, str) or not SHARED_KEY_RE.match(key):
            return False
        if key.startswith("ws/"):
            wid = key.split("/")[1]
            if wid in self.walled_ids() and wid not in set(walled_ok or ()):
                return False
        return True

    def path(self, key: str) -> Path | None:
        if not SHARED_KEY_RE.match(key or ""):
            return None
        if key in SHARED_HOME:
            return self.home / SHARED_HOME[key]
        if key in SHARED_DATA:
            return self.data / SHARED_DATA[key]
        _, wid, name = key.split("/")
        return self.home / "workspaces" / wid / name

    @staticmethod
    def read(p: Path) -> bytes | None:
        try:
            if p.stat().st_size > MAX_FILE:
                log.warning("cluster: %s is over %d KB, not replicated", p.name, MAX_FILE // 1024)
                return None
            return p.read_bytes()
        except OSError:
            return None

    def write_local(self, key: str, content: bytes | None):
        p = self.path(key)
        if p is None:
            raise ValueError("bad key")
        if content is None:
            return   # deletions are not replicated (a missing file just isn't sent)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".cluster-tmp")
        mode = 0o600 if key.startswith("data/") else 0o644
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, p)

    def _conflict(self, key: str, content: bytes | None, why: str, by: str = "") -> dict:
        ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
        rec = {"at": self.clock(), "key": key, "why": why, "by": by, "copy": None}
        if content is not None:
            self.conflicts.mkdir(parents=True, exist_ok=True)
            os.chmod(self.conflicts, 0o700)
            name = f"{ts}-{secrets.token_hex(3)}-{key.replace('/', '_')}"
            dest = self.conflicts / name
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(content)
            rec["copy"] = str(dest)
        with open(self.conflicts.with_name("cluster-conflicts.log"), "a") as f:
            f.write(json.dumps(rec) + "\n")
        log.warning("cluster conflict on %s (%s): the other copy is kept in %s", key, why, rec["copy"])
        return rec

    def conflict_log(self, n: int = 20) -> list[dict]:
        try:
            lines = self.conflicts.with_name("cluster-conflicts.log").read_text().splitlines()
        except OSError:
            return []
        out = []
        for line in lines[-n:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out

    # ------------------------------------------------------------ lead side
    def refresh(self) -> list[str]:
        """Notice this node's own edits: bump the version of every file whose hash changed."""
        changed = []
        led = self.state["ledger"]
        for key, p in self.files(walled_ok=self.walled_ids()).items():
            h = sha(self.read(p))
            cur = led.get(key)
            if h is None and cur is None:
                continue
            if cur is None or cur.get("sha") != h:
                led[key] = {"v": (cur or {}).get("v", 0) + 1, "sha": h, "at": self.clock(), "by": self.me}
                changed.append(key)
        if changed:
            self.save()
        return changed

    def become_lead(self):
        """Start the ledger from what we last synced (versions keep going up)."""
        led = self.state["ledger"]
        for key, h in self.state["synced"].items():
            if key not in led:
                led[key] = {"v": 1, "sha": h, "at": self.clock(), "by": "sync"}
        self.state["stepped_down"] = False
        self.refresh()
        self.save()

    def step_down(self):
        """We were lead and someone else won: our edits made while we were (wrongly) lead are
        kept as conflict copies on the first pull, never pushed over the new lead's."""
        self.state["synced"] = {k: v.get("sha") for k, v in self.state["ledger"].items() if v.get("sha")}
        self.state["stepped_down"] = True
        self.save()

    def manifest(self, walled_ok=()) -> dict:
        self.refresh()
        return {k: {"v": v["v"], "sha": v["sha"]} for k, v in self.state["ledger"].items()
                if self.allowed(k, walled_ok) and v.get("sha")}

    def snapshot(self, have: dict, walled_ok=()) -> dict:
        """What a follower needs: the manifest, plus content for every file whose hash differs
        from the one it says it has."""
        have = have if isinstance(have, dict) else {}
        files = {}
        for key, m in self.manifest(walled_ok).items():
            ent = dict(m)
            if have.get(key) != m["sha"]:
                c = self.read(self.path(key))
                if c is None or sha(c) != m["sha"]:
                    continue   # changed under us: the next round gets it
                ent["content"] = c.decode("utf-8", "replace")
            files[key] = ent
        return {"files": files, "at": self.clock()}

    def accept_write(self, key: str, base: str | None, content: str, by: str, walled_ok=()) -> dict:
        """A follower's edit. Last writer wins; if the lead's copy changed since the follower
        last saw it (base != current hash), the lead's copy is saved as a conflict first."""
        if not self.allowed(key, walled_ok):
            raise PermissionError("that file isn't shared with this node")
        if not isinstance(content, str) or len(content.encode()) > MAX_FILE:
            raise ValueError("content too big")
        data = content.encode()
        self.refresh()
        led = self.state["ledger"]
        cur = led.get(key) or {"v": 0, "sha": None}
        new = sha(data)
        if new == cur.get("sha"):
            return {"v": cur["v"], "sha": new, "conflict": False}
        conflict = False
        if cur.get("sha") and base != cur["sha"]:
            self._conflict(key, self.read(self.path(key)), "two installs changed it; the later edit won",
                           by=self.me)
            conflict = True
        self.write_local(key, data)
        led[key] = {"v": cur.get("v", 0) + 1, "sha": new, "at": self.clock(), "by": by}
        self.save()
        return {"v": led[key]["v"], "sha": new, "conflict": conflict}

    # ------------------------------------------------------------ follower side
    def have(self) -> dict:
        return dict(self.state["synced"])

    def dirty(self, remote_keys=(), walled_ok=()) -> dict[str, bytes]:
        """Local edits to push to the lead: files changed since we last synced them, and files
        the lead doesn't have at all yet. Never right after we stepped down as lead (those go
        to the conflict folder instead), never walled files this node isn't assigned."""
        if self.state.get("stepped_down"):
            return {}
        out = {}
        synced = self.state["synced"]
        for key, p in self.files(walled_ok=walled_ok).items():
            c = self.read(p)
            if c is None:
                continue
            if key in synced:
                if sha(c) != synced[key]:
                    out[key] = c
            elif key not in set(remote_keys):
                out[key] = c
        return out

    def pushed(self, key: str, h: str):
        self.state["synced"][key] = h
        self.save()

    def apply(self, snap: dict, walled_ok=(), skip=()) -> list[str]:
        """Take the lead's copies. Returns the keys written. A local edit that wasn't pushed
        yet (or one made while we were a lead on the losing side of a split) is kept as a
        conflict copy before it is replaced."""
        written, skip = [], set(skip)
        files = (snap or {}).get("files") or {}
        if not isinstance(files, dict):
            return written
        stepped = bool(self.state.get("stepped_down"))
        for key, ent in files.items():
            if key in skip or not isinstance(ent, dict) or not self.allowed(key, walled_ok):
                continue
            rsha = ent.get("sha")
            if not isinstance(rsha, str):
                continue
            local = self.read(self.path(key))
            lsha = sha(local)
            if lsha == rsha:
                self.state["synced"][key] = rsha
                continue
            content = ent.get("content")
            if not isinstance(content, str):
                continue   # we said we have it but the hash differs: next round
            data = content.encode()
            if sha(data) != rsha:
                continue
            if local is not None and (stepped or lsha != self.state["synced"].get(key)):
                self._conflict(key, local, "replaced by the lead's copy" + (" after a split" if stepped else ""),
                               by=self.me)
            self.write_local(key, data)
            self.state["synced"][key] = rsha
            written.append(key)
        if stepped:
            self.state["stepped_down"] = False
        self.save()
        return written


# ---------------------------------------------------------------- the running node
class ClusterNode:
    """The cluster inside the running brain. Idle (one small file read every few seconds) while
    there are no peers."""

    def __init__(self, cfg, store: ClusterStore | None = None, tasks=None, on_finish=None,
                 workspaces=None, clock=time.time, data: Path | None = None, home: Path | None = None):
        self.cfg = cfg
        c = getattr(cfg, "cluster", None)
        self.hb_s = float(getattr(c, "heartbeat_s", 3.0) or 3.0)
        self.miss = int(getattr(c, "missed", 3) or 3)
        self.sync_s = float(getattr(c, "sync_s", 15.0) or 15.0)
        self.store = store or ClusterStore()
        self.clock = clock
        wall = getattr(getattr(cfg, "faces", None), "wall_name", "Walter")
        prio = clamp_priority(getattr(c, "priority", 50))
        me = self.store.peek_identity()
        if me is None:   # never clustered: an identity in memory only, written when we pair/join
            me = {"id": new_id(), "name": clean_name(getattr(c, "name", "") or wall) or "Walter",
                  "priority": prio, "created": clock()}
        elif me.get("priority") != prio:
            me = self.store.set_identity(priority=prio)   # [cluster] priority is the source
        self.me = me
        d = self.store.read()
        self.election = Election(me["id"], me["priority"], self.hb_s, self.miss, d.get("term", 0), clock())
        if workspaces is None:
            def workspaces():
                from .workspaces import WorkspaceStore
                st = WorkspaceStore()
                return st.all() if st.defined() else []
        data = Path(data) if data else data_dir()
        self.shared = SharedState(me["id"], data, Path(home or cfg.brain.home), workspaces=workspaces,
                                  clock=clock)
        self.verifier = Verifier(clock=clock)
        self.join_limiter = RateLimiter(max_fails=5, window_s=600, block_s=900, global_max=20)
        self.sig_limiter = RateLimiter(max_fails=20, window_s=600, block_s=600, global_max=200)
        self.tasks = tasks
        self.on_finish = on_finish
        self.facehub = None
        self.role = "solo"
        self.caps: dict[str, dict] = {}
        self.jobs = RemoteJobs(data / "cluster-jobs.json")
        self.served: dict[str, int] = self.jobs.served()
        self.last_sync = 0.0
        self.last_sync_ok: float | None = None
        self.sync_now = asyncio.Event()
        self.session = None
        self.tasks_running = 0
        self.url_cache = ""
        self.mdns = None

    # ------------------------------------------------------------ identity / view
    def persist(self) -> dict:
        """Write this node's identity (first pairing / join)."""
        self.me = self.store.identity(self.me["name"], self.me["priority"], node_id=self.me["id"])
        return self.me

    def my_url(self) -> str:
        c = getattr(self.cfg, "cluster", None)
        u = clean_url(getattr(c, "url", "") or "")
        if u:
            return u
        if not self.url_cache:
            try:
                from .dashboard import lan_ip
                ip = lan_ip()
            except Exception:
                ip = ""
            self.url_cache = f"http://{ip or '127.0.0.1'}:{self.cfg.server.port}"
        return self.url_cache

    def my_hb(self) -> dict:
        running = self.running_tasks()
        hb = {"id": self.me["id"], "name": self.me["name"], "url": self.my_url(),
              "priority": self.election.priority, "term": self.election.term,
              "leader": self.election.leader, "role": self.role, "caps": capacity(self.cfg, running),
              "known": sorted(self.store.peers())}
        return hb

    def running_tasks(self) -> int:
        try:
            return int(self.tasks.running) if self.tasks is not None else 0
        except Exception:
            return 0

    def is_follower(self) -> bool:
        return self.role == "follower"

    def may_run_schedules(self) -> bool:
        """Reminders and routines fire on the lead (or a solo install) only."""
        return self.role != "follower"

    def leader_peer(self) -> dict | None:
        lid = self.election.leader
        return self.store.peers().get(lid) if lid and lid != self.me["id"] else None

    def brain_urls(self) -> list[str]:
        """Every brain a face may use, the lead first (in the face hello response)."""
        urls = []
        lead = self.election.leader
        nodes = [{"id": self.me["id"], "url": self.my_url()}] + list(self.store.peers().values())
        nodes.sort(key=lambda n: (n["id"] != lead, n["id"] != self.me["id"], n["id"]))
        for n in nodes:
            u = clean_url(n.get("url"))
            if u and u not in urls:
                urls.append(u)
        return urls

    def lead_url(self) -> str:
        lead = self.election.leader
        if not lead or lead == self.me["id"]:
            return self.my_url()
        return clean_url((self.store.peers().get(lead) or {}).get("url")) or ""

    def status(self) -> dict:
        now = self.clock()
        peers = []
        for p in self.store.public_peers():
            s = self.election.seen.get(p["id"]) or {}
            peers.append({**p, "live": self.election.is_live(p["id"], now),
                          "last_heard": s.get("at"), "caps": self.caps.get(p["id"]),
                          "lead": p["id"] == self.election.leader,
                          "walled": self.store.walled_for(p["id"])})
        return {"me": {**self.me, "url": self.my_url(), "caps": capacity(self.cfg, self.running_tasks())},
                "role": self.role, "term": self.election.term, "leader": self.election.leader,
                "lead_url": self.lead_url(), "peers": peers, "last_sync": self.last_sync_ok,
                "brains": self.brain_urls(), "jobs": self.jobs.items[-10:],
                "conflicts": len(self.shared.conflict_log(1000))}

    # ------------------------------------------------------------ the tick
    def tick(self) -> str:
        """Run the election once; handle role changes. Cheap."""
        now = self.clock()
        d = self.store.read()
        peers = {k: v for k, v in d["peers"].items() if valid_id(k) and isinstance(v, dict)}
        if int(d.get("term") or 0) > self.election.term:   # just joined (walterctl cluster join)
            self.election.term, self.election.leader = int(d["term"]), d.get("leader")
            self.election.started = now
        for nid in list(self.election.seen):
            if nid not in peers:
                self.election.forget(nid)
        before = self.role
        role = self.election.tick(now, bool(peers))
        self.role = role
        if role != before:
            log.info("cluster: %s -> %s (term %d, lead %s)", before, role, self.election.term,
                     self.election.leader)
            if role == "lead":
                self.shared.become_lead()
            elif before == "lead" and role == "follower":
                self.shared.step_down()
        if peers:
            self.store.save_term(self.election.term, self.election.leader)
        return role

    def observe(self, sender: str, hb: dict):
        if not isinstance(hb, dict) or hb.get("id") != sender:
            return
        self.election.observe(sender, hb, self.clock())
        caps = hb.get("caps")
        if isinstance(caps, dict):
            self.caps[sender] = {k: caps.get(k) for k in ("cores", "load1", "mem_free_mb", "max_tasks",
                                                          "running", "stt", "tts", "jobs")}
        self.store.update_peer(sender, name=hb.get("name"), url=hb.get("url"), priority=hb.get("priority"))

    def lead_extras(self, sender: str, known) -> dict:
        """What the lead adds to its heartbeat answer: the member list, keys for members the
        sender doesn't know yet, removals, and the walled-workspace assignments."""
        if self.role != "lead":
            return {}
        known = set(known or []) if isinstance(known, list) else set()
        members = [{"id": self.me["id"], "name": self.me["name"], "url": self.my_url(),
                    "priority": self.election.priority}]
        keys = {}
        for p in self.store.peers().values():
            members.append({k: p.get(k) for k in ("id", "name", "url", "priority")})
            if p["id"] != sender and p["id"] not in known:
                k = self.store.pair_key_for(sender, p["id"])
                if k:
                    keys[p["id"]] = k
        return {"members": members, "keys": keys, "removed": sorted(self.store.removed()),
                "walled": self.store.all_walled()}

    def take_lead_extras(self, sender: str, extras: dict):
        """A follower learning members / keys / removals from the lead's answer."""
        if sender != self.election.leader or not isinstance(extras, dict):
            return
        members = {m["id"]: m for m in extras.get("members") or [] if isinstance(m, dict) and valid_id(m.get("id"))}
        for nid, key in (extras.get("keys") or {}).items():
            if valid_id(nid) and isinstance(key, str) and KEY_RE.match(key) and nid in members:
                try:
                    self.store.add_peer(members[nid], key)
                    log.info("cluster: learned a new member %s", members[nid].get("name"))
                except ValueError:
                    pass
        for nid in extras.get("removed") or []:
            if valid_id(nid) and nid != sender and nid in self.store.peers() and nid not in members:
                self.store.remove_peer(nid)
                self.election.forget(nid)
                log.info("cluster: %s was removed by the lead", nid)
            if nid == self.me["id"]:
                log.warning("cluster: the lead removed this install; leaving")
                self.store.leave()
                return
        self.store.replace_walled(extras.get("walled") or {})

    def walled_ok(self, node_id: str) -> list[str]:
        return self.store.walled_for(node_id)

    # ------------------------------------------------------------ HTTP client
    async def _session(self):
        if self.session is None or self.session.closed:
            import aiohttp
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8))
        return self.session

    async def request(self, peer: dict, method: str, path: str, payload=None, timeout: float = 8.0,
                      raw: bytes | None = None):
        """Signed request to a peer; returns parsed JSON (or bytes for audio). Raises on errors."""
        import aiohttp
        key = self.store.key_for(peer["id"])
        url = clean_url(peer.get("url"))
        if not key or not url:
            raise LookupError(f"no key or address for {peer.get('name') or peer.get('id')}")
        body = raw if raw is not None else (b"" if payload is None else json.dumps(payload).encode())
        headers = signed_headers(self.me["id"], key, method, path, body, self.clock())
        headers["Content-Type"] = "application/octet-stream" if raw is not None else "application/json"
        s = await self._session()
        async with s.request(method, url + path, data=body or None, headers=headers,
                             timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            data = await r.read()
            if r.status >= 400:
                try:
                    msg = json.loads(data).get("error")
                except Exception:
                    msg = data[:200].decode("utf-8", "replace")
                raise RuntimeError(f"{r.status}: {msg}")
            if r.content_type == "application/json":
                return json.loads(data or b"null")
            return data

    # ------------------------------------------------------------ loops
    async def heartbeat_once(self):
        peers = list(self.store.peers().values())
        if not peers:
            self.tick()
            return

        async def one(p):
            try:
                ans = await self.request(p, "POST", "/api/cluster/heartbeat", self.my_hb(), timeout=self.hb_s)
            except Exception as e:
                log.debug("cluster: heartbeat to %s failed: %s", p.get("name"), e)
                return
            if isinstance(ans, dict):
                self.observe(p["id"], ans.get("hb") or {})
                self.take_lead_extras(p["id"], ans.get("lead") or {})
        await asyncio.gather(*(one(p) for p in peers))
        self.tick()

    async def run(self):
        """Heartbeats + election every hb_s; sync every sync_s (or when poked); remote jobs."""
        loops = [asyncio.ensure_future(self._hb_loop()), asyncio.ensure_future(self._sync_loop()),
                 asyncio.ensure_future(self._jobs_loop())]
        try:
            await asyncio.gather(*loops)
        finally:
            for t in loops:
                t.cancel()
            if self.session is not None:
                await self.session.close()

    async def _hb_loop(self):
        while True:
            try:
                await self.heartbeat_once()
            except Exception:
                log.exception("cluster heartbeat failed")
            await asyncio.sleep(self.hb_s)

    async def _sync_loop(self):
        while True:
            try:
                await asyncio.wait_for(self.sync_now.wait(), self.sync_s)
            except asyncio.TimeoutError:
                pass
            self.sync_now.clear()
            try:
                await self.sync_once()
            except Exception as e:
                log.warning("cluster sync failed: %s", e)

    async def _jobs_loop(self):
        while True:
            await asyncio.sleep(5)
            try:
                await self.poll_jobs()
            except Exception:
                log.exception("cluster job poll failed")

    # ------------------------------------------------------------ sync
    async def sync_once(self) -> dict:
        """Follower: push local edits, then pull. Lead: notice own edits, poke followers."""
        if self.role == "lead":
            changed = self.shared.refresh()
            if changed:
                await self.notify_followers()
            self.last_sync_ok = self.clock()
            return {"role": "lead", "changed": changed}
        lead = self.leader_peer()
        if self.role != "follower" or not lead or not self.election.is_live(lead["id"], self.clock()):
            return {"role": self.role, "skipped": "no live lead"}
        mine = self.walled_ok(self.me["id"])
        snap = await self.request(lead, "POST", "/api/cluster/snapshot", {"have": self.shared.have()},
                                  timeout=20)
        files = snap.get("files") if isinstance(snap, dict) and isinstance(snap.get("files"), dict) else {}
        pushed = []
        for key, content in self.shared.dirty(set(files), mine).items():
            try:
                r = await self.request(lead, "POST", "/api/cluster/write",
                                       {"key": key, "base": self.shared.state["synced"].get(key),
                                        "content": content.decode("utf-8", "replace")})
                self.shared.pushed(key, r.get("sha"))
                pushed.append(key)
            except Exception as e:
                log.warning("cluster: couldn't push %s to the lead: %s", key, e)
        written = self.shared.apply(snap, mine, skip=pushed)
        self.after_apply(written)
        self.last_sync = self.last_sync_ok = self.clock()
        return {"role": "follower", "pushed": pushed, "pulled": written}

    def after_apply(self, keys: list[str]):
        if "data/dashboard.json" in keys:
            try:
                from . import dashboard as dash
                dash.ListStore(title=self.cfg.dashboard.list_title).publish()
            except Exception:
                log.exception("cluster: couldn't refresh the list on the face")
        if keys:
            log.info("cluster: pulled %s", ", ".join(keys))

    async def notify_followers(self):
        async def one(p):
            try:
                await self.request(p, "POST", "/api/cluster/notify", {}, timeout=3)
            except Exception:
                pass
        await asyncio.gather(*(one(p) for p in self.store.peers().values()
                               if self.election.is_live(p["id"], self.clock())))

    # ------------------------------------------------------------ jobs
    def candidates(self, include_self: bool = True) -> list[dict]:
        now = self.clock()
        nodes = []
        if include_self:
            nodes.append({"id": self.me["id"], "caps": capacity(self.cfg, self.running_tasks()), "self": True})
        for p in self.store.peers().values():
            if self.election.is_live(p["id"], now) and self.caps.get(p["id"]):
                nodes.append({"id": p["id"], "caps": self.caps[p["id"]], "walled": self.store.walled_for(p["id"])})
        return nodes

    def resolve_target(self, on: str, workspace: str | None, include_self=True) -> str | None:
        walled = bool(workspace and workspace in self.shared.walled_ids())
        if on in ("best", "any", ""):
            return pick_node(self.candidates(include_self), workspace, walled)
        f = self.store.find(on)
        if f is None:
            raise KeyError(f"no install called {on!r} in the cluster (walterctl cluster peers)")
        if walled and not f.get("self") and workspace not in self.store.walled_for(f["id"]):
            raise PermissionError(f"{workspace} is walled and not assigned to {f['name']}")
        return f["id"]

    async def submit(self, title: str, prompt: str, workspace: str | None, on: str = "best",
                     include_self: bool = True) -> dict:
        """`walterctl task --on NAME|best`: run a worker on another install. Placement is decided
        by the lead (a follower asks the lead). Any failure to place it falls back to local."""
        title, prompt = str(title)[:120], str(prompt)[:MAX_PROMPT]
        try:
            if self.role == "follower" and self.leader_peer():
                r = await self.request(self.leader_peer(), "POST", "/api/cluster/place",
                                       {"title": title, "prompt": prompt, "workspace": workspace, "on": on,
                                        "origin": self.me["id"]}, timeout=15)
                if r.get("node") == self.me["id"]:
                    return {"id": r.get("tid"), "title": title, "node": self.me["id"],
                            "node_name": self.me["name"], "note": "the lead picked this install"}
                return self.jobs.add(r["node"], r.get("node_name", ""), r["job"], title, prompt, workspace)
            target = self.resolve_target(on, workspace, include_self)
            if target is None or target == self.me["id"]:
                return await self._local(title, prompt, workspace, note="ran here (best node)")
            job = self.jobs.next_id(self.me["id"])
            peer = self.store.peers()[target]
            await self.request(peer, "POST", "/api/cluster/job",
                               {"job": job, "title": title, "prompt": prompt, "workspace": workspace,
                                "origin": self.me["id"]}, timeout=15)
            return self.jobs.add(target, peer.get("name", ""), job, title, prompt, workspace)
        except (KeyError, PermissionError):
            raise
        except Exception as e:
            log.warning("cluster: couldn't place %r on another install (%s); running it here", title, e)
            return await self._local(title, prompt, workspace, note=f"fell back to this install ({e})")

    async def place(self, title, prompt, workspace, on, origin) -> dict:
        """The lead deciding for a follower: pick the node and start the job there."""
        target = self.resolve_target(on, workspace, include_self=True)
        if target is None:
            target = origin if origin in self.store.peers() else self.me["id"]
        job = self.jobs.next_id(origin)
        if target == self.me["id"]:
            tid = await self.run_job(job, title, prompt, workspace, origin)
            return {"node": self.me["id"], "node_name": self.me["name"], "job": job, "tid": tid}
        peer = self.store.peers()[target]
        r = await self.request(peer, "POST", "/api/cluster/job",
                               {"job": job, "title": title, "prompt": prompt, "workspace": workspace,
                                "origin": origin}, timeout=15)
        return {"node": target, "node_name": peer.get("name", ""), "job": job, "tid": (r or {}).get("tid")}

    async def _local(self, title, prompt, workspace, note=""):
        if self.tasks is None:
            raise RuntimeError("no local workers")
        item = await self.tasks.start(title, prompt, workspace=workspace)
        return {**item, "node": self.me["id"], "node_name": self.me["name"], "note": note}

    async def run_job(self, job: str, title: str, prompt: str, workspace, origin: str) -> int:
        """Run an authorised job here as a normal low-priority worker (walter/tasks.py)."""
        if job in self.served:
            return self.served[job]
        if workspace and workspace in self.shared.walled_ids() and workspace not in self.walled_ok(self.me["id"]):
            raise PermissionError("that workspace is walled and not assigned to this install")
        if self.tasks is None:
            raise RuntimeError("no workers here")
        origin_name = (self.store.peers().get(origin) or {}).get("name") or "another install"
        item = await self.tasks.start(f"[{origin_name}] {title}"[:120], prompt, workspace=workspace,
                                      quiet=origin != self.me["id"])
        self.served[job] = item["id"]
        self.jobs.save_served(self.served)
        return item["id"]

    def job_status(self, job: str) -> dict:
        tid = self.served.get(job)
        if tid is None:
            return {"status": "unknown"}
        item = next((i for i in (self.tasks.items if self.tasks else []) if i.get("id") == tid), None)
        if item is None:
            return {"status": "lost"}
        out = {"status": item["status"], "result": item.get("result"), "started": item.get("started"),
               "finished": item.get("finished"), "activity": ""}
        if item["status"] == "running":
            try:
                from .workerlive import Snapshotter
                w = next((w for w in Snapshotter().snapshot()["workers"] if w.get("id") == tid), None)
                if w:
                    out["activity"] = str(w.get("activity") or "")[:300]
            except Exception:
                pass
        return out

    async def poll_jobs(self):
        for j in [j for j in self.jobs.items if j["status"] == "running"]:
            peer = self.store.peers().get(j["node"])
            st = None
            if peer:
                try:
                    st = await self.request(peer, "GET", f"/api/cluster/job/{j['job']}", timeout=5)
                    j["last_ok"] = self.clock()
                except Exception as e:
                    log.debug("cluster job %s poll failed: %s", j["job"], e)
            if st and st.get("status") in ("done", "failed", "cancelled"):
                j.update(status=st["status"], result=st.get("result"), finished=self.clock())
                self.jobs.save()
                self._finished(j)
            elif st:
                if st.get("activity") != j.get("activity"):
                    j["activity"] = st.get("activity", "")
                    self.jobs.save()
            elif self.clock() - j.get("last_ok", j["started"]) > 180 or not peer:
                j.update(status="lost", finished=self.clock())
                self.jobs.save()
                log.warning("cluster: lost touch with %s running %r; running it here instead",
                            j.get("node_name"), j["title"])
                try:
                    await self._local(j["title"], j.get("prompt", ""), j.get("workspace"))
                except Exception as e:
                    j["result"] = f"lost, and the local fallback failed too ({e})"
                    self._finished(j)

    def _finished(self, j: dict):
        if self.on_finish:
            item = {"id": j["id"], "title": f"{j['title']} (on {j.get('node_name') or j['node']})",
                    "status": j["status"], "result": j.get("result") or "(no report)"}
            try:
                self.on_finish(item)
            except Exception:
                log.exception("cluster job report failed")


class RemoteJobs:
    """Jobs this install sent elsewhere (and which jobs it runs for others), in
    ~/.walter/cluster-jobs.json."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            d = {}
        self.items: list[dict] = d.get("items") if isinstance(d.get("items"), list) else []
        self._served = d.get("served") if isinstance(d.get("served"), dict) else {}
        self.n = int(d.get("n") or 0)

    def served(self) -> dict:
        return dict(self._served)

    def save_served(self, served: dict):
        self._served = dict(list(served.items())[-200:])
        self.save()

    def save(self):
        self.items = self.items[-50:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"items": self.items, "served": self._served, "n": self.n}, f, indent=1)
        os.replace(tmp, self.path)

    def next_id(self, origin: str) -> str:
        return f"{origin}:R{secrets.randbelow(900_000) + 100_000}"

    def add(self, node, node_name, job, title, prompt, workspace) -> dict:
        j = {"id": job.split(":")[1], "job": job, "node": node, "node_name": node_name, "title": title,
             "prompt": prompt, "workspace": workspace, "status": "running", "started": time.time(),
             "finished": None, "result": None}
        self.items.append(j)
        self.save()
        return {k: v for k, v in j.items() if k != "prompt"}


# ---------------------------------------------------------------- routes
def add_routes(app, cfg, node: ClusterNode):
    """/api/cluster/*: signed peer API + /api/cluster/local/* for walterctl on this machine."""
    from aiohttp import web

    from . import devices as D

    routes = web.RouteTableDef()

    async def read_body(request, limit=MAX_BODY) -> bytes:
        raw = await request.content.read(limit + 1)
        if len(raw) > limit:
            raise web.HTTPRequestEntityTooLarge(max_size=limit, actual_size=len(raw))
        return raw

    def err(status, msg):
        return web.json_response({"error": msg}, status=status)

    async def signed(request, limit=MAX_BODY):
        """(sender id, json body) of a signed peer request, or raise 401/403."""
        raw = await read_body(request, limit)
        ip = D.client_ip(request.remote, request.headers)
        if node.sig_limiter.retry_after(ip):
            raise web.HTTPTooManyRequests(text="too many bad requests")
        try:
            sender = node.verifier.verify(request.headers, request.method, request.path_qs, raw,
                                          node.store.key_for)
        except PermissionError as e:
            if str(e) not in ("unknown node", "stale request (check the clocks)"):
                node.sig_limiter.fail(ip)
            raise web.HTTPUnauthorized(text=json.dumps({"error": str(e)}), content_type="application/json")
        node.sig_limiter.ok(ip)
        if request.content_type == "application/octet-stream":
            return sender, raw
        try:
            d = json.loads(raw or b"{}")
        except ValueError:
            raise web.HTTPBadRequest(text="send JSON")
        if not isinstance(d, dict):
            raise web.HTTPBadRequest(text="send a JSON object")
        return sender, d

    def local_only(request):
        if not D.is_kiosk(request.remote, request.headers):
            raise web.HTTPForbidden(text="local only")

    # ---- joining (code, no signature yet)
    @routes.post("/api/cluster/join")
    async def join(request):
        ip = D.client_ip(request.remote, request.headers)
        wait = node.join_limiter.retry_after(ip)
        if wait:
            return err(429, "too many tries; wait a few minutes")
        raw = await read_body(request, 4096)
        try:
            d = json.loads(raw or b"{}")
            assert isinstance(d, dict)
        except (ValueError, AssertionError):
            return err(400, "send a JSON object")
        if node.role == "follower":
            return err(409, f"I'm not the lead; pair with {node.lead_url() or 'the lead'}")
        new = d.get("node") if isinstance(d.get("node"), dict) else {}
        if not valid_id(new.get("id")) or new.get("id") == node.me["id"]:
            return err(400, "bad node id")
        url = clean_url(new.get("url"))
        if not url:
            return err(400, "give the joining install's own address (http://ip:port)")
        try:
            node.store.use_code(str(d.get("code") or ""))
        except PermissionError as e:
            node.join_limiter.fail(ip)
            return err(403, str(e))
        node.join_limiter.ok(ip)
        try:
            existing = list(node.store.peers().values())
            key = secrets.token_urlsafe(KEY_BYTES)
            node.store.add_peer({"id": new["id"], "name": new.get("name"), "url": url,
                                 "priority": new.get("priority", 50)}, key)
        except ValueError as e:
            return err(400, str(e))
        members = []
        for p in existing:
            if p["id"] == new["id"]:
                continue
            members.append({"id": p["id"], "name": p.get("name"), "url": p.get("url"),
                            "priority": p.get("priority", 50),
                            "key": node.store.ensure_pair_key(p["id"], new["id"])})
        if node.role == "solo":
            node.election.promote(node.me["id"])   # the first pairing makes this install the lead
            node.tick()
        log.info("cluster: %s joined from %s", clean_name(new.get("name")), ip)
        return web.json_response({"lead": {"id": node.me["id"], "name": node.me["name"], "url": node.my_url(),
                                           "priority": node.election.priority},
                                  "key": key, "members": members, "term": node.election.term})

    # ---- peer API (signed)
    @routes.post("/api/cluster/heartbeat")
    async def heartbeat(request):
        sender, d = await signed(request, 16384)
        node.observe(sender, d)
        node.tick()
        return web.json_response({"hb": node.my_hb(), "lead": node.lead_extras(sender, d.get("known"))})

    @routes.post("/api/cluster/snapshot")
    async def snapshot(request):
        sender, d = await signed(request, 256 * 1024)
        if node.role != "lead":
            return err(409, "not the lead")
        return web.json_response(node.shared.snapshot(d.get("have") or {}, node.walled_ok(sender)))

    @routes.post("/api/cluster/write")
    async def write(request):
        sender, d = await signed(request)
        if node.role != "lead":
            return err(409, "not the lead")
        try:
            r = node.shared.accept_write(str(d.get("key") or ""), d.get("base"), d.get("content"), sender,
                                         node.walled_ok(sender))
        except PermissionError as e:
            return err(403, str(e))
        except ValueError as e:
            return err(400, str(e))
        asyncio.ensure_future(node.notify_followers())
        return web.json_response(r)

    @routes.post("/api/cluster/notify")
    async def notify(request):
        sender, _ = await signed(request, 1024)
        if sender == node.election.leader:
            node.sync_now.set()
        return web.json_response({"ok": True})

    @routes.post("/api/cluster/place")
    async def place(request):
        sender, d = await signed(request, MAX_PROMPT * 2)
        if node.role != "lead":
            return err(409, "not the lead")
        if not cfg.cluster.jobs:
            return err(403, "jobs are off here")
        try:
            r = await node.place(str(d.get("title") or "")[:120], str(d.get("prompt") or "")[:MAX_PROMPT],
                                 _ws(d.get("workspace")), str(d.get("on") or "best"), sender)
        except (KeyError, PermissionError) as e:
            return err(403, str(e))
        except Exception as e:
            return err(503, str(e))
        return web.json_response(r)

    @routes.post("/api/cluster/job")
    async def job(request):
        """Run a worker for the cluster. Only the current lead may ask (it authorises jobs)."""
        sender, d = await signed(request, MAX_PROMPT * 2)
        if not cfg.cluster.jobs:
            return err(403, "jobs are off on this install")
        if sender != node.election.leader:
            return err(403, "only the lead hands out jobs")
        jid, prompt = str(d.get("job") or ""), d.get("prompt")
        if not JOB_ID_RE.match(jid) or not isinstance(prompt, str) or not prompt.strip():
            return err(400, "bad job")
        origin = d.get("origin") if valid_id(d.get("origin")) else sender
        try:
            tid = await node.run_job(jid, str(d.get("title") or prompt[:60])[:120], prompt[:MAX_PROMPT],
                                     _ws(d.get("workspace")), origin)
        except PermissionError as e:
            return err(403, str(e))
        except RuntimeError as e:
            return err(409, str(e))
        return web.json_response({"ok": True, "tid": tid})

    @routes.get("/api/cluster/job/{job}")
    async def job_status(request):
        await signed(request, 0)
        jid = request.match_info["job"]
        if not JOB_ID_RE.match(jid):
            return err(400, "bad job id")
        return web.json_response(node.job_status(jid))

    @routes.post("/api/cluster/leave")
    async def peer_leave(request):
        sender, _ = await signed(request, 1024)
        if node.role == "lead" or sender == node.election.leader:
            node.store.remove_peer(sender)
            node.election.forget(sender)
        return web.json_response({"ok": True})

    @routes.post("/api/cluster/transcribe")
    async def transcribe(request):
        """Speech offload: 16 kHz mono s16le PCM in, text out (same path as a face's audio turn)."""
        _, raw = await signed(request, 16000 * 2 * 60)
        fh = node.facehub
        if fh is None or not isinstance(raw, (bytes, bytearray)):
            return err(503 if fh is None else 400, "no ears here" if fh is None else "send raw PCM")
        try:
            text = await fh.transcribe(raw)
        except Exception as e:
            return err(503, f"couldn't transcribe ({e})")
        return web.json_response({"text": text})

    @routes.post("/api/cluster/speak")
    async def speak(request):
        _, d = await signed(request, 4096)
        fh = node.facehub
        text = str(d.get("text") or "").strip()[:1000]
        if fh is None or not text:
            return err(400, "nothing to say")
        wav = await fh.synth(text)
        if not wav:
            return err(503, "no voice here")
        return web.Response(body=wav, content_type="audio/wav")

    # ---- this machine only (walterctl)
    @routes.get("/api/cluster/local/status")
    async def local_status(request):
        local_only(request)
        return web.json_response(node.status())

    @routes.post("/api/cluster/local/pair")
    async def local_pair(request):
        local_only(request)
        d = await request.json()
        if node.role == "follower":
            return err(409, f"pair new installs on the lead ({node.lead_url() or 'unknown'})")
        if isinstance(d, dict) and d.get("cancel"):
            node.store.cancel_pairing()
            return web.json_response({"ok": True})
        node.persist()
        p = node.store.start_pairing()
        return web.json_response({**p, "url": node.my_url(), "name": node.me["name"]})

    @routes.post("/api/cluster/local/join")
    async def local_join(request):
        local_only(request)
        d = await request.json()
        if not isinstance(d, dict):
            return err(400, "send {url, code}")
        if node.store.has_peers():
            return err(409, "already in a cluster: walterctl cluster leave first")
        node.persist()
        my_url = clean_url(d.get("my_url")) or node.my_url()
        try:
            lead = await asyncio.get_running_loop().run_in_executor(
                None, lambda: join(str(d.get("url") or ""), str(d.get("code") or ""), node.me["name"],
                                   my_url, node.me["priority"], node.store))
        except (ValueError, PermissionError) as e:
            return err(403 if isinstance(e, PermissionError) else 400, str(e))
        except Exception as e:
            return err(502, f"couldn't reach the lead ({e})")
        node.tick()
        return web.json_response({"lead": lead, "me": node.me})

    @routes.post("/api/cluster/local/sync")
    async def local_sync(request):
        local_only(request)
        try:
            r = await node.sync_once()
        except Exception as e:
            return err(503, str(e))
        return web.json_response(r)

    @routes.post("/api/cluster/local/promote")
    async def local_promote(request):
        local_only(request)
        d = await request.json()
        f = node.store.find(d.get("node"))
        if f is None:
            return err(404, "no such install")
        node.election.promote(f["id"])
        node.tick()
        await node.heartbeat_once()
        return web.json_response({"leader": f["id"], "name": f["name"], "term": node.election.term})

    @routes.post("/api/cluster/local/remove")
    async def local_remove(request):
        local_only(request)
        d = await request.json()
        f = node.store.find(d.get("node"))
        if f is None or f.get("self"):
            return err(404, "no such peer")
        if node.role == "follower":
            return err(409, "remove installs on the lead")
        node.store.remove_peer(f["id"])
        node.election.forget(f["id"])
        return web.json_response({"removed": f["name"]})

    @routes.post("/api/cluster/local/leave")
    async def local_leave(request):
        local_only(request)
        for p in list(node.store.peers().values()):
            try:
                await node.request(p, "POST", "/api/cluster/leave", {}, timeout=3)
            except Exception:
                pass
        node.store.leave()
        node.election = Election(node.me["id"], node.election.priority, node.hb_s, node.miss,
                                 node.election.term, node.clock())
        node.tick()
        return web.json_response({"ok": True, "role": node.role})

    @routes.post("/api/cluster/local/task")
    async def local_task(request):
        local_only(request)
        d = await request.json()
        if not isinstance(d, dict) or not d.get("prompt"):
            return err(400, "prompt required")
        try:
            r = await node.submit(str(d.get("title") or d["prompt"][:60]), str(d["prompt"]),
                                  _ws(d.get("workspace")), str(d.get("on") or "best"))
        except KeyError as e:
            return err(404, str(e.args[0]))
        except PermissionError as e:
            return err(403, str(e))
        except RuntimeError as e:
            return err(409, str(e))
        return web.json_response(r)

    app.add_routes(routes)


def _ws(v) -> str | None:
    s = re.sub(r"[^a-z0-9-]", "", str(v or "").lower())[:40]
    return s or None


# ---------------------------------------------------------------- joining (walterctl, sync)
def join(lead_url: str, code: str, name: str, my_url: str, priority: int = 50,
         store: ClusterStore | None = None, opener=None) -> dict:
    """`walterctl cluster join URL CODE` on the new install: send the code + who we are; store
    the lead and every member with the keys the lead made for us. Returns the lead."""
    import urllib.request
    store = store or ClusterStore()
    lead_url = clean_url(lead_url)
    if not lead_url:
        raise ValueError("give the lead's address like http://192.168.1.20:8765")
    my_url = clean_url(my_url)
    if not my_url:
        raise ValueError("this install's own address is unknown: pass --url http://<this-ip>:8765")
    me = store.identity(name, priority)
    body = json.dumps({"code": re.sub(r"\D", "", str(code)),
                       "node": {"id": me["id"], "name": me["name"], "url": my_url,
                                "priority": me["priority"]}}).encode()
    req = urllib.request.Request(lead_url + "/api/cluster/join", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    op = opener or urllib.request.urlopen
    try:
        with op(req, timeout=10) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except Exception:
            msg = e.reason
        raise PermissionError(msg) from None
    lead = d.get("lead") or {}
    if not valid_id(lead.get("id")) or not KEY_RE.match(str(d.get("key") or "")):
        raise RuntimeError("the lead's answer didn't make sense")
    store.add_peer({**lead, "url": clean_url(lead.get("url")) or lead_url}, d["key"])
    for m in d.get("members") or []:
        if isinstance(m, dict) and valid_id(m.get("id")) and KEY_RE.match(str(m.get("key") or "")):
            store.add_peer(m, m["key"])
    store.save_term(int(d.get("term") or 0), lead["id"])
    return lead


# ---------------------------------------------------------------- mDNS (optional)
def advertise(port: int, node: dict):
    """_walter-cluster._tcp (needs python zeroconf; else None and `--peer URL` / join URL)."""
    try:
        import socket

        from zeroconf import ServiceInfo, Zeroconf

        from .dashboard import lan_ip
    except Exception:
        log.info("cluster mDNS: python zeroconf isn't installed (join with the lead's URL instead)")
        return None
    try:
        ip = lan_ip()
        if not ip:
            return None
        info = ServiceInfo("_walter-cluster._tcp.local.",
                           f"{clean_name(node.get('name')) or 'Walter'}-{node['id'][-4:]}._walter-cluster._tcp.local.",
                           addresses=[socket.inet_aton(ip)], port=int(port),
                           properties={"id": node["id"], "v": "1"})
        zc = Zeroconf()
        zc.register_service(info)
        return (zc, info)
    except Exception as e:
        log.warning("cluster mDNS: couldn't advertise (%s)", e)
        return None


def discover(timeout: float = 3.0) -> list[dict]:
    """Walter installs advertising on this network: [{name, url, id}] ([] without zeroconf)."""
    try:
        import socket

        from zeroconf import ServiceBrowser, Zeroconf
    except Exception:
        return []
    found: dict[str, dict] = {}

    class L:
        def add_service(self, zc, typ, name):
            info = zc.get_service_info(typ, name, 2000)
            if info and info.addresses:
                ip = socket.inet_ntoa(info.addresses[0])
                props = {k.decode(): (v or b"").decode() for k, v in (info.properties or {}).items()}
                found[name] = {"name": name.split("._")[0], "url": f"http://{ip}:{info.port}",
                               "id": props.get("id", "")}

        def update_service(self, *a):
            pass

        def remove_service(self, *a):
            pass

    zc = Zeroconf()
    try:
        ServiceBrowser(zc, "_walter-cluster._tcp.local.", L())
        time.sleep(timeout)
    finally:
        zc.close()
    return list(found.values())
