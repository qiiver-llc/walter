"""Cloud workers: background jobs that add-ons run somewhere else (first: the Teak add-on's cloud boxes),
shown and reported like Walter's own workers.

An add-on opts in with a `[workers]` section in its addon.toml (docs/ADDONS.md):

    [workers]
    list = "bin/run-worker.py list --json"        # JSON array of jobs (below)
    fetch = "bin/run-worker.py fetch {id} --json"  # optional: {"report": "...", ...} for one finished job

A job: {id, title, state: queued|running|done|failed|stopped, started, workspace, step, box}.

While such an add-on is ON:
- its list runs at most every POLL_S seconds (cache: ~/.walter/cloud-workers.json, shared by every process
  that asks; a file lock keeps two processes from running it at once), with a LIST_TIMEOUT-second limit,
  never on the voice loop. Bad output is logged and dropped, never fatal; everything it says is untrusted
  data (titles cleaned and capped, ids checked, unknown states skipped).
- the jobs appear in `walterctl tasks` (+ --live/--json), face/dashboard-workers.json (the Workers card,
  with a cloud mark) and the app's workers list, tagged with their workspace.
- when a job reaches done/failed/stopped, Walter (the running service, `Watcher`) runs its fetch once and
  hands the report over as an ordinary "[background task finished ...]" turn, deferred like any other
  report. Delivered jobs are remembered in ~/.walter/cloud-workers-state.json so a restart doesn't repeat
  them. The first time an add-on is seen, jobs that had already ended are recorded without a report.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import fcntl
import json
import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("walter.cloudworkers")

POLL_S = 60              # at most one list per add-on per minute
LIST_TIMEOUT = 20        # seconds for one list
FETCH_TIMEOUT = 300      # one fetch (copies results back; runs in a thread)
FETCH_TRIES = 3          # then the report is delivered without the fetched text
OUT_CAP = 1024 * 1024    # bytes of a script's stdout we read
MAX_JOBS = 50            # per add-on
REPORT_CAP = 6000
STATES = ("queued", "running", "done", "failed", "stopped")
TERMINAL = ("done", "failed", "stopped")
ALIASES = {"pending": "queued", "waiting": "queued", "starting": "running", "working": "running",
           "complete": "done", "completed": "done", "finished": "done", "success": "done", "succeeded": "done",
           "error": "failed", "errored": "failed", "lost": "failed", "missing": "failed",
           "cancelled": "stopped", "canceled": "stopped", "killed": "stopped"}
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
WS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_CTRL = re.compile(r"[\x00-\x1f\x7f‪-‮⁦-⁩]")


def data_dir() -> Path:
    return Path(os.environ.get("WALTER_DATA") or Path.home() / ".walter")


def _clean(s, n: int) -> str:
    """One line of add-on output for a title/step: no control characters, no tags, capped."""
    s = _CTRL.sub(" ", str(s if s is not None else ""))
    s = re.sub(r"[<>]", "", s)
    return re.sub(r"\s+", " ", s).strip()[:n]


def _when(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if 0 < v < 1e11 else None
    if isinstance(v, str) and v.strip():
        try:
            return dt.datetime.fromisoformat(v.strip().replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def normalize(raw, aid: str, name: str) -> list[dict]:
    """The add-on's list output -> clean job rows. Anything odd is dropped, never raised."""
    if isinstance(raw, dict) and isinstance(raw.get("jobs"), list):
        raw = raw["jobs"]
    if not isinstance(raw, list):
        raise ValueError("the list command didn't print a JSON array")
    out, seen = [], set()
    for r in raw[:MAX_JOBS * 4]:
        if not isinstance(r, dict):
            continue
        jid = r.get("id")
        if not isinstance(jid, (str, int)) or isinstance(jid, bool) or not JOB_ID_RE.match(str(jid)):
            continue
        jid = str(jid)
        st = str(r.get("state") or r.get("status") or "").strip().lower()
        st = ALIASES.get(st, st)
        if st not in STATES or jid in seen:
            continue
        seen.add(jid)
        ws = r.get("workspace")
        out.append({"key": f"{aid}:{jid}", "job": jid, "addon": aid, "addon_name": name, "cloud": True,
                    "title": _clean(r.get("title") or jid, 120) or jid, "state": st,
                    "started": _when(r.get("started")), "finished": _when(r.get("finished")),
                    "workspace": ws if isinstance(ws, str) and WS_RE.match(ws) else None,
                    "step": _clean(r.get("step"), 160), "box": _clean(r.get("box"), 60) or None})
    return out[-MAX_JOBS:] if len(out) > MAX_JOBS else out


def _read(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def _write(p: Path, d) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(d, indent=1))
    os.replace(tmp, p)


class CloudWorkers:
    def __init__(self, data: Path | None = None, store=None, clock: Callable[[], float] = time.time):
        self.data = Path(data or data_dir())
        self.cache_path = self.data / "cloud-workers.json"
        self.state_path = self.data / "cloud-workers-state.json"
        self.lock_path = self.data / ".cloud-workers.lock"
        self._store = store
        self.clock = clock
        self._bg: threading.Thread | None = None

    # ---------------------------------------------------------- which add-ons
    @property
    def store(self):
        if self._store is None:
            from . import addons
            self._store = addons.Addons(self.data)
        return self._store

    def sources(self) -> list[dict]:
        """Installed, valid, ON add-ons with a [workers] section."""
        try:
            st = self.store.state()
            return [m for m in self.store.all_installed()
                    if m.get("workers") and not m.get("broken") and self.store.is_on(m["id"], st)]
        except Exception:  # noqa: BLE001
            log.exception("cloud workers: couldn't read the add-ons")
            return []

    def looks(self) -> dict[str, dict]:
        """{add-on id: {color, shape, icon_url?}} for the ON add-ons that declare a [workers.look]
        (walter/addonlook.py; icons are sanitized data: URIs). Missing = the default cloud-blue diamond."""
        out = {}
        for m in self.sources():
            try:
                lk = self.store.look(m)
            except Exception:  # noqa: BLE001  (a bad look never breaks the workers list)
                lk = None
            if lk:
                out[m["id"]] = lk
        return out

    def _run(self, m: dict, which: str, job: str | None, timeout: float):
        argv, env, cwd = self.store.workers_argv(m["id"], which, job)
        p = subprocess.run(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=timeout)
        if p.returncode != 0:
            err = p.stderr.decode("utf-8", "replace").strip().splitlines()
            raise RuntimeError(f"exit {p.returncode}: {_clean(err[-1] if err else 'no message', 200)}")
        return json.loads(p.stdout[:OUT_CAP].decode("utf-8", "replace") or "null")

    # ---------------------------------------------------------- the list (cached)
    def cache(self) -> dict:
        c = _read(self.cache_path, {})
        return c if isinstance(c, dict) and isinstance(c.get("addons"), dict) else {"addons": {}}

    def stale(self, c: dict | None = None) -> bool:
        c = c or self.cache()
        now = self.clock()
        on = {m["id"] for m in self.sources()}
        if set(c["addons"]) != on:
            return True
        return any(now - float((c["addons"][a] or {}).get("checked") or 0) >= POLL_S for a in on)

    def refresh(self, force: bool = False) -> dict:
        """Run each ON add-on's list if its cache entry is older than POLL_S (or `force`). Never raises."""
        try:
            self.data.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return self.cache()
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return self.cache()            # someone else is listing right now: use what we have
            c = self.cache()
            now = self.clock()
            srcs = self.sources()
            on = {m["id"] for m in srcs}
            for gone in set(c["addons"]) - on:  # switched off or removed: its jobs go away
                c["addons"].pop(gone, None)
            for m in srcs:
                ent = c["addons"].get(m["id"]) or {}
                if not force and now - float(ent.get("checked") or 0) < POLL_S:
                    continue
                was_ok = bool(ent.get("ok"))
                ent = {"name": m["name"], "jobs": ent.get("jobs") or [], "checked": now, "ok": was_ok}
                prev = {j.get("key"): j for j in ent["jobs"] if isinstance(j, dict)}
                try:
                    jobs = normalize(self._run(m, "list", None, LIST_TIMEOUT), m["id"], m["name"])
                    for j in jobs:              # when it ended, as far as we can tell (for the face's fade-out)
                        p = prev.get(j["key"])
                        if j["state"] not in TERMINAL:
                            continue
                        if p and p.get("state") in TERMINAL:
                            j["seen_end"] = bool(p.get("seen_end"))
                            j["finished"] = j["finished"] or p.get("finished")
                        elif p or ent.get("ok"):   # watched it run, or it appeared since the last good list
                            j["seen_end"] = True
                            j["finished"] = j["finished"] or now
                    ent["jobs"] = jobs
                    ent["ok"] = True
                except subprocess.TimeoutExpired:
                    ent.update(ok=False, error=f"list took longer than {LIST_TIMEOUT}s")
                except Exception as e:  # noqa: BLE001  (bad JSON, script missing, exit != 0, ...)
                    ent.update(ok=False, error=_clean(e, 200))
                if not ent["ok"]:
                    log.warning("cloud workers: %s list: %s", m["id"], ent["error"])
                c["addons"][m["id"]] = ent
            c["updated"] = now
            _write(self.cache_path, c)
            return c
        except Exception:  # noqa: BLE001
            log.exception("cloud workers: refresh failed")
            return self.cache()
        finally:
            os.close(fd)

    def refresh_background(self) -> None:
        """Refresh in a daemon thread if stale (for callers that must not wait, e.g. the 3 s face writer)."""
        if self._bg is not None and self._bg.is_alive():
            return
        try:
            if not self.stale():
                return
        except Exception:  # noqa: BLE001
            return
        self._bg = threading.Thread(target=self.refresh, name="cloud-workers", daemon=True)
        self._bg.start()

    def jobs(self, refresh: bool | str = False) -> list[dict]:
        """All cloud jobs of the add-ons that are ON. refresh: True = wait for a refresh if stale,
        "background" = start one and answer from the cache, False = cache only."""
        if refresh == "background":
            self.refresh_background()
            c = self.cache()
        elif refresh:
            c = self.refresh() if self.stale() else self.cache()
        else:
            c = self.cache()
        try:
            on = {m["id"] for m in self.sources()}
        except Exception:  # noqa: BLE001
            on = set()
        out = []
        for aid, ent in sorted(c["addons"].items()):
            if aid in on and isinstance(ent, dict):
                out += [j for j in ent.get("jobs") or [] if isinstance(j, dict)]
        return out

    def errors(self) -> dict[str, str]:
        return {a: e.get("error") for a, e in self.cache()["addons"].items() if isinstance(e, dict) and e.get("error")}

    # ---------------------------------------------------------- finished jobs -> reports
    def state(self) -> dict:
        s = _read(self.state_path, {})
        s = s if isinstance(s, dict) else {}
        for k in ("seen", "delivered", "tries"):
            s.setdefault(k, {})
        return s

    def fetch_report(self, job: dict) -> str | None:
        """The finished job's report from the add-on's fetch (None when it can't be had right now)."""
        m = next((x for x in self.sources() if x["id"] == job["addon"]), None)
        if m is None:
            return None
        if not (m.get("workers") or {}).get("fetch"):
            return ""
        try:
            d = self._run(m, "fetch", job["job"], FETCH_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            log.warning("cloud workers: %s fetch %s: %s", job["addon"], job["job"], _clean(e, 200))
            return None
        if isinstance(d, dict):
            r = d.get("report")
            return str(r)[-REPORT_CAP:] if r is not None else json.dumps(d)[:REPORT_CAP]
        if isinstance(d, str):
            return d[-REPORT_CAP:]
        return ""

    def finished(self) -> list[dict]:
        """Jobs that ended and haven't been reported. Records a baseline the first time an add-on is seen."""
        s = self.state()
        out, changed = [], False
        on = {m["id"] for m in self.sources()}
        for aid, ent in sorted(self.cache()["addons"].items()):
            if aid not in on or not isinstance(ent, dict) or not ent.get("ok"):
                continue
            jobs = [j for j in ent.get("jobs") or [] if isinstance(j, dict) and j.get("key")]
            if aid not in s["seen"]:            # first look: what ended before anyone watched is history
                s["seen"][aid] = time.time()
                for j in jobs:
                    if j["state"] in TERMINAL and not j.get("seen_end"):
                        s["delivered"][j["key"]] = time.time()
                changed = True
            for j in jobs:
                if j["state"] not in TERMINAL or j["key"] in s["delivered"]:
                    continue
                if j["state"] == "stopped":     # stopped on purpose: nothing to report (like a cancelled task)
                    s["delivered"][j["key"]] = time.time()
                    changed = True
                    continue
                out.append(j)
        if changed:
            self._save_state(s)
        return out

    def _save_state(self, s: dict) -> None:
        if len(s["delivered"]) > 500:
            keep = sorted(s["delivered"].items(), key=lambda kv: kv[1])[-400:]
            s["delivered"] = dict(keep)
        _write(self.state_path, s)

    def deliver(self, on_finish: Callable[[dict], None]) -> int:
        """Fetch + report every newly finished job once. Blocking (run it in a thread). Returns how many."""
        n = 0
        for j in self.finished():
            report = self.fetch_report(j)
            s = self.state()
            if j["key"] in s["delivered"]:
                continue
            if report is None:
                s["tries"][j["key"]] = int(s["tries"].get(j["key"], 0)) + 1
                if s["tries"][j["key"]] < FETCH_TRIES:
                    self._save_state(s)
                    continue
                report = f"(Couldn't fetch the report from {j['addon_name']}; last step: {j['step'] or 'unknown'})"
            s["tries"].pop(j["key"], None)
            s["delivered"][j["key"]] = time.time()
            self._save_state(s)                 # remembered before it's handed over: never twice
            item = task_item(j, report)
            try:
                on_finish(item)
            except Exception:  # noqa: BLE001
                log.exception("cloud workers: couldn't hand over %s", j["key"])
            n += 1
            log.info("cloud job %s %s: reported", j["key"], j["state"])
        return n


def task_item(j: dict, report: str) -> dict:
    """A finished cloud job shaped like a tasks.json item (for the task-finished turn)."""
    from .security import wrap
    body = (report or "").strip() or "(no report)"
    return {"id": j["key"], "title": j["title"], "status": j["state"], "cloud": True, "addon": j["addon"],
            "addon_name": j["addon_name"], "workspace": j.get("workspace"), "started": j.get("started"),
            "finished": j.get("finished") or time.time(),
            "result": wrap(body, f"add-on {j['addon']} cloud worker", REPORT_CAP)}


def finish_text(item: dict) -> str:
    """The inbox text for a finished cloud job (same shape as a local task's)."""
    return (f"Cloud task {item['id']} (from the {item.get('addon_name') or item.get('addon')} add-on) "
            f"\"{item['title']}\" {item['status']}.\nWorker's report:\n{item['result']}")


def live_row(j: dict, now: float, look: dict | None = None) -> dict:
    """A cloud job as a row of face/dashboard-workers.json (walter/workerlive.py). `look`: the add-on's
    [workers.look] as CloudWorkers.looks() resolves it (left out = the default cloud-blue diamond)."""
    st = "cancelled" if j["state"] == "stopped" else j["state"]
    started = j.get("started")
    fin = j.get("finished")
    act = j.get("step") or {"queued": "Waiting in the cloud", "running": "Working in the cloud",
                            "done": "Finished", "failed": "Stopped with a problem",
                            "stopped": "Stopped"}[j["state"]]
    return {"id": j["key"], "title": j["title"][:80], "status": st, "started": started, "finished": fin,
            "elapsed": int(((fin or now) - started)) if started else 0, "activity": act[:120],
            "last_activity": None, "files": 0, "steps": 0, "log": [], "workspace": j.get("workspace"),
            "cloud": True, "addon": j["addon"], "addon_name": j["addon_name"],
            **({"look": look} if look else {})}


class Watcher:
    """In the running service: refresh the cache in a thread every POLL_S seconds and report finished jobs."""

    def __init__(self, on_finish: Callable[[dict], None], cw: CloudWorkers | None = None, every: float = POLL_S):
        self.cw = cw or CloudWorkers()
        self.on_finish = on_finish
        self.every = every
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        self.task = asyncio.get_running_loop().create_task(self._loop())

    async def _loop(self):
        await asyncio.sleep(15)                 # let startup settle first
        loop = asyncio.get_running_loop()

        def hand(item):                         # from the worker thread back to the event loop
            loop.call_soon_threadsafe(self.on_finish, item)

        while True:
            try:
                await asyncio.to_thread(self._once, hand)   # file reads, the list and fetch: all off the loop
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("cloud workers: poll failed")
            await asyncio.sleep(self.every)

    def _once(self, hand):
        if not self.cw.sources():
            return
        self.cw.refresh()
        self.cw.deliver(hand)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

