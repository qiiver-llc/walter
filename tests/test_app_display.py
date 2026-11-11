"""Wall text size from the companion app: walter/display_settings.py + /api/app/display.
Everything writes to a temp face/display.json (the iso fixture points dashboard.FACE_DIR there)."""
import asyncio
import json
import os

import pytest

from walter import display_settings as S
from tests.test_app import PROXY, iso, make_app  # noqa: F401  (iso is a fixture)


def test_parse_accepts_the_range_and_rounds():
    assert S.parse_text_scale(0.5) == 0.5 and S.parse_text_scale("1.5") == 1.5
    assert S.parse_text_scale(0.8333) == 0.83 and S.parse_text_scale(1) == 1.0


@pytest.mark.parametrize("bad", [0.49, 1.51, -1, "big", "", None, True, float("nan"), float("inf"), [1], {}])
def test_parse_refuses_junk(bad):
    with pytest.raises(ValueError):
        S.parse_text_scale(bad)


def test_set_keeps_rotation_and_layout_and_never_looks_like_an_edit(tmp_path):
    p = tmp_path / "display.json"
    p.write_text(json.dumps({"rotate": 90, "layout": "large", "set": 1}))
    assert S.get_text_scale(p) == S.TEXT_SCALE_DEFAULT == 0.8
    assert S.set_text_scale(1.2, p) == (1.2, 0.8)
    d = json.loads(p.read_text())
    assert d["rotate"] == 90 and d["layout"] == "large" and d["text_scale"] == 1.2 and d["set"] > 1
    assert os.stat(p).st_mtime < 1e9            # 2000-01-01: the face's edit watcher ignores it
    assert not (tmp_path / "display.json.tmp").exists()
    with pytest.raises(ValueError):
        S.set_text_scale(9, p)
    assert json.loads(p.read_text())["text_scale"] == 1.2   # a refusal changes nothing


def test_missing_or_broken_file_gives_the_default(tmp_path):
    assert S.get_text_scale(tmp_path / "nope.json") == 0.8
    (tmp_path / "bad.json").write_text("{not json")
    assert S.get_text_scale(tmp_path / "bad.json") == 0.8
    (tmp_path / "odd.json").write_text(json.dumps({"text_scale": 7}))
    assert S.get_text_scale(tmp_path / "odd.json") == 0.8
    S.set_text_scale(0.6, tmp_path / "new.json")            # creates it
    assert json.loads((tmp_path / "new.json").read_text())["text_scale"] == 0.6


def test_route_needs_a_paired_device_and_validates(iso):  # noqa: F811
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        (iso / "face").mkdir()
        disp = iso / "face" / "display.json"
        disp.write_text(json.dumps({"rotate": 90}))
        app, apphub, cond, hub = make_app(iso)
        async with TestClient(TestServer(app)) as c:
            assert (await c.get("/api/app/display", headers=PROXY)).status == 401
            assert (await c.post("/api/app/display", json={"text_scale": 1}, headers=PROXY)).status == 401
            assert "text_scale" not in json.loads(disp.read_text())
            code = apphub.store.start_pairing()["code"]
            token = (await (await c.post("/api/app/pair", json={"code": code, "name": "Phone"},
                                         headers=PROXY)).json())["token"]
            auth = {**PROXY, "Authorization": f"Bearer {token}"}
            j = await (await c.get("/api/app/display", headers=auth)).json()
            assert j == {"text_scale": 0.8, "default": 0.8, "min": 0.5, "max": 1.5}
            r = await c.post("/api/app/display", json={"text_scale": 1.25}, headers=auth)
            assert r.status == 200 and (await r.json())["text_scale"] == 1.25
            d = json.loads(disp.read_text())
            assert d["text_scale"] == 1.25 and d["rotate"] == 90
            for bad in ({"text_scale": 2}, {"text_scale": "1; rm -rf /"}, {"text_scale": True}, {}):
                r = await c.post("/api/app/display", json=bad, headers=auth)
                assert r.status == 400 and (await r.json())["text_scale"] == 1.25
            r = await c.post("/api/app/display", data="text_scale=1", headers={**auth, "Content-Type": "text/plain"})
            assert r.status == 415
            assert json.loads(disp.read_text())["text_scale"] == 1.25
            # the kiosk on localhost needs no pairing, like the other wall controls
            assert (await c.get("/api/app/display")).status == 200
    asyncio.run(go())


def test_app_has_the_control():
    from pathlib import Path
    app = Path(__file__).resolve().parents[1] / "app"
    html, js = (app / "index.html").read_text(), (app / "app.js").read_text()
    assert 'id="tsRange"' in html and 'min="50"' in html and 'max="150"' in html
    assert 'id="tsVal"' in html and 'id="tsReset"' in html
    assert '"/api/app/display"' in js and "Needs one restart to enable" in js
    assert 'addEventListener("change"' in js       # applies when the finger lifts
