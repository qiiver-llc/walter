#!/usr/bin/env python3
"""Runs one background worker, detached from Walter (see tasks.py).

    python taskrun.py SPEC.json

SPEC.json (mode 600, deleted as soon as it's read, because the environment can
hold credentials) has {"cmd": [...], "cwd": "...", "env": {...}, "out": "...",
"err": "...", "rc": "...", "pid": "..."}. The worker's stdout/stderr go to the out/err files;
when it exits its return code is written to the rc file (atomically), which is
how Walter, possibly restarted in the meantime, knows it finished.

Standalone on purpose: no imports from the walter package, so it keeps working
while Walter's own code is being changed or restarted.
"""
import json
import os
import signal
import subprocess
import sys


def main() -> int:
    spec_path = sys.argv[1]
    with open(spec_path) as f:
        spec = json.load(f)
    try:
        os.unlink(spec_path)
    except OSError:
        pass
    old = os.umask(0o077)
    try:
        out = open(spec["out"], "ab")
        err = open(spec["err"], "ab")
    finally:
        os.umask(old)
    if spec.get("pid"):
        with open(spec["pid"], "w") as f:
            f.write(str(os.getpid()))
    proc = subprocess.Popen(spec["cmd"], cwd=spec.get("cwd") or None, env=spec.get("env"),
                            stdin=subprocess.DEVNULL, stdout=out, stderr=err)

    def forward(sig, _frame):  # cancel / timeout: pass it on to the worker
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass
    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    rc = proc.wait()
    tmp = spec["rc"] + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(rc))
    os.replace(tmp, spec["rc"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
