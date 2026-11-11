"""Live system gauges for the build cockpit: writes face/dashboard-gauges.json every few seconds.

A tiny separate loop (own CPU cache, so it never disturbs the 15 s walter-build.timer deltas).
Run as walter-gauges-live.service; one-off: python3 -m walter.gaugelive --once.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from walter import cockpit

FACE = Path(__file__).resolve().parent.parent / "face"
CACHE = cockpit.DATA / "cache" / "cockpit-cpu-live.json"


def snapshot() -> dict:
    try:
        load = os.getloadavg()[0]
    except OSError:
        load = None
    return {"cpu": cockpit.cpu_percent(cache=CACHE), "mem": cockpit.mem_percent(),
            "load": round(load, 2) if load is not None else None, "cores": os.cpu_count() or 1,
            "ts": round(time.time(), 1)}


def write(out: Path) -> None:
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(snapshot()))
    os.replace(tmp, out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(FACE / "dashboard-gauges.json"))
    ap.add_argument("--loop", type=float, default=3.0)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    write(out)
    if a.once:
        return
    while True:
        time.sleep(max(1.0, a.loop))
        try:
            write(out)
        except OSError:
            pass


if __name__ == "__main__":
    main()
