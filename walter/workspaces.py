"""Workspaces (owner approved 10 Nov 2026, PLAN-faces.md "Workspaces"): one per project or company,
each with its own repos, notes, backlog, Google account, calendars, goals, usage share and privacy.

  ~/.walter/workspaces.json         the workspaces (seeded on first use with just Home; add projects with walterctl workspace add)
  ~/.walter/workspace-state.json    the current workspace PER FACE ({"faces": {"wall": "acme"}})
  ~/.walter/home/workspaces/<id>/   notes.md + backlog.md for each workspace
  ~/.walter/task-workspaces.json    worker id -> workspace tag (written by `walterctl task`, so tags
                                    work even before the server knows about workspaces)

Privacy: "normal" or "walled". A walled workspace never shares notes, files or worker context with
the others. That is enforced by (1) the brief every worker gets (instructions, NOT a sandbox: a
worker runs with Walter's normal file access) and (2) path_allowed(), which the cockpit, board and
CLI use before they read anything.

When ~/.walter/workspaces.json does not exist, defined() is False and every caller behaves exactly
as before workspaces existed (one backlog, Walter's repos, no tags, no headers).

Faces (walter/faces.py, built in parallel) may carry a per-face `default_workspace`;
default_workspace_for_face() reads it if that module exists and falls back to the store default.
Stdlib only; safe to import from the CLI, the build timer and the server.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import os
import re
import time
from pathlib import Path

HOME = Path.home()
DATA = HOME / ".walter"
WALTER_HOME = DATA / "home"
WALL = "wall"                       # the wall face's id until faces.py gives faces their own
PRIVACY = ("normal", "walled")
KEYS = ("name", "aliases", "repos", "notes_dir", "backlog", "google_account", "calendars", "goals",
        "budget_share", "privacy", "journal", "pipeline")
PIPELINES = ("release", "site", "none", "auto")   # the cockpit's ship pipeline preset (walter/cockpit.py)
LIST_KEYS = ("aliases", "repos", "calendars", "goals")

# The seed. No real emails or names here: the owner sets google_account / calendars themselves
# (walterctl workspace set NAME google_account <email>).
SEED = {
    "version": 1,
    "default": "home",
    "workspaces": [
        {"id": "home", "name": "Home", "aliases": ["home", "house", "family", "personal", "home stuff"],
         "repos": [], "google_account": None, "calendars": [], "goals": ["Keep the household running smoothly"],
         "budget_share": 1.0, "privacy": "normal"},
    ],
}
# A new install starts with just Home; the owner adds projects with `walterctl workspace add NAME`
# (and `workspace set NAME privacy walled` for one that must never mix with the others).

NOTES_TEMPLATE = "# {name} notes\n\n(Workspace notes: people, decisions, context for this workspace only.)\n"
BACKLOG_TEMPLATE = ("# {name} backlog\nStatuses: [ready] [ask] [in progress #N] [done YYYY-MM-DD HH:MM]\n"
                    "Format: - [status] **Short title**: details.\n\n")


def slug(s) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")


def norm(s) -> str:
    """'Acme Corp' / 'acme corp' / 'ACME-corp' -> 'acme corp' (for matching)."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).split())


def expand(p) -> Path:
    return Path(os.path.expanduser(str(p)))


def _read_json(p: Path, default):
    try:
        v = json.loads(Path(p).read_text())
        return v if isinstance(v, type(default)) else default
    except (OSError, ValueError):
        return default


def _write_json(p: Path, d) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    os.replace(tmp, p)


class WorkspaceStore:
    DEFAULT_PATH: Path | None = None
    DEFAULT_STATE: Path | None = None
    DEFAULT_NOTES_ROOT: Path | None = None
    DEFAULT_TAGS: Path | None = None
    DEFAULT_FACE_OUT: Path | None = None   # face/dashboard-workspace.json (tests point it elsewhere)

    def __init__(self, path=None, state_path=None, notes_root=None, tags_path=None):
        data = Path(os.environ.get("WALTER_DATA") or DATA)
        self.path = Path(path or self.DEFAULT_PATH or data / "workspaces.json")
        self.state_path = Path(state_path or self.DEFAULT_STATE or data / "workspace-state.json")
        self.notes_root = Path(notes_root or self.DEFAULT_NOTES_ROOT or data / "home" / "workspaces")
        self.tags_path = Path(tags_path or self.DEFAULT_TAGS or data / "task-workspaces.json")

    # ------------------------------------------------------------ storage
    def defined(self) -> bool:
        return self.path.exists()

    def _fill(self, w: dict) -> dict:
        w = dict(w)
        w["id"] = slug(w.get("id") or w.get("name")) or "workspace"
        w.setdefault("name", w["id"].replace("-", " ").title())
        for k in LIST_KEYS:
            v = w.get(k)
            w[k] = [str(x) for x in v] if isinstance(v, list) else ([] if v in (None, "") else [str(v)])
        w.setdefault("google_account", None)
        w["privacy"] = w.get("privacy") if w.get("privacy") in PRIVACY else "normal"
        try:
            w["budget_share"] = max(0.0, min(1.0, float(w.get("budget_share", 0) or 0)))
        except (TypeError, ValueError):
            w["budget_share"] = 0.0
        w.setdefault("notes_dir", str(self.notes_root / w["id"]))
        w.setdefault("backlog", str(Path(w["notes_dir"]) / "backlog.md"))
        return w

    def load(self) -> dict:
        """{"default", "workspaces": [...]}: the file, or the seed (not written) when there is none."""
        d = _read_json(self.path, {}) if self.defined() else copy.deepcopy(SEED)
        ws = [self._fill(w) for w in d.get("workspaces") or [] if isinstance(w, dict)]
        if not ws:
            ws = [self._fill(w) for w in SEED["workspaces"]]
        ids = [w["id"] for w in ws]
        default = d.get("default") if d.get("default") in ids else ids[0]
        return {"version": d.get("version", 1), "default": default, "workspaces": ws}

    def save(self, d: dict) -> None:
        _write_json(self.path, {"version": d.get("version", 1), "default": d["default"],
                                "workspaces": d["workspaces"]})

    @contextlib.contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.path) + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            d = self.load()
            yield d
            self.save(d)

    def ensure(self) -> dict:
        """Write the seed if there's no file yet, and make every workspace's notes folder."""
        if not self.defined():
            self.save(self.load())
        d = self.load()
        for w in d["workspaces"]:
            self.ensure_folder(w)
        return d

    def ensure_folder(self, w: dict) -> None:
        nd = expand(w["notes_dir"])
        try:
            nd.mkdir(parents=True, exist_ok=True)
            if w["privacy"] == "walled":
                os.chmod(nd, 0o700)
            notes = nd / "notes.md"
            if not notes.exists():
                notes.write_text(NOTES_TEMPLATE.format(name=w["name"]))
            bl = expand(w["backlog"])
            if not bl.exists() and nd in bl.parents:
                bl.write_text(BACKLOG_TEMPLATE.format(name=w["name"]))
        except OSError:
            pass

    # ------------------------------------------------------------ lookups
    def all(self) -> list[dict]:
        return self.load()["workspaces"]

    def ids(self) -> list[str]:
        return [w["id"] for w in self.all()]

    def get(self, ident) -> dict | None:
        """By id, name or alias (case / punctuation / spaces ignored)."""
        n = norm(ident)
        if not n:
            return None
        ws = self.all()
        for w in ws:
            if n == norm(w["id"]) or n == norm(w["name"]):
                return w
        for w in ws:
            if n in (norm(a) for a in w["aliases"]):
                return w
        n2 = re.sub(r"^(?:the|my|our)\s+", "", n)
        n2 = re.sub(r"\s+(?:workspace|project|company|stuff|side|account)$", "", n2)
        if n2 != n:
            return self.get(n2)
        return None

    def default_id(self) -> str:
        return self.load()["default"]

    # ------------------------------------------------------------ changes
    def add(self, name: str, **fields) -> dict:
        wid = slug(fields.pop("id", None) or name)
        if not wid:
            raise ValueError("a workspace needs a name")
        with self._locked() as d:
            if any(w["id"] == wid for w in d["workspaces"]):
                raise ValueError(f"workspace '{wid}' already exists")
            w = self._fill({"id": wid, "name": name.strip(), "aliases": [norm(name)], **fields})
            d["workspaces"].append(w)
        self.ensure_folder(w)
        return w

    def set(self, ident, key: str, value) -> dict:
        key = key.strip().lower().replace("-", "_")
        if key == "budget":
            key = "budget_share"
        if key not in KEYS and key != "default":
            raise ValueError(f"unknown key '{key}' (one of: {', '.join(KEYS)}, default)")
        with self._locked() as d:
            w = next((x for x in d["workspaces"] if x["id"] == (self.get(ident) or {}).get("id")), None)
            if w is None:
                raise KeyError(f"no workspace '{ident}'")
            if key == "default":
                d["default"] = w["id"]
                return w
            if key in LIST_KEYS:
                if isinstance(value, str):
                    v = value.strip()
                    if v.startswith("+"):
                        value = w[key] + [v[1:].strip()]
                    elif v.startswith("-"):
                        value = [x for x in w[key] if x != v[1:].strip()]
                    else:
                        value = [x.strip() for x in v.split(",") if x.strip()]
            elif key == "privacy":
                if value not in PRIVACY:
                    raise ValueError("privacy is 'normal' or 'walled'")
            elif key == "budget_share":
                v = str(value).strip()
                f = float(v.rstrip("%"))
                value = f / 100 if v.endswith("%") or f > 1 else f
            elif key == "pipeline":
                value = str(value).strip().lower()
                if value not in PIPELINES:
                    raise ValueError("pipeline is one of: " + ", ".join(PIPELINES))
            elif key == "google_account" and str(value).lower() in ("", "none", "null", "-"):
                value = None
            w[key] = value
            w.update(self._fill(w))
            return dict(w)

    # ------------------------------------------------------------ per-face current workspace
    def _state(self) -> dict:
        st = _read_json(self.state_path, {})
        st.setdefault("faces", {})
        return st

    def current_id(self, face_id: str = WALL) -> str:
        st = self._state()
        wid = (st["faces"].get(face_id) or {}).get("workspace") if isinstance(st["faces"].get(face_id), dict) \
            else st["faces"].get(face_id)
        if wid and self.get(wid):
            return self.get(wid)["id"]
        return default_workspace_for_face(face_id, self)

    def current(self, face_id: str = WALL) -> dict:
        return self.get(self.current_id(face_id)) or self.all()[0]

    def switch(self, ident, face_id: str = WALL) -> dict:
        w = self.get(ident)
        if w is None:
            raise KeyError(f"no workspace '{ident}'")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(str(self.state_path) + ".lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            st = self._state()
            st["faces"][face_id] = {"workspace": w["id"], "since": time.time()}
            _write_json(self.state_path, st)
        self.publish(face_id)
        return w

    def publish(self, face_id: str = WALL, out: Path | None = None) -> dict:
        """face/dashboard-workspace.json: {id, name, walled} for the face's chip (best effort)."""
        if not self.defined():
            return {}
        w = self.current(face_id)
        d = {"updated": time.time(), "face": face_id, "id": w["id"], "name": w["name"],
             "walled": w["privacy"] == "walled"}
        try:
            from walter.dashboard import FACE_DIR, write_face_json
            write_face_json(Path(out or self.DEFAULT_FACE_OUT or FACE_DIR / "dashboard-workspace.json"), d)
        except Exception:
            pass
        return d

    # ------------------------------------------------------------ worker tags
    def tags(self) -> dict:
        return _read_json(self.tags_path, {})

    def tag_task(self, tid, wid: str) -> None:
        t = self.tags()
        t[str(tid)] = wid
        if len(t) > 300:   # keep the newest few hundred
            t = dict(sorted(t.items(), key=lambda kv: int(kv[0]) if kv[0].isdigit() else 0)[-200:])
        _write_json(self.tags_path, t)

    def task_workspace(self, task: dict) -> str | None:
        """A worker's workspace: its own record's tag, else the sidecar tag, else None (untagged)."""
        w = task.get("workspace") if isinstance(task, dict) else None
        return w or self.tags().get(str((task or {}).get("id")))

    def task_visible(self, task: dict, wid: str) -> bool:
        """Show this worker in workspace `wid`? Tagged: only its own. Untagged (older workers, from
        before workspaces): shown everywhere except in walled workspaces."""
        tag = self.task_workspace(task)
        if tag:
            return tag == wid
        w = self.get(wid)
        return not (w and w["privacy"] == "walled")

    # ------------------------------------------------------------ privacy
    def roots(self, w: dict) -> list[Path]:
        """Folders/files that belong to a workspace: notes folder, backlog, journal, repos."""
        out = [expand(w["notes_dir"]), expand(w["backlog"])]
        out += [expand(p) for p in w["repos"]]
        if w.get("journal"):
            out.append(expand(w["journal"]))
        return out

    def owner_of(self, path) -> dict | None:
        """The workspace whose folder holds `path` (the deepest match), or None."""
        p = _resolve(path)
        best, depth = None, -1
        for w in self.all():
            for r in self.roots(w):
                r = _resolve(r)
                if p == r or r in p.parents:
                    if len(r.parts) > depth or (len(r.parts) == depth and w["privacy"] == "walled"):
                        best, depth = w, len(r.parts)
        return best

    def path_allowed(self, path, wid: str) -> bool:
        """May something working in workspace `wid` read `path`? No when the path belongs to ANOTHER
        workspace and either side is walled. Paths no workspace owns are allowed."""
        p = _resolve(path)
        claims = [w for w in self.all() if any(p == r or r in p.parents for r in map(_resolve, self.roots(w)))]
        me = self.get(wid)
        mine = any(w["id"] == wid for w in claims)
        others = [w for w in claims if w["id"] != wid]
        if any(w["privacy"] == "walled" for w in others):
            return False      # another walled workspace claims it (even if this one lists it too)
        if me and me["privacy"] == "walled" and others and not mine:
            return False      # a walled workspace stays out of everyone else's folders
        return True

    def off_limits(self, wid: str) -> list[Path]:
        """Roots a worker in `wid` must not touch (for the brief)."""
        me = self.get(wid)
        out = []
        for w in self.all():
            if w["id"] == wid:
                continue
            if w["privacy"] == "walled" or (me and me["privacy"] == "walled"):
                out += [r for r in self.roots(w) if not (me and _shared(r, self.roots(me)))]
        seen, uniq = set(), []
        for p in out:
            if any(o != p and o in p.parents for o in out):
                continue   # already covered by its folder
            if str(p) not in seen:
                seen.add(str(p))
                uniq.append(p)
        return uniq

    # ------------------------------------------------------------ worker brief
    def brief(self, wid: str) -> str:
        """Lines prepended to a worker's instructions: where it works and what it must not read."""
        w = self.get(wid)
        if w is None:
            return ""
        lines = [f"[Workspace: {w['name']} (id {w['id']}). Work only on this workspace."]
        lines.append("Repos: " + (", ".join(w["repos"]) if w["repos"] else "none") + ".")
        lines.append(f"Workspace notes: {_tilde(w['notes_dir'])}/notes.md; backlog: {_tilde(w['backlog'])}.")
        if w.get("google_account"):
            lines.append(f"Google account for this workspace: {w['google_account']} (use only this one).")
        if w["calendars"]:
            lines.append("Calendars: " + ", ".join(w["calendars"]) + ".")
        off = self.off_limits(wid)
        if w["privacy"] == "walled":
            lines.append("PRIVACY: this workspace is WALLED. Do not read, search or write any other "
                         "workspace's notes, files, repos or journals, and never copy anything from this "
                         "workspace into them, into Walter's shared memory.md/journal, or into commits of "
                         "other repos. Never touch patient or customer data.")
        elif off:
            lines.append("PRIVACY: some workspaces are walled. Do not read, search or open their folders.")
        if off:
            lines.append("Off limits: " + ", ".join(_tilde(p) for p in off) + ".")
        lines.append("(This isolation is by instruction: follow it strictly.)]")
        return "\n".join(lines) + "\n\n"

    def header(self, face_id: str = WALL) -> str:
        """The brain's turn header, e.g. "[workspace: Acme]"; empty when workspaces aren't defined."""
        if not self.defined():
            return ""
        w = self.current(face_id)
        return f"[workspace: {w['name']}" + (" (walled)" if w["privacy"] == "walled" else "") + "]"

    # ------------------------------------------------------------ budget
    def budget_shares(self) -> dict[str, float]:
        """Normalised usage shares (sum 1.0; equal split if none are set)."""
        ws = self.all()
        tot = sum(w["budget_share"] for w in ws)
        if tot <= 0:
            return {w["id"]: round(1 / len(ws), 3) for w in ws}
        return {w["id"]: round(w["budget_share"] / tot, 3) for w in ws}

    def pick_next(self, recent_tasks: list[dict], candidates: list[str] | None = None) -> str:
        """Which workspace is most behind its budget share, judged by the recent workers' tags."""
        shares = self.budget_shares()
        cands = [c for c in (candidates or list(shares)) if c in shares]
        counts = {c: 0 for c in shares}
        for t in recent_tasks:
            tag = self.task_workspace(t)
            if tag in counts:
                counts[tag] += 1
        n = sum(counts.values()) or 1
        return min(cands, key=lambda c: (counts[c] / n - shares[c], -shares[c]))


def _resolve(p) -> Path:
    try:
        return expand(p).resolve()
    except OSError:
        return expand(p).absolute()


def _shared(root: Path, mine: list[Path]) -> bool:
    r = _resolve(root)
    return any(r == _resolve(m) for m in mine)


def _tilde(p) -> str:
    s = str(p)
    h = str(HOME)
    return "~" + s[len(h):] if s.startswith(h + "/") or s == h else s


# ---------------------------------------------------------------- faces (walter/faces.py adapter)

def default_workspace_for_face(face_id: str = WALL, store: WorkspaceStore | None = None) -> str:
    """The face's default workspace from walter/faces.py (field default_workspace) if that module
    exists and knows the face; else the store default ("home"). Never raises."""
    store = store or WorkspaceStore()
    wid = None
    try:
        from walter import faces  # built by the faces track; may not exist yet
        face = None
        for name in ("get_face", "get", "face", "lookup"):
            fn = getattr(faces, name, None)
            if callable(fn):
                try:
                    face = fn(face_id)
                except Exception:
                    face = None
                if face:
                    break
        if not face:
            for name in ("load_faces", "load", "list_faces", "all_faces"):
                fn = getattr(faces, name, None)
                if callable(fn):
                    try:
                        got = fn()
                    except Exception:
                        continue
                    face = _find_face(got, face_id)
                    if face:
                        break
        if not face:
            for cls_name in ("FaceStore", "FaceRegistry", "Registry", "Faces"):
                cls = getattr(faces, cls_name, None)
                if cls is None:
                    continue
                try:
                    reg = cls()
                    for m in ("get", "get_face", "face"):
                        if hasattr(reg, m):
                            face = getattr(reg, m)(face_id)
                            if face:
                                break
                    if not face:
                        for m in ("all", "list", "faces", "load"):
                            if hasattr(reg, m):
                                v = getattr(reg, m)
                                face = _find_face(v() if callable(v) else v, face_id)
                                if face:
                                    break
                except Exception:
                    face = None
                if face:
                    break
        if face:
            wid = face.get("default_workspace") if isinstance(face, dict) else getattr(face, "default_workspace", None)
    except Exception:
        wid = None
    if wid:
        try:
            w = store.get(wid)
            if w:
                return w["id"]
        except Exception:
            pass
    try:
        return store.default_id()
    except Exception:
        return "home"


def _find_face(got, face_id):
    if isinstance(got, dict):
        if face_id in got:
            return got[face_id]
        got = got.get("faces", got)
        if isinstance(got, dict):
            return got.get(face_id)
    if isinstance(got, (list, tuple)):
        for f in got:
            fid = f.get("id") if isinstance(f, dict) else getattr(f, "id", None)
            if fid == face_id:
                return f
    return None


# ---------------------------------------------------------------- voice

_SWITCH = re.compile(r"(?:(?:can you|could you|please|let's|lets|ok|okay|now)\s+)*"
                     r"(?:switch|swap|change|move|go|jump|flip)(?:\s+(?:over|back))?\s+(?:to|into)\s+(?:the\s+)?(.+?)"
                     r"(?:\s+(?:workspace|project|mode))?"
                     r"|(?:(?:let's|lets|i want to|i'm going to|i'll)\s+)?(?:work|working)\s+on\s+(?:the\s+)?(.+?)"
                     r"(?:\s+(?:workspace|project))?(?:\s+(?:now|for a bit|for a while))?"
                     r"|(?:open|load)\s+(?:the\s+)?(.+?)\s+workspace", re.I)
_WHICH = re.compile(r"(?:which|what)\s+workspace(?:\s+(?:am i|are we|are you|is this))?(?:\s+in)?(?:\s+now)?"
                    r"|where\s+(?:am i|are we)(?:\s+working)?|what\s+(?:am i|are we)\s+working\s+in", re.I)
_LIST = re.compile(r"(?:list|what are)\s+(?:my|the|our)\s+workspaces|what\s+workspaces\s+(?:are there|do i have)", re.I)


def voice_match(text: str, store: WorkspaceStore | None = None, strip=None):
    """"switch to acme" -> ("switch", workspace); "which workspace" -> ("which", None);
    "list my workspaces" -> ("list", None); anything else -> None. Only when workspaces are defined
    and the name resolves exactly (so "work on the garage" stays the brain's)."""
    store = store or WorkspaceStore()
    if not store.defined():
        return None
    t = strip(text) if strip else text
    t = re.sub(r"^\W*(?:(?:hey|hi|ok|okay|so)\W+)?walter\W*", "", t.strip(), flags=re.I)
    t = t.strip(" ,.!?").strip()
    if not t or len(t) > 80:
        return None
    if _WHICH.fullmatch(t):
        return ("which", None)
    if _LIST.fullmatch(t):
        return ("list", None)
    m = _SWITCH.fullmatch(t)
    if not m:
        return None
    name = next(g for g in m.groups() if g)
    w = store.get(name)
    return ("switch", w) if w else None


def current_face_id() -> str:
    """Which face this process speaks for: WALTER_FACE env, else the wall."""
    return os.environ.get("WALTER_FACE") or WALL


# ---------------------------------------------------------------- tap a pill (POST /api/workspace/switch)

_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


def switch_request(body, face_id: str = WALL, store: WorkspaceStore | None = None) -> tuple[int, dict]:
    """The cockpit's pill bar: {"id": "acme"} -> (HTTP status, reply). Only exact workspace ids are
    accepted (a whitelist: no names, aliases or paths), and only for this face. Same helper as
    `walterctl workspace switch`."""
    store = store or WorkspaceStore()
    wid = body.get("id") if isinstance(body, dict) else None
    if not isinstance(wid, str) or not _ID.fullmatch(wid):
        return 400, {"error": "id must be a workspace id"}
    if not store.defined():
        return 404, {"error": "no workspaces yet"}
    if wid not in store.ids():
        return 400, {"error": "unknown workspace"}
    w = store.switch(wid, face_id)
    return 200, {"ok": True, "id": w["id"], "name": w["name"], "walled": w["privacy"] == "walled",
                 "face": face_id}


def refresh_build_view() -> None:
    """Rebuild face/dashboard-build.json now (detached, best effort) so the cockpit follows a switch at
    once instead of at the next walter-build.timer run."""
    import subprocess
    import sys
    if os.environ.get("WALTER_DATA"):
        return    # a test or another data folder: never rewrite the real face file from it
    repo = Path(__file__).resolve().parent.parent
    try:
        subprocess.Popen([sys.executable or "python3", "-m", "walter.buildview", "--out",
                          str(repo / "face" / "dashboard-build.json")], cwd=str(repo),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except OSError:
        pass
