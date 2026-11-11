"""Smoke test: every tag the brain can emit (walter/tags.py) is known to the face.

A merge once silently dropped the movement tags from face/walter.js and they failed
in front of a visitor (4 Nov). This parses the face's EMOTIONS / ACTIONS tables as
text (no browser) and checks nothing in tags.py has gone missing.
"""
import re
from pathlib import Path

from walter.tags import ACTIONS, EMOTIONS

FACE_JS = (Path(__file__).resolve().parent.parent / "face" / "walter.js").read_text()

# Brain actions the face handles without an ACTIONS entry, and why.
SPECIAL = {
    "blink": "addAction() special-cases it (resets the blink timer)",
    "laugh:big": "conductor sends it to the face as plain 'laugh' (only the sound differs)",
}


def _table_keys(name: str) -> set[str]:
    m = re.search(r"const " + name + r" = \{\n(.*?)\n  \};", FACE_JS, re.S)
    assert m, f"couldn't find const {name} in face/walter.js"
    return set(re.findall(r'^\s*"?([a-z][a-z:\-]*)"?\s*:\s*\{', m.group(1), re.M))


def test_face_tables_parse():
    assert {"bounce", "grow", "scoot:left", "laugh"} <= _table_keys("ACTIONS")
    assert {"neutral", "happy", "wink"} <= _table_keys("EMOTIONS")


def test_every_brain_emotion_is_on_the_face():
    missing = sorted(EMOTIONS - _table_keys("EMOTIONS"))
    assert not missing, f"emotions the face doesn't know: {missing}"


def test_every_brain_action_is_on_the_face():
    face = _table_keys("ACTIONS")
    wanted = {a for a in ACTIONS if not a.startswith("look:")}
    missing = sorted(wanted - face - set(SPECIAL))
    assert not missing, f"actions the face would silently drop: {missing}"


def test_special_cases_still_handled():
    assert 'if (name === "blink")' in FACE_JS
    conductor = (Path(__file__).resolve().parent.parent / "walter" / "conductor.py").read_text()
    assert '"laugh" if val.startswith("laugh")' in conductor


def test_look_directions_are_on_the_face():
    m = re.search(r"const L = \{(.*?)\}\[dir\]", FACE_JS)
    assert m, "couldn't find the look direction map in addAction()"
    dirs = set(re.findall(r'"?([a-z\-]+)"?\s*:\s*\[', m.group(1)))
    missing = sorted(a[5:] for a in ACTIONS if a.startswith("look:") and a[5:] not in dirs)
    assert not missing, f"look directions the face doesn't know: {missing}"
