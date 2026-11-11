"""Screen rotation (walter/screen_rotate.py, /api/app/rotate, walterctl display rotate). Never touches the
real screen: a fake wlr-randr on PATH keeps its state in a temp file, and HOME is a temp dir."""
import asyncio
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from walter import screen_rotate as R
from tests.test_app import PROXY, iso, make_app  # noqa: F401  (iso is a fixture)

REPO = Path(__file__).resolve().parent.parent

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
st = os.environ["FAKE_RANDR_STATE"]
d = json.load(open(st))
with open(st + ".log", "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
a = sys.argv[1:]
if not a:
    for name, o in d.items():
        print(f'{name} "Some Monitor"')
        print("  Enabled: " + ("yes" if o["on"] else "no"))
        print("  Modes:")
        print("    1920x1080 px, 60.000000 Hz (current)")
        print("  Position: 0,0")
        print("  Transform: " + o["t"])
        print("  Scale: 1.000000")
    sys.exit(0)
if a[0] == "--output" and a[2] == "--transform":
    if a[1] not in d:
        print("unknown output", file=sys.stderr); sys.exit(1)
    d[a[1]]["t"] = a[3]
    json.dump(d, open(st, "w"))
    sys.exit(0)
sys.exit(2)
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".walter").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "wlr-randr"
    exe.write_text(FAKE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "randr.json"
    state.write_text(json.dumps({"HDMI-A-2": {"on": True, "t": "normal"}, "eDP-1": {"on": False, "t": "normal"}}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_RANDR_STATE", str(state))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("WALTER_WAYLAND", "wayland-test")

    class F:
        env = home / ".walter" / "kiosk.env"

        @staticmethod
        def screen():
            return {k: v["t"] for k, v in json.loads(state.read_text()).items()}

        @staticmethod
        def calls():
            p = Path(str(state) + ".log")
            return p.read_text().splitlines() if p.exists() else []
    F.bindir, F.home = bindir, home
    return F


def test_parse_outputs_only_enabled():
    text = ('HDMI-A-2 "LG"\n  Enabled: yes\n  Transform: 270\n'
            'eDP-1 "Panel"\n  Enabled: no\n  Transform: normal\n'
            'DP-1 "Other"\n  Enabled: yes\n')
    assert R.parse_outputs(text) == {"HDMI-A-2": "270", "DP-1": "normal"}
    assert R.parse_outputs("") == {}
    assert R.orientation_of("90") == R.orientation_of("flipped-270") == "portrait"
    assert R.orientation_of("normal") == R.orientation_of("180") == "landscape"


def test_current_reads_the_fake_screen(fake):
    st = R.current()
    assert st["outputs"] == {"HDMI-A-2": "normal"} and st["orientation"] == "landscape" and st["live"]
    assert st["error"] is None and st["portrait_transform"] == "270"


def test_portrait_landscape_flip_live_and_persisted(fake):
    fake.env.write_text("XCURSOR_THEME=walter-blank\n# a comment\nWALTER_ROTATE=90\nXCURSOR_SIZE=1\n")
    r = R.set("portrait")
    assert r["ok"] and r["orientation"] == "portrait" and r["transform"] == "90" and r["applied"] == ["HDMI-A-2"]
    assert fake.screen() == {"HDMI-A-2": "90", "eDP-1": "normal"}   # disabled output untouched
    r = R.set("landscape")
    assert r["ok"] and fake.screen()["HDMI-A-2"] == "normal" and r["saved"] == "0"
    lines = fake.env.read_text().splitlines()
    assert lines == ["XCURSOR_THEME=walter-blank", "# a comment", "WALTER_ROTATE=0", "XCURSOR_SIZE=1",
                     "WALTER_PORTRAIT=90"]
    r = R.set("flip")                      # landscape -> portrait, the remembered way (90)
    assert r["ok"] and r["orientation"] == "portrait" and fake.screen()["HDMI-A-2"] == "90"
    assert "WALTER_ROTATE=90" in fake.env.read_text()
    r = R.set("flip")                      # portrait -> landscape
    assert r["orientation"] == "landscape" and fake.screen()["HDMI-A-2"] == "normal"
    assert fake.env.read_text().count("WALTER_ROTATE=") == 1
    assert "XCURSOR_SIZE=1" in fake.env.read_text()


def test_default_portrait_is_270_and_file_created(fake):
    assert not fake.env.exists()
    r = R.set("portrait")
    assert r["transform"] == "270" and fake.screen()["HDMI-A-2"] == "270"
    assert "WALTER_ROTATE=270" in fake.env.read_text().splitlines()


def test_persist_false_leaves_kiosk_env(fake):
    fake.env.write_text("WALTER_ROTATE=270\n")
    R.set("landscape", persist=False)
    assert fake.env.read_text() == "WALTER_ROTATE=270\n" and fake.screen()["HDMI-A-2"] == "normal"


def test_whitelist(fake):
    for bad in ("upside", "", None, "PORTRAIT", "90", "portrait; rm -rf /"):
        r = R.set(bad)
        assert r["ok"] is False and "orientation" in r["error"]
    assert fake.calls() == [] and not fake.env.exists()


def test_missing_wlr_randr_never_raises(fake, monkeypatch):
    (fake.bindir / "wlr-randr").unlink()
    monkeypatch.setenv("PATH", str(fake.bindir))
    st = R.current()
    assert st["live"] is False and "wlr-randr" in st["error"]
    r = R.set("portrait")
    assert r["ok"] is False and "wlr-randr" in r["error"] and r["saved"] == "270"   # still saved for next start


def test_failing_wlr_randr_reports(fake, monkeypatch):
    (fake.bindir / "wlr-randr").write_text("#!/bin/sh\necho 'cannot connect to display' >&2\nexit 1\n")
    r = R.set("landscape")
    assert r["ok"] is False and "cannot connect" in r["error"]


def test_walterctl_display_rotate(fake):
    env = dict(os.environ)
    run = lambda *a: subprocess.run([sys.executable, str(REPO / "bin" / "walterctl"), "display", "rotate", *a],
                                    capture_output=True, text=True, env=env, timeout=30)
    r = run("portrait")
    assert r.returncode == 0, r.stderr
    assert fake.screen()["HDMI-A-2"] == "270" and "WALTER_ROTATE=270" in fake.env.read_text()
    r = run("status")
    assert r.returncode == 0 and "portrait" in r.stdout
    assert run("flip").returncode == 0 and fake.screen()["HDMI-A-2"] == "normal"


def test_api_auth_and_whitelist(iso, fake):  # noqa: F811
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        app, apphub, _, _ = make_app(iso)
        async with TestClient(TestServer(app)) as c:
            assert (await c.get("/api/app/rotate", headers=PROXY)).status == 401
            assert (await c.post("/api/app/rotate", json={"orientation": "portrait"}, headers=PROXY)).status == 401
            assert fake.screen()["HDMI-A-2"] == "normal"
            code = apphub.store.start_pairing()["code"]
            token = (await (await c.post("/api/app/pair", json={"code": code, "name": "Phone"},
                                         headers=PROXY)).json())["token"]
            auth = {**PROXY, "Authorization": f"Bearer {token}"}
            j = await (await c.get("/api/app/rotate", headers=auth)).json()
            assert j["orientation"] == "landscape" and j["choices"] == ["portrait", "landscape", "flip", "portrait-flipped", "landscape-flipped"]
            for bad in ({"orientation": "sideways"}, {"orientation": 90}, {}, []):
                r = await c.post("/api/app/rotate", json=bad, headers=auth)
                assert r.status == 400
            assert fake.calls() == ["", ""] or all(x == "" for x in fake.calls())   # only reads so far
            r = await c.post("/api/app/rotate", json={"orientation": "flip"}, headers=auth)
            j = await r.json()
            assert r.status == 200 and j["ok"] and j["orientation"] == "portrait"
            assert fake.screen()["HDMI-A-2"] == "270"
    asyncio.run(go())


def test_flipped_orientations(tmp_path, monkeypatch):
    """portrait-flipped = the other portrait turn (90 <-> 270); landscape-flipped = 180; both are saved in kiosk.env."""
    import walter.screen_rotate as R
    env = tmp_path / "kiosk.env"
    env.write_text("WALTER_ROTATE=270\nOTHER=1\n")
    monkeypatch.setattr(R, "kiosk_env_path", lambda: str(env))
    calls = []
    monkeypatch.setattr(R, "_randr", lambda *a: calls.append(a) or "")
    monkeypatch.setattr(R, "outputs", lambda: {"HDMI-A-2": "270"})
    r = R.set_orientation("portrait-flipped")
    assert r["transform"] == "90" and ("--output", "HDMI-A-2", "--transform", "90") in calls
    assert "WALTER_ROTATE=90" in env.read_text() and "OTHER=1" in env.read_text()
    monkeypatch.setattr(R, "outputs", lambda: {"HDMI-A-2": "90"})
    r = R.set_orientation("landscape-flipped")
    assert r["transform"] == "180" and "WALTER_ROTATE=180" in env.read_text()
    assert R.orientation_of("180") == "landscape"
