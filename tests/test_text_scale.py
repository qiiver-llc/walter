"""Global text scale (face/textscale.js, walterctl display text-scale). The headless check
(tests/face/textscale_smoke.py, ~3 min) is opt-in: WALTER_FACE_SMOKE=1 pytest tests/test_text_scale.py."""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = Path.home() / ".walter" / "browser-venv" / "bin" / "python"
SCRIPT = ROOT / "tests" / "face" / "textscale_smoke.py"


@pytest.mark.skipif(not os.environ.get("WALTER_FACE_SMOKE") or not PY.exists()
                    or not Path("/usr/bin/chromium").exists(), reason="opt-in face smoke test")
def test_textscale_smoke(tmp_path):
    r = subprocess.run([str(PY), str(SCRIPT), str(tmp_path)], capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]


def test_textscale_loaded_last():
    scripts = re.findall(r'<script src="([^"]+)"', (ROOT / "face" / "index.html").read_text())
    assert scripts[-2:] == ["textscale.js", "themes.js"], scripts  # must see every injected style sheet; themes.js rewrites them after


def test_textscale_only_touches_font_size():
    js = (ROOT / "face" / "textscale.js").read_text()
    assert "DEFAULT = 0.8, MIN = 0.5, MAX = 1.5" in js
    assert re.findall(r'setProperty\("([a-z-]+)"', js) == ["font-size", "--ts"]


def run_ctl(tmp_path, *args):
    (tmp_path / "bin").mkdir(exist_ok=True)
    (tmp_path / "face").mkdir(exist_ok=True)
    shutil.copy(ROOT / "bin" / "walterctl", tmp_path / "bin" / "walterctl")
    if not (tmp_path / "walter").exists():   # walterctl uses walter/display_settings.py (shared with the app)
        (tmp_path / "walter").symlink_to(ROOT / "walter", target_is_directory=True)
    return subprocess.run([sys.executable, str(tmp_path / "bin" / "walterctl"), "display", "text-scale", *args],
                          capture_output=True, text=True, timeout=30)


def test_ctl_text_scale_saves_and_keeps_other_settings(tmp_path):
    disp = tmp_path / "face" / "display.json"
    (tmp_path / "face").mkdir()
    disp.write_text(json.dumps({"rotate": 90, "layout": "large"}))
    r = run_ctl(tmp_path)
    assert r.returncode == 0 and "text scale: 0.8" in r.stdout  # default
    r = run_ctl(tmp_path, "0.65")
    assert r.returncode == 0, r.stderr
    d = json.loads(disp.read_text())
    assert d["text_scale"] == 0.65 and d["rotate"] == 90 and d["layout"] == "large"
    assert disp.stat().st_mtime < 1e9  # old mtime: never triggers a face reload


@pytest.mark.parametrize("bad", ["0.4", "1.6", "abc", "nan"])
def test_ctl_text_scale_rejects_out_of_range(tmp_path, bad):
    r = run_ctl(tmp_path, bad)
    assert r.returncode != 0 and "0.5 to 1.5" in r.stderr
    assert not (tmp_path / "face" / "display.json").exists()
