"""Wall themes: walter/display_settings.py theme helpers, `walterctl display theme`, /api/app/theme.
The face side (face/themes.js) is tested by tests/themes.test.js."""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from walter import display_settings as S
from tests.test_app import PROXY, iso, make_app  # noqa: F401  (iso is a fixture)

REPO = Path(__file__).resolve().parents[1]


def test_names_match_the_face():
    js = (REPO / "face" / "themes.js").read_text()
    body = js[js.index("const THEMES = {"):js.index("const NAMES")]
    assert tuple(re.findall(r"^    (\w+): \{ label:", body, re.M)) == S.THEMES
    assert len(S.THEMES) == 10 and S.THEME_DEFAULT == "midnight"


def test_parse():
    assert S.parse_theme("Fall") == "fall" and S.parse_theme(" auto ") == "auto"
    assert S.parse_theme("autumn") == "fall"
    for bad in ("", "neon", None, 3, True, ["fall"], "fall; rm -rf /"):
        with pytest.raises(ValueError):
            S.parse_theme(bad)


def _t(mon, hour):
    return time.struct_time((2026, mon, 10, hour, 0, 0, 0, 1, -1))


def test_auto_matches_the_face_rule():
    assert S.auto_theme(_t(11, 9)) == "fall" and S.auto_theme(_t(12, 12)) == "winter"
    assert S.auto_theme(_t(2, 12)) == "winter" and S.auto_theme(_t(3, 12)) == "spring"
    assert S.auto_theme(_t(5, 12)) == "spring" and S.auto_theme(_t(6, 12)) == "summer"
    assert S.auto_theme(_t(8, 12)) == "summer" and S.auto_theme(_t(9, 12)) == "fall"
    assert S.auto_theme(_t(7, 22)) == "midnight" and S.auto_theme(_t(7, 5)) == "midnight"
    assert S.auto_theme(_t(7, 6)) == "summer" and S.auto_theme(_t(7, 21)) == "summer"


def test_set_step_and_keep_other_settings(tmp_path):
    p = tmp_path / "display.json"
    p.write_text(json.dumps({"rotate": 90, "text_scale": 0.5, "set": 1}))
    assert S.get_theme(p) == "midnight"
    assert S.set_theme("ocean", p) == ("ocean", "midnight")
    d = json.loads(p.read_text())
    assert d["theme"] == "ocean" and d["rotate"] == 90 and d["text_scale"] == 0.5
    assert os.stat(p).st_mtime < 1e9             # never looks like a face edit
    assert S.step_theme("next", p) == ("forest", "ocean")
    assert S.step_theme("next", p) == ("midnight", "forest")   # wraps
    new, old = S.step_theme("random", p)
    assert old == "midnight" and new in S.THEMES and new != "midnight"
    with pytest.raises(ValueError):
        S.set_theme("neon", p)
    assert S.get_theme(p) == new
    S.set_theme("auto", p)
    assert S.get_theme(p) == "auto" and S.showing_theme(p) in S.THEMES
    (tmp_path / "bad.json").write_text(json.dumps({"theme": "neon"}))
    assert S.get_theme(tmp_path / "bad.json") == "midnight"


def test_walterctl_display_theme(tmp_path):
    disp = tmp_path / "display.json"
    disp.write_text(json.dumps({"rotate": 0}))
    code = ("import runpy, sys; sys.argv = ['walterctl'] + sys.argv[1:]; "
            "g = runpy.run_path(%r, run_name='walterctl_test'); g['DISPLAY_JSON'] = %r; "
            "g['display_theme'].__globals__['DISPLAY_JSON'] = %r; g['display_cmd'](sys.argv[1:])"
            % (str(REPO / "bin" / "walterctl"), str(disp), str(disp)))

    def run(*args):
        return subprocess.run([sys.executable, "-c", code, "theme", *args], capture_output=True, text=True,
                              cwd=REPO, timeout=30)
    r = run("list")
    assert r.returncode == 0 and "midnight, light, cream" in r.stdout and "theme: midnight" in r.stdout
    r = run("winter")
    assert r.returncode == 0 and "theme: winter (was midnight)" in r.stdout
    assert json.loads(disp.read_text())["theme"] == "winter"
    assert run("next").returncode == 0 and json.loads(disp.read_text())["theme"] == "sunset"
    r = run("auto")
    assert r.returncode == 0 and "showing" in r.stdout and json.loads(disp.read_text())["theme"] == "auto"
    r = run("neon")
    assert r.returncode != 0 and "unknown theme" in r.stderr
    assert json.loads(disp.read_text())["theme"] == "auto"


def test_route_needs_a_paired_device_and_whitelists(iso):  # noqa: F811
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        (iso / "face").mkdir()
        disp = iso / "face" / "display.json"
        disp.write_text(json.dumps({"rotate": 90}))
        app, apphub, cond, hub = make_app(iso)
        async with TestClient(TestServer(app)) as c:
            assert (await c.get("/api/app/theme", headers=PROXY)).status == 401
            assert (await c.post("/api/app/theme", json={"theme": "fall"}, headers=PROXY)).status == 401
            assert "theme" not in json.loads(disp.read_text())
            code = apphub.store.start_pairing()["code"]
            token = (await (await c.post("/api/app/pair", json={"code": code, "name": "Phone"},
                                         headers=PROXY)).json())["token"]
            auth = {**PROXY, "Authorization": f"Bearer {token}"}
            j = await (await c.get("/api/app/theme", headers=auth)).json()
            assert j["theme"] == "midnight" and j["themes"][0] == "midnight" and len(j["themes"]) == 10
            r = await c.post("/api/app/theme", json={"theme": "Cream"}, headers=auth)
            assert r.status == 200 and (await r.json())["theme"] == "cream"
            d = json.loads(disp.read_text())
            assert d["theme"] == "cream" and d["rotate"] == 90
            r = await c.post("/api/app/theme", json={"theme": "next"}, headers=auth)
            assert (await r.json())["theme"] == "fall"
            for bad in ({"theme": "neon"}, {"theme": "../../etc"}, {"theme": 3}, {}):
                r = await c.post("/api/app/theme", json=bad, headers=auth)
                assert r.status == 400 and (await r.json())["theme"] == "fall"
            # the kiosk on localhost (the face's spoken picks) needs no pairing
            assert (await c.post("/api/app/theme", json={"theme": "auto"})).status == 200
            assert json.loads(disp.read_text())["theme"] == "auto"
    asyncio.run(go())
