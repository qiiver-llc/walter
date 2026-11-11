"""Portrait layout: runs the headless smoke test (tests/face/portrait_smoke.py) at 1080x1920,
800x1280 and 1920x1080 when the browser venv and system Chromium are there. ~4 min, so opt-in:
WALTER_FACE_SMOKE=1 pytest tests/test_face_portrait.py. The quick checks below always run."""
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = Path.home() / ".walter" / "browser-venv" / "bin" / "python"
SCRIPT = ROOT / "tests" / "face" / "portrait_smoke.py"


@pytest.mark.skipif(not os.environ.get("WALTER_FACE_SMOKE") or not PY.exists()
                    or not Path("/usr/bin/chromium").exists(), reason="opt-in face smoke test")
def test_portrait_smoke(tmp_path):
    r = subprocess.run([str(PY), str(SCRIPT), str(tmp_path)], capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]


def test_portrait_js_loads_last():
    html = (ROOT / "face" / "index.html").read_text()
    scripts = re.findall(r'<script src="([^"]+)"', html)
    # must see every other script's CSS; only textscale.js (font sizes) and themes.js (re-inks sheets) come after it
    assert scripts[-3:] == ["portrait.js", "textscale.js", "themes.js"], scripts  # themes.js rewrites every sheet, so it is last


def test_portrait_only_under_media_query():
    js = (ROOT / "face" / "portrait.js").read_text()
    assert "\"(orientation: portrait)\"" in js and len(re.findall(r"\bs\.media = MQ", js)) == 1 and "css.media = MQ" in js


def test_walter_js_landscape_unchanged_path():
    js = (ROOT / "face" / "walter.js").read_text()
    assert "(orientation: portrait)" in js and ": H * 0.5" in js  # landscape: centred (minus a small build-view lift)


def test_kiosk_rotation_wrapper(tmp_path):
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "wlr-randr").write_text('#!/bin/sh\nif [ -z "$1" ]; then printf \'eDP-1 "Panel"\\n  Enabled: yes\\n'
                                    'HDMI-A-1 "TV"\\n  Enabled: no\\n\'; else echo "$*" >> "$HOME/calls"; fi\n')
    (fake / "wlr-randr").chmod(0o755)
    (tmp_path / ".walter").mkdir()
    env = {**os.environ, "HOME": str(tmp_path), "PATH": f"{fake}:{os.environ['PATH']}"}
    wrap = ROOT / "deploy" / "kiosk-rotate.sh"
    r = subprocess.run([str(wrap), "echo", "browser"], env={**env, "WALTER_ROTATE": "90"},
                       capture_output=True, text=True, timeout=20)
    assert r.stdout.strip() == "browser"
    assert (tmp_path / "calls").read_text().strip() == "--output eDP-1 --transform 90"
    (tmp_path / "calls").unlink()
    r = subprocess.run([str(wrap), "echo", "browser"], env={**env, "WALTER_ROTATE": "0"},
                       capture_output=True, text=True, timeout=20)
    assert r.stdout.strip() == "browser" and not (tmp_path / "calls").exists()


def test_walterctl_display(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path)}
    kenv = tmp_path / ".walter" / "kiosk.env"
    kenv.parent.mkdir()
    kenv.write_text("WLR_DRM_NO_ATOMIC=1\n")
    ctl = [str(ROOT / "bin" / "walterctl"), "display"]
    subprocess.run(ctl + ["portrait"], env=env, check=True, capture_output=True)
    assert kenv.read_text() == "WLR_DRM_NO_ATOMIC=1\nWALTER_ROTATE=90\n"
    subprocess.run(ctl + ["rotate", "270"], env=env, check=True, capture_output=True)
    assert kenv.read_text() == "WLR_DRM_NO_ATOMIC=1\nWALTER_ROTATE=270\n"
    subprocess.run(ctl + ["landscape"], env=env, check=True, capture_output=True)
    assert kenv.read_text() == "WLR_DRM_NO_ATOMIC=1\n"
    assert subprocess.run(ctl + ["rotate", "45"], env=env, capture_output=True).returncode != 0
