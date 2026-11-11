"""Autonomy loop state (walter/loop.py) and `walterctl loop`."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from walter import loop as L

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def store(tmp_path):
    return L.LoopStore(tmp_path / "loop.json", tmp_path / "face" / "dashboard-loop.json")


def face(store):
    return json.loads(store.face_out.read_text())


def test_focus_next_and_face(store):
    store.set_focus("  Clean-machine   install test ")
    store.set_next("tunnel broker onboarding")
    st = store.state()
    assert st["focus"] == "Clean-machine install test" and st["focus_since"] > 0
    f = face(store)
    assert f["focus"] == "Clean-machine install test" and f["next"] == "tunnel broker onboarding"
    assert f["blocked"] == 0
    assert os.stat(store.face_out).st_mtime < 10 ** 9   # fixed mtime: no face auto-reload


def test_block_dedupe_and_unblock(store):
    a = store.block("Approve publishing the site")
    b = store.block("approve publishing the SITE")
    assert a["id"] == b["id"]
    store.block("Push to GitHub?")
    assert face(store)["blocked"] == 2 and face(store)["blocked_first"].startswith("Approve")
    assert store.unblock("github") == 1
    assert store.unblock(str(a["id"])) == 1
    store.block("x"); store.block("y")
    assert store.unblock("all") == 2 and face(store)["blocked"] == 0
    with pytest.raises(ValueError):
        store.block("   ")


def test_wins_are_per_day(store):
    store.win("Signed release channel")
    st = json.loads(store.path.read_text())
    st["wins"].append({"text": "old", "at": 0, "date": "2000-01-01"})
    store.path.write_text(json.dumps(st))
    assert [w["text"] for w in store.state()["wins"]] == ["Signed release channel"]


def test_tick_counts_runs_and_workers(store):
    now = time.time()
    store.tick(["Waitlist site", "Installer polish"], 1, now=now)
    st = store.tick(["Waitlist site"], 0, now=now + 60)
    assert st["runs_today"] == 2 and st["last_run"] == now + 60
    assert st["workers"] == {"running": ["Waitlist site"], "queued": 0}
    assert face(store)["running"] == 1
    out = L.format_status(store.state(), now=now + 60)
    assert "Focus: (none set)" in out and "1 running" in out and "Blocked on you: nothing" in out


def test_tick_keeps_workers_when_unknown(store):
    store.tick(["A"], 0)
    assert store.tick(None, None)["workers"]["running"] == ["A"]


def test_cli_roundtrip(tmp_path):
    env = dict(os.environ, WALTER_DATA=str(tmp_path), WALTER_URL="http://127.0.0.1:9")
    # point the face file into tmp via a tiny wrapper so the real face/ isn't touched
    code = (
        "import sys, runpy; from pathlib import Path; import walter.loop as L;"
        f"L.LoopStore.DEFAULT_FACE = Path({str(tmp_path / 'face.json')!r});"
        f"sys.argv = ['walterctl'] + sys.argv[1:]; runpy.run_path({str(REPO / 'bin' / 'walterctl')!r}, run_name='__main__')"
    )
    run = lambda *a: subprocess.run([sys.executable, "-c", code, *a], env=env, cwd=REPO,
                                    capture_output=True, text=True, timeout=30)
    assert "focus: Beta installer" in run("loop", "focus", "Beta", "installer").stdout
    assert "blocked #1" in run("loop", "blocked", "add", "Publish the site?").stdout
    assert "#1  Publish the site?" in run("loop", "blocked").stdout
    st = json.loads(run("loop", "status", "--json").stdout)
    assert st["focus"] == "Beta installer" and len(st["blocked"]) == 1
    assert "unblocked 1" in run("loop", "blocked", "clear").stdout
    assert json.loads((tmp_path / "face.json").read_text())["blocked"] == 0
    assert (tmp_path / "loop.json").exists()
