"""Face focus corner: runs the headless smoke test (tests/face/focus_corner_smoke.py)
when the browser venv and system Chromium are there. ~45 s, so opt-in:
WALTER_FACE_SMOKE=1 pytest tests/test_face_focus_corner.py"""
import os
import subprocess
from pathlib import Path

import pytest

PY = Path.home() / ".walter" / "browser-venv" / "bin" / "python"
SCRIPT = Path(__file__).resolve().parent / "face" / "focus_corner_smoke.py"


@pytest.mark.skipif(not os.environ.get("WALTER_FACE_SMOKE") or not PY.exists()
                    or not Path("/usr/bin/chromium").exists(), reason="opt-in face smoke test")
def test_focus_corner_smoke():
    r = subprocess.run([str(PY), str(SCRIPT)], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr


def test_face_has_focus_corner():
    js = (SCRIPT.parents[2] / "face" / "walter.js").read_text()
    assert "focusCornerWanted" in js and "CALM_ACTIONS" in js


def test_face_has_big_focus_ring():
    js = (SCRIPT.parents[2] / "face" / "focus.js").read_text()
    assert "focusring" in js and "fitTitle" in js
