"""Presence state machine + conductor reactions (no camera needed)."""
import asyncio
import time

from walter.config import Config
from walter.conductor import Conductor
from walter.presence import Observation, Presence, PresenceTracker


def feed(tr, t, seen, x=None):
    return [e["e"] for e in tr.observe(Observation(t, seen, x))]


def tracker(**kw):
    tr = PresenceTracker(**kw)
    tr.reset(0.0)
    return tr


def test_single_flicker_is_not_a_person():
    tr = tracker()
    assert feed(tr, 700, True) == []
    assert feed(tr, 720, True) == []      # too far apart to confirm
    assert not tr.present


def test_arrival_after_long_absence_greets_once():
    tr = tracker()
    feed(tr, 1, True); feed(tr, 2, True)  # someone there at the start (not an arrival: just started)
    assert tr.present
    t = 2
    for t in range(3, 3 + 1200, 10):     # 20 minutes of nobody
        feed(tr, t, False)
    assert tr.empty and not tr.present
    feed(tr, t + 1, True)
    evs = tr.observe(Observation(t + 2, True, 0.3))
    assert [e["e"] for e in evs] == ["arrival"]
    assert evs[0]["greet"] and evs[0]["away_s"] >= 600 and evs[0]["x"] == 0.3
    assert not evs[0]["since_start"]


def test_short_absence_is_not_an_arrival():
    tr = tracker()
    feed(tr, 1, True); feed(tr, 2, True)
    for t in range(3, 300, 10):           # 5 minutes away
        feed(tr, t, False)
    assert not tr.present and not tr.empty
    feed(tr, 301, True)
    assert feed(tr, 302, True) == ["present"]


def test_greeting_cooldown():
    tr = tracker(greet_cooldown_s=1800)

    def away_then_back(t0, mins):
        for t in range(t0, t0 + mins * 60, 10):
            feed(tr, t, False)
        t = t0 + mins * 60
        feed(tr, t, True)
        return tr.observe(Observation(t + 1, True))

    first = away_then_back(0, 11)          # arrives after 11 min (since camera start)
    assert first[0]["e"] == "arrival" and first[0]["greet"] and first[0]["since_start"]
    second = away_then_back(700, 11)       # arrives again 12 min later: perk, but no greeting
    assert second[0]["e"] == "arrival" and not second[0]["greet"]
    third = away_then_back(2700, 11)       # well past 30 minutes since the first greeting
    assert third[0]["greet"]


def test_empty_fires_once_after_15_minutes():
    tr = tracker()
    feed(tr, 1, True); feed(tr, 2, True)
    evs = []
    for t in range(3, 2000, 10):
        evs += feed(tr, t, False)
    assert evs.count("empty") == 1 and evs.count("away") == 1
    assert tr.status(2000)["room_empty"]


def test_reset_forgets_old_sightings():
    tr = tracker()
    feed(tr, 1, True); feed(tr, 2, True)
    tr.reset(5000)                         # camera restarted (e.g. after quiet hours)
    assert not tr.present and tr.unseen_for(5000) == 0


# ------------------------------------------------------------ conductor side
class FakeVoice:
    def __init__(self):
        self.said, self.generation, self.hushed = [], 0, False

    def say(self, t):
        self.said.append(t)

    def stop(self):
        self.generation += 1


def make():
    sent = []
    c = Conductor(Config(), brain=None, send=sent.append)
    c.voice = FakeVoice()
    c.in_quiet_hours = lambda: False
    return c, sent


def test_doze_and_wake_on_arrival():
    async def go():
        c, sent = make()
        c.doze()
        assert c.dozing and c.state == "sleeping"
        assert not c.user_muted                       # daytime doze never mutes
        c.on_presence_arrival({"e": "arrival", "away_s": 1500, "greet": True, "x": 0.2})
        assert not c.dozing and c.state == "idle"
        assert {"t": "action", "name": "perk"} in sent
        look = [m for m in sent if m["t"] == "look"][0]
        assert look["x"] > 0                          # left of the image = his right
        src, text = c.inbox.get_nowait()
        assert src == "presence" and "25 minutes" in text
        assert "[silent]" in c._frame(src, text)
    asyncio.run(go())


def test_no_greeting_when_muted_busy_or_not_allowed():
    async def go():
        c, _ = make()
        c.on_presence_arrival({"e": "arrival", "away_s": 900, "greet": False})
        c.user_muted = True
        c.on_presence_arrival({"e": "arrival", "away_s": 900, "greet": True})
        c.user_muted = False
        c.last_heard = time.time()                    # already talking with someone
        c.on_presence_arrival({"e": "arrival", "away_s": 900, "greet": True})
        assert c.inbox.empty()
    asyncio.run(go())


def test_no_doze_while_talking_or_busy():
    async def go():
        c, _ = make()
        c.last_heard = time.time()
        c.doze()
        assert not c.dozing
        c.last_heard = 0
        c.busy = True
        c.doze()
        assert not c.dozing
    asyncio.run(go())


def test_voice_wakes_from_doze():
    async def go():
        c, _ = make()
        c.doze()
        c.on_heard("Walter, what time is it?")
        assert not c.dozing and c.state == "listening"
    asyncio.run(go())


def test_presence_status_disabled_by_default():
    c, sent = make()
    p = Presence(Config(), c, sent.append)
    p._apply()                                        # camera = false: nothing starts
    assert p.camera is None and p.status()["camera"] == "disabled"


def test_presence_glue_events():
    async def go():
        c, sent = make()
        cfg = Config()
        cfg.senses.camera = True
        p = Presence(cfg, c, sent.append)
        p._active(True)                               # pretend frames are flowing
        assert any(m["t"] == "camera" and m["on"] is True for m in sent)
        t0 = time.time()
        p.tracker.reset(t0 - 2000)
        p._observed(Observation(t0, False))           # nobody for 2000 s -> empty -> doze
        assert c.dozing
        p._observed(Observation(t0 + 1, True))
        p._observed(Observation(t0 + 2, True, 0.5))   # confirmed arrival
        assert not c.dozing and c.inbox.get_nowait()[0] == "presence"
        p._active(False)
        assert any(m["t"] == "camera" and m["on"] is False for m in sent)
    asyncio.run(go())


def test_no_greeting_during_a_focus_box():
    async def go():
        from walter import focus
        c, sent = make()
        focus.FocusStore().start("Deep work", 25)
        c.on_presence_arrival({"e": "arrival", "away_s": 1500, "greet": True})
        assert c.inbox.empty()                        # heads-down: no spontaneous hello
        assert {"t": "action", "name": "perk"} in sent  # the face still notices (tiny move)
    asyncio.run(go())
