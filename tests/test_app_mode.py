"""Companion app mode switcher: walter/app_mode.py + /api/app/mode. All state is fake: display.json
and the focus box live under the iso fixture's temp dirs; the conductor and hub are mocks."""
import asyncio
import json
from unittest.mock import MagicMock

import pytest

from walter import app_mode as M
from walter import display_settings as S
from tests.test_app import PROXY, iso, make_app  # noqa: F401  (iso is a fixture)


@pytest.mark.parametrize("bad", [
    None, [], {}, {"mode": "party"}, {"mode": "BOARD"}, {"mode": 3}, {"mode": "build", "zoom": "rm -rf"},
    {"mode": "build", "zoom": "pipeline"}, {"mode": "build", "phase": "ship"},
    {"mode": "focus", "task": "x"}, {"mode": "focus", "task": "x", "minutes": 0},
    {"mode": "focus", "task": "x", "minutes": 241}, {"mode": "focus", "task": "x", "minutes": 2.5},
    {"mode": "focus", "task": "x", "minutes": True}, {"mode": "focus", "task": "x", "minutes": "25"},
    {"mode": "focus", "task": "x", "minutes": float("nan")}, {"mode": "focus", "task": "  ", "minutes": 5},
    {"mode": "focus", "task": "a" * 81, "minutes": 5}, {"mode": "focus", "task": 7, "minutes": 5},
    {"mode": "board", "end_focus": "explode"}])
def test_parse_refuses_junk(bad):
    with pytest.raises(M.ModeError):
        M.parse(bad)


def test_parse_accepts_the_whitelist():
    assert M.parse({"mode": "build", "zoom": "diff", "phase": "plan"}) == {"mode": "build", "zoom": "diff",
                                                                          "phase": "plan"}
    assert M.parse({"mode": "focus", "task": " Network app ", "minutes": 25}) == {
        "mode": "focus", "task": "Network app", "minutes": 25}
    assert M.parse({"mode": "focus", "task": "a" * 80, "minutes": 240})["minutes"] == 240
    assert M.parse({"mode": "sleep", "zoom": "diff"}) == {"mode": "sleep"}   # extras ignored off-mode
    for m in M.MODES:
        if m != "focus":
            assert M.parse({"mode": m})["mode"] == m


def test_chat_hidden_without_the_chat_view(tmp_path):
    (tmp_path / "face").mkdir()
    assert "chat" not in M.capabilities(tmp_path)
    (tmp_path / "face" / "chat.js").write_text("")
    assert "chat" in M.capabilities(tmp_path)


def switcher(iso, sleeping=False):  # noqa: F811
    cond = MagicMock(state="idle", sleeping=sleeping, user_muted=False)
    hub = MagicMock()
    return M.ModeSwitcher(cond, hub, repo=iso), cond, hub


def test_modes_write_what_walterctl_writes(iso):  # noqa: F811
    (iso / "face").mkdir(exist_ok=True)
    sw, cond, hub = switcher(iso)
    assert sw.state()["mode"] == "board" and "chat" not in sw.state()["capabilities"]
    assert sw.apply({"mode": "face"})["mode"] == "face"
    assert S.read_display()["layout"] == "compact"
    st = sw.apply({"mode": "build", "phase": "plan", "zoom": "tests"})
    d = S.read_display()
    assert st["mode"] == "build" and d["build_mode"] == "on" and d["build_phase"] == "plan"
    assert d["build_zoom"] == "tests" and d["build_seq"] == 3 and d["build_cmd"] == "zoom tests"
    sw.apply({"mode": "build", "zoom": "back"})
    assert S.read_display()["build_zoom"] == ""
    st = sw.apply({"mode": "conversation"})
    assert st["mode"] == "conversation" and S.read_display()["build_mode"] == "auto"
    assert hub.send.call_args[0][0] == {"t": "convo", "open": True}
    with pytest.raises(M.ModeError) as e:
        sw.apply({"mode": "chat"})          # no face/chat.js in this repo
    assert e.value.status == 409
    (iso / "face" / "chat.js").write_text("")
    assert sw.apply({"mode": "chat"})["mode"] == "chat" and S.read_display()["chat_view"] == "chat"
    st = sw.apply({"mode": "board"})
    assert st["mode"] == "board" and S.read_display()["chat_view"] == "default"
    assert S.read_display()["layout"] == "large" and hub.send.call_args[0][0]["open"] is False
    sw.apply({"mode": "sleep"})
    cond.go_to_sleep.assert_called_once()
    cond.sleeping = True
    assert sw.state()["mode"] == "sleep"
    sw.apply({"mode": "face"})
    cond.wake_up.assert_called_once()


def test_focus_box_is_never_ended_silently(iso):  # noqa: F811
    sw, _, _ = switcher(iso)
    st = sw.apply({"mode": "focus", "task": "Network app", "minutes": 25})
    assert st["mode"] == "focus" and st["focus"]["task"] == "Network app"
    with pytest.raises(M.ModeError) as e:
        sw.apply({"mode": "build"})
    assert e.value.status == 409 and e.value.code == "focus_running"
    assert sw.state()["mode"] == "focus"
    assert sw.apply({"mode": "build", "end_focus": "park"})["mode"] == "build"
    assert sw.state()["focus"] is None


def test_routes_need_pairing_and_validate(iso):  # noqa: F811
    from aiohttp.test_utils import TestClient, TestServer

    (iso / "face").mkdir(exist_ok=True)

    async def go():
        app, apphub, cond, _ = make_app(iso)
        async with TestClient(TestServer(app)) as c:
            assert (await c.get("/api/app/mode", headers=PROXY)).status == 401
            assert (await c.post("/api/app/mode", json={"mode": "sleep"}, headers=PROXY)).status == 401
            cond.go_to_sleep.assert_not_called()
            code = apphub.store.start_pairing()["code"]
            token = (await (await c.post("/api/app/pair", json={"code": code, "name": "Phone"},
                                         headers=PROXY)).json())["token"]
            auth = {**PROXY, "Authorization": f"Bearer {token}"}
            j = await (await c.get("/api/app/mode", headers=auth)).json()
            assert j["mode"] in M.MODES and "board" in j["capabilities"]
            r = await c.post("/api/app/mode", json={"mode": "disco"}, headers=auth)
            assert r.status == 400 and "mode" in (await r.json())["error"]
            r = await c.post("/api/app/mode", json={"mode": "focus", "task": "x", "minutes": 999}, headers=auth)
            assert r.status == 400
            r = await c.post("/api/app/mode", data="nope", headers={**auth, "Content-Type": "application/json"})
            assert r.status == 400
            r = await c.post("/api/app/mode", json={"mode": "build", "zoom": "graph"}, headers=auth)
            j = await r.json()
            assert r.status == 200 and j["mode"] == "build" and j["build"]["zoom"] == "graph"
            assert json.loads((iso / "face" / "display.json").read_text())["build_mode"] == "on"
    asyncio.run(go())


# ---- T47: typed "dev mode" / "exit dev mode" in the app flips the build cockpit
@pytest.mark.parametrize("text", ["dev mode", "Developer mode", "go to dev mode", "Go into dev mode please",
                                  "switch to dev mode", "start dev mode", "build mode", "let's build"])
def test_build_phrase_on(text):
    assert M.build_phrase(text) == "on"


@pytest.mark.parametrize("text", ["exit dev mode", "Leave dev mode.", "done with dev mode", "I'm done with dev mode",
                                  "exit build mode", "done building", "turn off dev mode"])
def test_build_phrase_off(text):
    assert M.build_phrase(text) == "off"


@pytest.mark.parametrize("text", ["what's the weather", "development of the site is going well",
                                  "can you explain what dev mode does and why the board shows three ready tasks"])
def test_build_phrase_none(text):
    assert M.build_phrase(text) is None


def test_typed_dev_mode_switches_the_wall(iso):  # noqa: F811
    (iso / "face").mkdir(exist_ok=True)
    sw, cond, hub = switcher(iso)
    assert sw.typed("dev mode") == "on"
    assert S.read_display()["build_mode"] == "on"
    assert sw.typed("hello there") is None
    assert S.read_display()["build_mode"] == "on"
    assert sw.typed("exit dev mode") == "off"
    assert S.read_display()["build_mode"] == "auto"
