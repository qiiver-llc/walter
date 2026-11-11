"""display.json "calendar_style" (walter/display_settings.py; face/calstyle.js)."""
import json
import re
from pathlib import Path

import pytest

from walter import display_settings as S

REPO = Path(__file__).resolve().parents[1]


def test_default_and_set(tmp_path):
    p = tmp_path / "display.json"
    assert S.get_calendar_style(p) == "chips"
    assert S.set_calendar_style("counts", p) == ("counts", "chips")
    assert json.loads(p.read_text())["calendar_style"] == "counts"
    assert S.set_calendar_style("toggle", p) == ("chips", "counts")
    assert S.set_calendar_style("TOGGLE", p) == ("counts", "chips")
    assert S.set_calendar_style("chip", p) == ("chips", "counts")


def test_bad_values(tmp_path):
    p = tmp_path / "display.json"
    for bad in ("tiles please", "", 3, None):
        with pytest.raises(ValueError):
            S.set_calendar_style(bad, p)
    p.write_text(json.dumps({"calendar_style": "weird", "theme": "fall"}))
    assert S.get_calendar_style(p) == "chips"
    S.set_calendar_style("counts", p)
    assert json.loads(p.read_text())["theme"] == "fall"  # other settings kept


def test_same_styles_as_face():
    js = (REPO / "face" / "calstyle.js").read_text()
    m = re.search(r"const STYLES = \[([^\]]*)\]", js)
    assert tuple(re.findall(r'"(\w+)"', m.group(1))) == S.CAL_STYLES
