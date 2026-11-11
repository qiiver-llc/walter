"""Restart Walter from the companion app, the same safe way Walter does it by hand:

1. py_compile every walter/*.py with the service's own Python; refuse (with the
   error text) if anything fails to compile, so a broken edit never takes him down.
2. Look at the running background workers (the same list `walterctl tasks` shows).
   They survive restarts now, but the app warns and needs an explicit
   "Restart anyway" (force=True) before going ahead.
3. At most one restart per 30 s (remembered across the restart itself).
4. `systemctl --user restart walter` in its own transient unit (systemd-run), after
   a short delay, so the HTTP answer reaches the phone first.

Every attempt is logged to the journal and to ~/.walter/app-restarts.log.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from .config import REPO_DIR

log = logging.getLogger("walter.app.restart")

MIN_GAP_S = 30.0
DELAY_S = 1.5          # let the HTTP response go out before systemd stops us
UNIT = "walter"


def compile_check(repo: Path = REPO_DIR, python: str = sys.executable, timeout: float = 60) -> str:
    """'' if every walter/*.py compiles, else the compiler's error text."""
    files = sorted(str(p) for p in (repo / "walter").glob("*.py"))
    if not files:
        return "no walter/*.py files found"
    try:
        p = subprocess.run([python, "-m", "py_compile", *files], cwd=str(repo), capture_output=True,
                           text=True, timeout=timeout)
    except Exception as e:   # timeout, missing interpreter
        return f"couldn't run the compile check: {e}"
    if p.returncode == 0:
        return ""
    return ((p.stderr or "") + (p.stdout or "")).strip()[-2000:] or f"py_compile exited {p.returncode}"


def launch_restart(delay: float = DELAY_S, unit: str = UNIT) -> str:
    """Start the restart outside our own cgroup and return at once. Returns how."""
    sh = f"sleep {delay:g}; exec systemctl --user restart {unit}"
    run = shutil.which("systemd-run")
    if run:
        try:
            p = subprocess.run([run, "--user", "--collect", "--quiet",
                                f"--unit=walter-app-restart-{int(time.time())}",
                                "/bin/sh", "-c", sh], capture_output=True, text=True, timeout=10)
            if p.returncode == 0:
                return "systemd-run"
            log.warning("systemd-run failed (%s); falling back to a detached shell", p.stderr.strip())
        except Exception as e:
            log.warning("systemd-run failed (%s); falling back to a detached shell", e)
    # even if systemd kills this shell with the rest of Walter, the restart job
    # systemctl has already queued carries on
    subprocess.Popen(["/bin/sh", "-c", sh], start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    return "detached shell"


class Restarter:
    def __init__(self, tasks, data_dir: Path, *, compile_fn: Callable[[], str] = compile_check,
                 launch_fn: Callable[[], str] = launch_restart, clock: Callable[[], float] = time.time,
                 min_gap: float = MIN_GAP_S):
        self.tasks = tasks
        self.data_dir = Path(data_dir)
        self.compile_fn, self.launch_fn, self.clock, self.min_gap = compile_fn, launch_fn, clock, min_gap
        self._busy = False

    @property
    def state_path(self) -> Path:
        return self.data_dir / "app-restart.json"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "app-restarts.log"

    # ---------------------------------------------------------------- state
    def _last(self) -> float:
        try:
            return float(json.loads(self.state_path.read_text()).get("last", 0))
        except (OSError, ValueError, TypeError, AttributeError):
            return 0.0

    def retry_after(self) -> float:
        left = self.min_gap - (self.clock() - self._last())
        return max(0.0, min(left, self.min_gap))   # a clock jump back never locks it for long

    def workers(self) -> list[dict]:
        items = list(getattr(self.tasks, "items", None) or [])
        return [{"id": i.get("id"), "title": i.get("title") or "", "started": i.get("started")}
                for i in items if i.get("status") == "running"]

    def info(self) -> dict:
        return {"available": True, "workers": self.workers(), "retry_after": round(self.retry_after(), 1)}

    def _log(self, device: dict, outcome: str):
        name = (device or {}).get("name") or "?"
        log.info("app restart from %s: %s", name, outcome)
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {name}: {outcome}\n")
        except OSError:
            pass

    # ---------------------------------------------------------------- the action
    async def request(self, device: dict, force: bool = False) -> tuple[int, dict]:
        """(HTTP status, body) for POST /api/app/restart."""
        wait = self.retry_after()
        if wait > 0 or self._busy:
            self._log(device, "refused, too soon")
            return 429, {"ok": False, "reason": "too_soon", "retry_after": int(wait) + 1,
                         "error": f"Walter restarted moments ago; try again in {int(wait) + 1} s."}
        self._busy = True
        try:
            loop = asyncio.get_running_loop()
            err = await loop.run_in_executor(None, self.compile_fn)
            if err:
                self._log(device, "refused, compile check failed")
                return 409, {"ok": False, "reason": "compile", "error": err}
            workers = self.workers()
            if workers and not force:
                self._log(device, f"waiting for confirmation, {len(workers)} worker(s) running")
                return 409, {"ok": False, "reason": "workers", "workers": workers,
                             "error": "background workers are running"}
            try:
                self.data_dir.mkdir(parents=True, exist_ok=True)
                self.state_path.write_text(json.dumps({"last": self.clock(), "by": (device or {}).get("name")}))
            except OSError:
                pass
            try:
                how = await loop.run_in_executor(None, self.launch_fn)
            except Exception as e:
                self._log(device, f"launch failed: {e}")
                return 500, {"ok": False, "reason": "launch", "error": f"couldn't start the restart: {e}"}
            self._log(device, f"restarting ({how})" + (f", {len(workers)} worker(s) running" if workers else ""))
            return 200, {"ok": True, "restarting": True, "workers": workers}
        finally:
            self._busy = False
