"""Presentation mode (walter/present.py): script parser, step sequencing on a fake clock with fake wall
actions, Stop mid-show restores everything, crash recovery, and the /api/app/present endpoint."""
import asyncio
import json
import os
from unittest.mock import MagicMock

import pytest

from walter import display_settings as S
from walter import present as P
from tests.test_app import PROXY, make_app
from tests.test_app import iso as _iso  # noqa: F401  (the base fixture)


@pytest.fixture
def iso(_iso):  # noqa: F811
    (_iso / "face").mkdir(exist_ok=True)
    return _iso


# ---------------------------------------------------------------- the script
def test_default_script_parses_and_fits_a_minute():
    steps = P.parse_script(P.DEFAULT_SCRIPT)
    assert [s["name"] for s in steps] == ["hello", "expressions", "themes", "cockpit", "board", "faces", "goodbye"]
    words = sum(len(__import__("re").sub(r"\[[^\]]*\]", "", s["say"]).split()) for s in steps)
    assert 110 <= words <= 175, words
    themes = steps[2]["screen"]
    assert [c[2] for c in themes] == ["cream", "sunset", "forest", "back"] and themes[1][0] == 3.0
    assert all(s["say"] for s in steps)


def test_default_script_names_nobody():
    low = P.DEFAULT_SCRIPT.lower()
    assert "@" not in low   # no emails in the shipped words
    words = os.path.expanduser("~/.walter/private-words.txt")   # this install's private names, if any
    if os.path.exists(words):
        for w in (x.strip().lower() for x in open(words)):
            assert not w or w not in low.split()


@pytest.mark.parametrize("cmd,out", [
    ("theme cream", (0.0, "theme", "cream")), ("+3 theme back", (3.0, "theme", "back")),
    ("build on", (0.0, "build", "on")), ("+2.5 zoom diff", (2.5, "zoom", "diff")),
    ("layout large", (0.0, "layout", "large")), ("board 14", (0.0, "board", "14")), ("BOARD  off", (0.0, "board", "off"))])
def test_screen_whitelist(cmd, out):
    assert P.parse_screen(cmd) == out


@pytest.mark.parametrize("cmd", ["theme disco", "rm -rf /", "build maybe", "zoom pipeline", "layout huge",
                                 "board 0", "board 61", "+999 theme cream", "theme", "say hi", "+x theme cream", ""])
def test_screen_refuses_junk(cmd):
    with pytest.raises(P.ScriptError):
        P.parse_screen(cmd)


@pytest.mark.parametrize("bad", ["", "# only comments", "say: hi", "## a\nbogus: 1", "## a\nhold: 99",
                                 "## a\ntimeout: 0", "## a\nhold: soon", "## a\nscreen: explode",
                                 "\n".join(f"## s{i}\nsay: x" for i in range(17))])
def test_parse_refuses_broken_scripts(bad):
    with pytest.raises(P.ScriptError):
        P.parse_script(bad)


def test_parse_joins_say_lines_and_sorts_screen():
    s = P.parse_script("# comment\n## one\nsay: Hello\nsay: there [happy]\nscreen: +4 theme ocean\n"
                       "screen: theme cream\nhold: 2\ntimeout: 10\n")[0]
    assert s == {"name": "one", "say": "Hello there [happy]", "hold": 2.0, "timeout": 10.0,
                 "screen": [(0.0, "theme", "cream"), (4.0, "theme", "ocean")]}


def test_load_writes_the_default_and_survives_a_broken_file(tmp_path):
    steps = P.load_script(tmp_path)
    assert (tmp_path / "presentation.md").read_text() == P.DEFAULT_SCRIPT and len(steps) == 7
    (tmp_path / "presentation.md").write_text("## x\nscreen: launch missiles\n")
    assert len(P.load_script(tmp_path)) == 7       # falls back to the built-in show
    (tmp_path / "presentation.md").write_text("## only\nsay: Hi.\n")
    assert [s["name"] for s in P.load_script(tmp_path)] == ["only"]


# ---------------------------------------------------------------- the runner, on a fake clock
class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    async def sleep(self, s):
        self.t += s
        await asyncio.sleep(0)


class FakeWall:
    """Same methods as WallActions; speech 'lasts' talk_s seconds on the fake clock."""

    def __init__(self, clock, talk_s=3.0, fail_screen=None, fail_say=None):
        self.clock, self.talk_s = clock, talk_s
        self.log = []
        self.talk_until = 0.0
        self.fail_screen, self.fail_say = fail_screen, fail_say
        self.began = self.ended = 0
        self.hushes = 0

    def begin(self):
        self.began += 1
        self.log.append(("begin", round(self.clock.t - 1000, 1)))
        S.write_display(demo=True)
        return {"sleeping": False}

    def end(self, was, snap):
        self.ended += 1
        self.log.append(("end", round(self.clock.t - 1000, 1)))
        P.restore_display(snap)

    def say(self, text):
        if self.fail_say and self.fail_say in text:
            raise RuntimeError("tts broke")
        self.log.append(("say", text, round(self.clock.t - 1000, 1)))
        self.talk_until = self.clock.t + self.talk_s

    def speaking(self):
        return self.clock.t < self.talk_until

    def hush(self):
        self.hushes += 1
        self.talk_until = 0.0

    def screen(self, verb, arg, snap):
        if self.fail_screen == verb:
            raise RuntimeError("screen broke")
        self.log.append(("screen", verb, arg, round(self.clock.t - 1000, 1)))
        if verb == "theme":
            S.write_display(theme=snap.get("theme") or "midnight" if arg == "back" else arg)


SCRIPT = """
## one
screen: theme cream
screen: +2 theme back
say: [happy] First.
hold: 1
## two
say: Second.
hold: 5
## three
screen: build on
say: Third.
"""


def presenter(clock, wall, script=SCRIPT):
    return P.Presenter(wall, now=clock.now, sleep=clock.sleep, load=lambda: P.parse_script(script))


def test_steps_run_in_order_with_holds_and_screen_offsets(iso):  # noqa: F811
    S.write_display(theme="ocean", layout="compact")
    clock = Clock()
    wall = FakeWall(clock, talk_s=3.0)
    pr = presenter(clock, wall)

    async def go():
        st = pr.start()
        assert st["running"] and st["steps"] == 3
        assert P.read_state()["display"]["theme"] == "ocean"     # crash-safe snapshot written first
        assert S.read_display()["demo"] is True
        await pr.task
    asyncio.run(go())
    ev = [e for e in wall.log if e[0] in ("say", "screen")]
    assert ev[0] == ("screen", "theme", "cream", 0.0)
    assert ev[1][:2] == ("say", "[happy] First.") and ev[1][2] == 0.0
    assert ev[2] == ("screen", "theme", "back", 2.0)
    # step one ends when the 3 s of speech ends; step two holds 5 s (longer than its speech)
    assert ev[3][:2] == ("say", "Second.") and 3.0 <= ev[3][2] <= 3.2
    assert ev[4][:3] == ("screen", "build", "on") and 8.0 <= ev[4][3] <= 8.4
    assert wall.began == wall.ended == 1 and wall.hushes == 0
    d = S.read_display()
    assert d["theme"] == "ocean" and d["layout"] == "compact" and "demo" not in d
    assert P.read_state() is None
    assert pr.status()["running"] is False and pr.status()["last"]["result"] == "done"


def test_a_failing_step_is_skipped_and_the_show_goes_on(iso):  # noqa: F811
    clock = Clock()
    wall = FakeWall(clock, fail_screen="build", fail_say="Second")
    pr = presenter(clock, wall)

    async def go():
        pr.start()
        await pr.task
    asyncio.run(go())
    said = [e[1] for e in wall.log if e[0] == "say"]
    assert said == ["[happy] First.", "Third."]     # "Second." failed to speak; the build screen failed too
    assert pr.status()["last"]["result"] == "done" and wall.ended == 1


def test_step_timeout_and_total_cap(iso):  # noqa: F811
    clock = Clock()
    wall = FakeWall(clock, talk_s=500.0)           # speech that never ends
    script = "\n".join(f"## s{i}\nsay: Step {i}.\ntimeout: 30" for i in range(10))
    pr = presenter(clock, wall, script)

    async def go():
        pr.start()
        await pr.task
    asyncio.run(go())
    says = [e for e in wall.log if e[0] == "say"]
    assert len(says) == 8                          # 8 x 30 = the 240 s cap
    assert says[1][2] == pytest.approx(30.0, abs=0.3)
    assert clock.t - 1000 <= P.TOTAL_CAP_S + 0.5
    assert wall.hushes >= 3


def test_timeout_still_finishes_the_screen_changes(iso):  # noqa: F811
    S.write_display(theme="forest")
    clock = Clock()
    wall = FakeWall(clock, talk_s=500.0)
    pr = presenter(clock, wall, "## t\nscreen: theme cream\nscreen: +20 theme back\nsay: Hi.\ntimeout: 5")

    async def go():
        pr.start()
        await pr.task
    asyncio.run(go())
    assert ("screen", "theme", "back") == tuple(e[:3] for e in wall.log if e[0] == "screen")[-1]


def test_stop_mid_show_restores_everything(iso):  # noqa: F811
    S.write_display(theme="sunset", build_mode="auto", layout="large", build_seq=4)
    clock = Clock()
    wall = FakeWall(clock, talk_s=10.0)
    pr = presenter(clock, wall)

    async def go():
        pr.start()
        S.write_display(theme="cream", build_mode="on", build_zoom="diff", demo_board_until=123)
        for _ in range(5):
            await asyncio.sleep(0)
        assert pr.running and pr.status()["step"] == 1
        st = await pr.stop()
        assert not st["running"] and st["last"]["result"] == "stopped"
    asyncio.run(go())
    assert wall.hushes == 1 and wall.ended == 1
    d = S.read_display()
    assert d["theme"] == "sunset" and d["build_mode"] == "auto" and d["layout"] == "large"
    assert "demo" not in d and "demo_board_until" not in d and "build_zoom" not in d
    assert d["build_seq"] == 5 and d["build_cmd"] == "mode auto"   # the face re-applies the build mode
    assert P.read_state() is None


def test_only_one_show_at_a_time(iso):  # noqa: F811
    clock = Clock()
    pr = presenter(clock, FakeWall(clock))

    async def go():
        pr.start()
        with pytest.raises(RuntimeError):
            pr.start()
        await pr.stop()
    asyncio.run(go())


def test_crash_recovery_restores_from_the_state_file(iso):  # noqa: F811
    S.write_display(theme="ocean", layout="auto")
    P.save_state({"display": P.snapshot_display(), "started": 1})
    S.write_display(theme="forest", demo=True, build_mode="on", demo_board_until=5)   # the show was mid-way
    assert P.recover() is True
    d = S.read_display()
    assert d["theme"] == "ocean" and "demo" not in d and "build_mode" not in d and "demo_board_until" not in d
    assert P.read_state() is None
    assert P.recover() is False                    # nothing left to do


def test_recover_clears_a_stray_demo_flag(iso):  # noqa: F811
    S.write_display(theme="cream", demo=True)
    assert P.recover() is True
    d = S.read_display()
    assert "demo" not in d and d["theme"] == "cream"


# ---------------------------------------------------------------- the real wall actions (mock conductor)
def test_wall_actions_mute_ears_wake_and_restore(iso):  # noqa: F811
    cond = MagicMock(sleeping=True, state="sleeping", user_muted=True, presenting=False)
    seen = []
    cond.apply_user_mute.side_effect = lambda: seen.append(cond.presenting)
    w = P.WallActions(cond)
    S.write_display(theme="midnight", build_mode="on")
    snap = P.snapshot_display()
    was = w.begin()
    assert cond.presenting is True and seen == [True] and was == {"sleeping": True}
    cond.wake_up.assert_called_once()
    assert S.read_display()["demo"] is True and S.read_display()["chat_view"] == "default"
    w.screen("theme", "sunset", snap)
    w.screen("build", "off", snap)
    w.screen("board", "12", snap)
    d = S.read_display()
    assert d["theme"] == "sunset" and d["build_mode"] == "off" and d["demo_board_until"] > 0
    w.screen("theme", "back", snap)
    assert S.read_display()["theme"] == "midnight"
    w.end(was, snap)
    assert cond.presenting is False and seen == [True, False]
    cond.go_to_sleep.assert_called_once()          # it was asleep before the show
    d = S.read_display()
    assert d["build_mode"] == "on" and "demo" not in d and "chat_view" not in d


def test_conductor_mute_rules_during_the_show():
    from walter.conductor import Conductor
    c = Conductor.__new__(Conductor)
    c.voice = MagicMock(hushed=False)
    c.ears = MagicMock()
    for muted in (False, True):
        c.user_muted, c.presenting = muted, True
        c.ears.reset_mock()
        c.apply_user_mute()
        assert c.voice.hushed is False             # the show is always heard
        c.ears.user_muted.set.assert_called_once()  # and never listens
        c.presenting = False
        c.ears.reset_mock()
        c.apply_user_mute()
        assert c.voice.hushed is muted             # afterwards: the person's own setting
        (c.ears.user_muted.set if muted else c.ears.user_muted.clear).assert_called_once()


# ---------------------------------------------------------------- /api/app/present
def test_routes_need_pairing_and_validate(iso, monkeypatch):  # noqa: F811
    from aiohttp.test_utils import TestClient, TestServer

    (iso / "face").mkdir(exist_ok=True)
    (iso / "home").mkdir(exist_ok=True)
    (iso / "home" / "presentation.md").write_text("## only\nscreen: theme cream\nsay: Hi.\nhold: 30\n")

    async def go():
        cond = MagicMock(state="idle", sleeping=False, user_muted=False, busy=False, speech=None, presenting=False)
        cond.voice = MagicMock(busy=0)
        app, apphub, cond, _ = make_app(iso, conductor=cond)
        async with TestClient(TestServer(app)) as c:
            assert (await c.get("/api/app/present", headers=PROXY)).status == 401
            assert (await c.post("/api/app/present", json={"action": "start"}, headers=PROXY)).status == 401
            cond.voice.say.assert_not_called()
            code = apphub.store.start_pairing()["code"]
            token = (await (await c.post("/api/app/pair", json={"code": code, "name": "Phone"},
                                         headers=PROXY)).json())["token"]
            auth = {**PROXY, "Authorization": f"Bearer {token}"}
            j = await (await c.get("/api/app/present", headers=auth)).json()
            assert j["running"] is False and j["names"] == ["only"]
            for bad in ({"action": "launch"}, {}, {"action": ["start"]}):
                assert (await c.post("/api/app/present", json=bad, headers=auth)).status == 400
            r = await c.post("/api/app/present", data="nope", headers={**auth, "Content-Type": "application/json"})
            assert r.status == 400
            j = await (await c.post("/api/app/present", json={"action": "rehearse"}, headers=auth)).json()
            assert j["ok"] and j["running"] and j["rehearse"]
            assert json.loads((iso / "face" / "display.json").read_text())["demo"] is True
            assert cond.presenting is True
            assert (await c.post("/api/app/present", json={"action": "start"}, headers=auth)).status == 409
            j = await (await c.post("/api/app/present", json={"action": "stop"}, headers=auth)).json()
            assert j["ok"] and not j["running"] and j["last"]["result"] == "stopped"
            assert cond.presenting is False
            d = json.loads((iso / "face" / "display.json").read_text())
            assert "demo" not in d and "theme" not in d
    asyncio.run(go())


def test_split_beats_pauses():
    from walter.present import split_beats
    assert split_beats("Hi. [pause] [wink] Bye. [pause:1.5] Ok.") == [("Hi.", 0.8), ("[wink] Bye.", 1.5), ("Ok.", 0.0)]
    assert split_beats("One. [pause] [pause]") == [("One.", 1.6)]
    assert split_beats("x [pause:9] y") == [("x", 4.0), ("y", 0.0)]
    assert split_beats("") == []
