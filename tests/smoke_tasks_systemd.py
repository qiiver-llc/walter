#!/usr/bin/env python3
"""Manual smoke test (not part of pytest): real systemd-run worker survives a
"restart" of the Tasks manager and is re-adopted.

    WALTER_DATA=$(mktemp -d) .venv/bin/python tests/smoke_tasks_systemd.py

Starts a harmless worker (a shell: sleep 20, then print a JSON result) as a real
transient user unit with a separate name prefix, kills the first Python process
that started it (as a service restart would), then a second process
re-instantiates Tasks and must re-adopt and finish it. Uses only $WALTER_DATA.
"""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
DATA = os.environ.get("WALTER_DATA")
if not DATA or Path(DATA).resolve() == (Path.home() / ".walter").resolve():
    sys.exit("set WALTER_DATA to a throwaway directory")

from walter.config import Config  # noqa: E402
from walter.tasks import SystemdLauncher, Tasks  # noqa: E402

PREFIX = "walter-smoketest-task"


def cfg():
    c = Config()
    fake = Path(DATA) / "fake-claude.sh"
    fake.write_text('#!/bin/sh\nsleep 20\necho \'{"type":"result","is_error":false,"result":"ok"}\'\n')
    fake.chmod(0o755)
    c.brain.claude_bin = str(fake)
    c.brain.home = DATA
    return c


async def phase1():
    t = Tasks(cfg(), print, lambda n: None, launcher=SystemdLauncher(PREFIX))
    item = await t.start("smoke", "smoke test")
    print("started", item)
    await asyncio.sleep(2)
    assert await t.launcher.alive(item["id"], None), "unit not active"
    os._exit(0)          # die abruptly, like the service being killed mid-run


async def phase2():
    done = []
    counts = []
    t = Tasks(cfg(), done.append, counts.append, launcher=SystemdLauncher(PREFIX))
    await asyncio.sleep(0.5)
    assert t.watchers, "nothing re-adopted"
    print("re-adopted; running =", t.running, "counts =", counts)
    end = time.time() + 60
    while not done and time.time() < end:
        await asyncio.sleep(1)
    assert done, "never finished"
    print("finished:", done[0]["status"], repr(done[0]["result"]))
    assert done[0]["status"] == "done" and done[0]["result"] == "ok"
    await t.close()


if __name__ == "__main__":
    if sys.argv[1:] == ["phase1"]:
        asyncio.run(phase1())
    elif sys.argv[1:] == ["phase2"]:
        asyncio.run(phase2())
    else:
        env = dict(os.environ)
        subprocess.run([sys.executable, __file__, "phase1"], env=env, check=True)
        print(subprocess.run(["systemctl", "--user", "show", "-p", "ActiveState", "-p", "ControlGroup",
                              f"{PREFIX}-1.service"], capture_output=True, text=True).stdout)
        subprocess.run([sys.executable, __file__, "phase2"], env=env, check=True)
        print(json.loads((Path(DATA) / "tasks.json").read_text())[-1]["status"])
        print("SMOKE TEST PASSED")
