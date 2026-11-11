"""Background tasks: Walter keeps chatting while separate Claude Code workers
do the long jobs ("research this", "fix that script", "compare these flights").

Each task is its own headless `claude -p` run in Walter's home folder. When it
finishes, its report goes to Walter's inbox and he tells you the outcome, in
his own words, like a good assistant would.

Workers outlive Walter: each one runs as a transient systemd user unit
(walter-task-<ID>, its own cgroup) through taskrun.py, with its output in
~/.walter/tasks/<ID>.out/.err and its exit code in <ID>.rc. Restarting the
walter service doesn't touch them; on startup Walter re-adopts the ones still
running and finishes the ones that ended while he was away, so their reports
still reach him. Without systemd-run it falls back to plain child processes
(which do die with the service, as before).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from .config import REPO_DIR, Config, claude_env
from .security import RULE as UNTRUSTED_RULE, claude_permission_args

log = logging.getLogger("walter.tasks")

WORKER_PROMPT = """\
You are a background worker for Walter, a voice assistant. Walter delegated
this task to you so he can keep talking with his person while you work.
Work autonomously and completely; nobody can answer questions mid-task, so
make sensible decisions and note any assumptions. Don't do anything
destructive or irreversible outside Walter's own folders.

Your FINAL message is a report Walter will read and summarise out loud:
start with the outcome in one to three plain sentences, then any details,
file paths or numbers he might need for follow-up questions.

{rule}

Task: {title}

{prompt}
"""


RUNNER = Path(__file__).with_name("taskrun.py")
POLL_S = 3.0


def worker_priority(brain) -> dict[str, int]:
    """{cpu_weight, io_weight, nice} for workers, clamped to what systemd accepts."""
    def num(name, default, lo, hi):
        try:
            v = int(getattr(brain, name, default))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))
    return {"cpu_weight": num("worker_cpu_weight", 20, 1, 10000),
            "io_weight": num("worker_io_weight", 20, 1, 10000),
            "nice": num("worker_nice", 10, 0, 19)}


def priority_props(prio: dict[str, int]) -> list[str]:
    """systemd-run --property args; every process the worker spawns (Chromium,
    builds) sits in the unit's cgroup and inherits its weights and niceness."""
    return [f"--property=CPUWeight={prio['cpu_weight']}", f"--property=IOWeight={prio['io_weight']}",
            f"--property=Nice={prio['nice']}"]


class SystemdLauncher:
    """Each worker is a transient user unit: its own cgroup, so it survives
    `systemctl --user restart walter` (whose default KillMode kills everything
    in walter.service's cgroup, children included)."""
    kind = "systemd"

    def __init__(self, prefix: str = "walter-task"):
        self.prefix = prefix
        self.priority: dict[str, int] = worker_priority(None)   # Tasks sets it per launch

    def unit(self, tid: int) -> str:
        return f"{self.prefix}-{tid}.service"

    async def _run(self, *args: str) -> tuple[int, str]:
        p = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await p.communicate()
        return p.returncode, out.decode("utf-8", "replace").strip()

    async def launch(self, tid: int, runner_cmd: list[str], timeout: int) -> None:
        # The unit's own timeout is a backstop for when Walter isn't around to
        # enforce it; Walter's watcher normally stops it first.
        rc, out = await self._run(
            "systemd-run", "--user", "--collect", "--quiet", f"--unit={self.unit(tid)}",
            f"--description=Walter background worker #{tid}",
            f"--property=RuntimeMaxSec={int(timeout) + 60}",
            "--property=KillMode=control-group", *priority_props(self.priority),
            "--", *runner_cmd)
        if rc != 0:
            raise RuntimeError(f"systemd-run failed ({rc}): {out[-300:]}")

    async def alive(self, tid: int, pid: int | None) -> bool:
        _, out = await self._run("systemctl", "--user", "show", "-p", "ActiveState", "--value", self.unit(tid))
        return out in ("active", "activating", "reloading", "deactivating")

    def stop(self, tid: int, pid: int | None) -> None:
        subprocess.Popen(["systemctl", "--user", "stop", "--no-block", self.unit(tid)],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class DirectLauncher:
    """Fallback: the runner is a child of Walter in its own session. It still
    dies when the walter service restarts (same cgroup), as workers always did."""
    kind = "direct"

    def __init__(self):
        self.procs: dict[int, subprocess.Popen] = {}
        self.priority: dict[str, int] = worker_priority(None)

    async def launch(self, tid: int, runner_cmd: list[str], timeout: int) -> None:
        self.procs[tid] = p = subprocess.Popen(runner_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL, start_new_session=True)
        try:   # no cgroup weights here; niceness is inherited by the worker's children
            os.setpriority(os.PRIO_PROCESS, p.pid, self.priority["nice"])
        except (OSError, KeyError):
            pass

    async def alive(self, tid: int, pid: int | None) -> bool:
        p = self.procs.get(tid)
        if p is not None:
            if p.poll() is None:
                return True
            self.procs.pop(tid, None)
            return False
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    def stop(self, tid: int, pid: int | None) -> None:
        p = self.procs.get(tid)
        target = p.pid if p is not None else pid
        if target:
            try:
                os.killpg(target, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass


class Tasks:
    def __init__(self, cfg: Config, on_finish: Callable[[dict], None], on_count: Callable[[int], None],
                 launcher=None, fallback=None):
        self.cfg = cfg
        self.on_finish = on_finish
        self.on_count = on_count
        self.path = cfg.data_dir / "tasks.json"
        self.dir = cfg.data_dir / "tasks"
        self.launcher = launcher or SystemdLauncher()
        self.fallback = fallback or DirectLauncher()
        self.launchers = {l.kind: l for l in (self.fallback, self.launcher)}
        self.items: list[dict] = []
        self.watchers: dict[int, asyncio.Task] = {}
        try:
            self.items = json.loads(self.path.read_text())
        except Exception:
            self.items = []
        self._save()
        try:  # constructed inside the running service: re-adopt right away
            asyncio.get_running_loop().create_task(self.adopt())
        except RuntimeError:
            pass

    def _save(self):
        self.items = self.items[-50:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.items, indent=2))
        os.replace(tmp, self.path)

    @property
    def running(self) -> int:
        return sum(1 for i in self.items if i["status"] == "running")

    def _files(self, tid: int) -> dict[str, Path]:
        return {k: self.dir / f"{tid}.{k}" for k in ("out", "err", "rc", "pid", "spec")}

    def _cmd(self, title: str, prompt: str, model: str | None = None) -> list[str]:
        b = self.cfg.brain
        cmd = [shutil.which(b.claude_bin) or b.claude_bin, "-p",
               WORKER_PROMPT.format(title=title, prompt=prompt, rule=UNTRUSTED_RULE),
               "--output-format", "json",
               "--add-dir", str(REPO_DIR)] + claude_permission_args(b)
        model = model or b.task_model or b.model
        if model:
            cmd += ["--model", model]
        return cmd

    def _priority(self) -> dict[str, int]:
        """Worker priority from config.toml as it is *now* (no restart needed),
        falling back to the config Walter started with."""
        brain = self.cfg.brain
        path = Path(self.cfg.data_dir) / "config.toml"
        try:
            if path.exists():
                from . import config as config_mod
                brain = config_mod.load(path).brain
        except Exception as e:
            log.debug("worker priority: using startup config (%s)", e)
        return worker_priority(brain)

    async def start(self, title: str, prompt: str, quiet: bool = False, model: str | None = None,
                    workspace: str | None = None) -> dict:
        """quiet: the report is logged, not handed to the brain to speak (sleep-time review).
        workspace: the workspace tag (walter/workspaces.py); `walterctl task` already put the
        workspace's repos and privacy rule in front of the prompt."""
        if self.running >= self.cfg.brain.max_tasks:
            raise RuntimeError(f"already running {self.running} tasks (max {self.cfg.brain.max_tasks})")
        item = {"id": max([i["id"] for i in self.items], default=0) + 1, "title": title,
                "prompt": prompt, "status": "running", "started": time.time(),
                "finished": None, "result": None}
        if quiet:
            item["quiet"] = True
        if workspace:
            item["workspace"] = str(workspace)[:40]
        env = claude_env(self.cfg)
        env["WALTER_TASK_ID"] = str(item["id"])
        home = Path(self.cfg.brain.home)
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        f = self._files(item["id"])
        for k in ("out", "err", "rc", "pid"):
            f[k].unlink(missing_ok=True)
        spec = {"cmd": self._cmd(title, prompt, model), "cwd": str(home), "env": env,
                "out": str(f["out"]), "err": str(f["err"]), "rc": str(f["rc"]), "pid": str(f["pid"])}
        fd = os.open(f["spec"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(spec, fh)
        runner_cmd = [sys.executable, str(RUNNER), str(f["spec"])]
        timeout = self.cfg.brain.task_timeout
        prio = self._priority()
        for l in (self.launcher, self.fallback):
            l.priority = prio
        item["priority"] = prio
        try:
            await self.launcher.launch(item["id"], runner_cmd, timeout)
            item["launcher"] = self.launcher.kind
        except Exception as e:
            if self.fallback is self.launcher:
                f["spec"].unlink(missing_ok=True)
                raise
            log.warning("task #%d: %s launcher failed (%s); running it as a plain child, "
                        "which won't survive a restart", item["id"], self.launcher.kind, e)
            try:
                await self.fallback.launch(item["id"], runner_cmd, timeout)
            except Exception:
                f["spec"].unlink(missing_ok=True)
                raise
            item["launcher"] = self.fallback.kind
        self.items.append(item)
        self._save()
        self.on_count(self.running)
        log.info("task #%d started (%s): %s", item["id"], item["launcher"], title)
        self._spawn_watch(item)
        return item

    def _spawn_watch(self, item: dict):
        t = asyncio.create_task(self._watch(item))
        self.watchers[item["id"]] = t
        t.add_done_callback(lambda _t, tid=item["id"]: self.watchers.pop(tid, None)
                            if self.watchers.get(tid) is _t else None)

    async def adopt(self):
        """After a restart: keep watching workers that are still running and
        finish the ones that ended (or vanished) while Walter was away."""
        for item in [i for i in self.items if i["status"] == "running"]:
            if item["id"] in self.watchers:
                continue
            log.info("task #%d: re-adopting after restart", item["id"])
            self._spawn_watch(item)
        self.on_count(self.running)

    def _launcher_for(self, item: dict):
        return self.launchers.get(item.get("launcher", "direct"), self.fallback)

    def _pid(self, tid: int) -> int | None:
        try:
            return int(self._files(tid)["pid"].read_text().strip())
        except Exception:
            return None

    def _rc(self, tid: int) -> int | None:
        try:
            return int(self._files(tid)["rc"].read_text().strip())
        except Exception:
            return None

    def _parse(self, tid: int, rc: int | None) -> tuple[bool, str] | None:
        """(ok, result) like the old in-process watcher, or None when there is
        nothing at all to go on (no exit code and no JSON result)."""
        f = self._files(tid)
        out = f["out"].read_bytes() if f["out"].exists() else b""
        err = f["err"].read_bytes() if f["err"].exists() else b""
        ok = rc == 0
        try:
            data = json.loads(out.decode("utf-8", "replace").strip().splitlines()[-1])
            result = data.get("result") or ""
            ok = (rc is None or rc == 0) and not data.get("is_error")
        except Exception:
            if rc is None:
                return None
            result = (out or err).decode("utf-8", "replace")[-3000:]
        return ok, result

    def _cleanup_files(self, tid: int):
        f = self._files(tid)
        for k in ("rc", "pid", "spec"):
            f[k].unlink(missing_ok=True)
        keep = {i["id"] for i in self.items}
        try:  # drop output of tasks that fell off the 50-item history
            for p in self.dir.iterdir():
                stem = p.name.split(".", 1)[0]
                if stem.isdigit() and int(stem) not in keep:
                    p.unlink(missing_ok=True)
        except OSError:
            pass

    async def _watch(self, item: dict):
        tid = item["id"]
        launcher = self._launcher_for(item)
        timeout = self.cfg.brain.task_timeout
        deadline = (item.get("started") or time.time()) + timeout
        try:
            while True:
                if item["status"] != "running":       # cancelled meanwhile
                    return
                rc = self._rc(tid)
                if rc is not None:
                    parsed = self._parse(tid, rc)
                    break
                if not await launcher.alive(tid, self._pid(tid)):
                    await asyncio.sleep(0.2)          # it may be writing the rc file right now
                    rc = self._rc(tid)
                    parsed = self._parse(tid, rc)
                    if parsed is None:
                        f = self._files(tid)
                        err = f["err"].read_text("utf-8", "replace")[-500:].strip() if f["err"].exists() else ""
                        if time.time() >= deadline:
                            parsed = (False, f"timed out after {timeout}s")
                        else:
                            parsed = (False, "worker lost: it stopped without a report"
                                      + (f" ({err})" if err else ""))
                    break
                if time.time() >= deadline:
                    launcher.stop(tid, self._pid(tid))
                    parsed = (False, f"timed out after {timeout}s")
                    break
                await asyncio.sleep(POLL_S)
        except asyncio.CancelledError:
            raise                                     # close(): stop watching, leave the worker alone
        if item["status"] != "running":
            return
        ok, result = parsed
        item["status"] = "done" if ok else "failed"
        item["result"] = result.strip()[-6000:] or "(no report)"
        item["finished"] = time.time()
        self._save()
        self._cleanup_files(tid)
        self.on_count(self.running)
        log.info("task #%d %s", tid, item["status"])
        self.on_finish(item)

    def cancel(self, tid: int) -> bool:
        for it in self.items:
            if it["id"] == tid and it["status"] == "running":
                it["status"] = "cancelled"
                it["finished"] = time.time()
                self._launcher_for(it).stop(tid, self._pid(tid))
                w = self.watchers.pop(tid, None)
                if w:
                    w.cancel()
                self._save()
                self.on_count(self.running)
                return True
        return False

    async def close(self):
        """Stop watching; the workers keep going and are re-adopted on startup."""
        for w in list(self.watchers.values()):
            w.cancel()
        if self.watchers:
            await asyncio.gather(*self.watchers.values(), return_exceptions=True)
        self.watchers.clear()
