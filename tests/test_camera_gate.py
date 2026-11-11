"""Sound-triggered camera sleep: the CameraGate state machine and its glue,
with synthetic sound levels and detections (no camera, no microphone)."""
import asyncio
import time

from walter.config import Config
from walter.conductor import Conductor
from walter.presence import CameraGate, Observation, Presence, PresenceTracker

G = CameraGate


def gate(**kw):
    g = CameraGate(**kw)
    g.allow(0.0, True)
    return g


def run_quiet(g, t0, t1, present=False, unseen0=0.0, step=2.0):
    """Tick with nobody seen since t0 - unseen0 and a silent mic."""
    t = t0
    while t <= t1:
        g.tick(t, present, unseen0 + (t - t0))
        t += step
    return t


def test_starts_watching_and_off_when_not_allowed():
    g = CameraGate()
    assert g.state == G.OFF and not g.camera_wanted
    assert g.allow(10, True) and g.state == G.WATCHING and g.camera_wanted
    assert g.allow(20, False, "quiet hours") and g.state == G.OFF and g.reason == "quiet hours"
    # quiet hours: sounds never wake it
    assert not g.sound(30, 20.0) and not g.voice(31, strong=True) and g.state == G.OFF


def test_sleeps_after_five_quiet_minutes_without_anyone():
    g = gate()
    run_quiet(g, 0, 290)
    assert g.state == G.WATCHING                      # not 5 minutes yet
    run_quiet(g, 292, 310, unseen0=292)
    assert g.state == G.SLEEPING and not g.camera_wanted
    assert "nobody seen" in g.reason


def test_no_sleep_while_someone_present_or_room_noisy():
    g = gate()
    run_quiet(g, 0, 2000, present=True)              # someone sitting there: keep watching
    assert g.state == G.WATCHING
    g = gate()
    t = 0.0
    while t < 600:                                    # nobody seen, but sounds keep coming
        g.sound(t, 2.5)
        g.tick(t, False, t)
        t += 10
    assert g.state == G.WATCHING
    g.sound(905, 2.5)
    g.tick(910, False, 910)                           # 15 min unseen: sleep anyway (TV/radio)
    assert g.state == G.SLEEPING and "background noise" in g.reason


def test_sleep_can_be_disabled():
    g = gate(sleep_enabled=False)
    run_quiet(g, 0, 3000)
    assert g.state == G.WATCHING


def asleep(**kw):
    g = gate(**kw)
    run_quiet(g, 0, 400)
    assert g.state == G.SLEEPING
    return g


def test_single_blip_or_hum_does_not_wake():
    g = asleep()
    assert not g.sound(500, 4.0)                      # one click
    assert not g.sound(510, 4.0)                      # another, 10 s later: not a pair
    for i in range(100):                              # fan hum just above the floor
        assert not g.sound(600 + i * 0.25, 1.6)
    assert g.state == G.SLEEPING


def test_footsteps_bang_or_voice_wake_to_verify():
    g = asleep()
    assert not g.sound(500, 3.5)
    assert g.sound(500.5, 3.2)                        # two loud moments within 2 s
    assert g.state == G.VERIFYING and g.camera_wanted and "background" in g.reason
    g = asleep()
    assert g.sound(500, 12.0) and g.state == G.VERIFYING   # a door slam
    g = asleep()
    assert g.voice(500) and g.state == G.VERIFYING


def test_verify_finds_someone_then_watches():
    g = asleep()
    g.sound(500, 10)
    g.tick(505, False, 505)
    assert g.state == G.VERIFYING
    assert g.tick(510, True, 0) and g.state == G.WATCHING and "verified" in g.reason


def test_verify_finds_nobody_back_to_sleep_with_growing_cooldown():
    g = asleep(verify_s=45, cooldown_s=60)
    g.sound(500, 10)
    g.tick(530, False, 530)
    assert g.state == G.VERIFYING
    assert g.tick(546, False, 546) and g.state == G.SLEEPING and "nobody confirmed" in g.reason
    # cooldown 60 s: the TV's next outburst is ignored...
    assert not g.sound(560, 10) and not g.voice(570) and g.state == G.SLEEPING
    # ...but his name always counts
    assert g.voice(580, strong=True) and g.state == G.VERIFYING
    g.tick(630, False, 630)
    assert g.state == G.SLEEPING                      # second miss: cooldown 120 s
    assert not g.sound(700, 10)
    assert not g.sound(740, 10)
    assert g.sound(760, 10) and g.state == G.VERIFYING   # 130 s later: allowed again
    g.tick(761, True, 0)                                 # someone was there: cooldown resets
    assert g.state == G.WATCHING and g.fails == 0


def test_cooldown_is_capped():
    g = asleep(cooldown_s=60, max_cooldown_s=600)
    t = 500.0
    for _ in range(8):
        g.voice(t, strong=True)
        t += 50
        g.tick(t, False, t)
        assert g.state == G.SLEEPING
    assert g.cooldown_until - t <= 600


def test_frame_rate_by_situation():
    g = gate(base_period=1.0, talk_period=2.0, still_period=5.0, still_after_s=600)
    assert g.period(0, present=False) == 1.0          # looking for someone
    g.voice(100)
    assert g.period(150, present=True) == 2.0         # someone there, talking
    assert g.period(800, present=True) == 5.0         # there and quiet for 10+ min
    g.sound(805, 2.5)
    assert g.period(806, present=True) == 2.0
    g = asleep()
    g.sound(500, 10)
    assert g.period(501, present=True) == 1.0         # verifying: full rate


def test_status_fields():
    g = asleep()
    st = g.status(450)
    assert st["camera_state"] == "sleeping" and st["camera_reason"]
    g.sound(500, 10)
    st = g.status(510)
    assert st["camera_state"] == "verifying" and 0 < st["verify_left_s"] <= 45


# ------------------------------------------------------------- glue
class FakeCamera:
    def __init__(self):
        self.want, self.resume, self.period = False, None, None

    def set_want(self, on, resume=False):
        if on and not self.want:
            self.resume = resume
        self.want = on

    def set_period(self, s):
        self.period = s


def make(tmp_path):
    sent = []
    cfg = Config()
    cfg.senses.camera = True
    c = Conductor(cfg, brain=None, send=sent.append)
    c.in_quiet_hours = lambda: False
    p = Presence(cfg, c, sent.append)
    p.camera = FakeCamera()
    c.presence = p
    return c, p, sent


def test_glue_sleep_wake_and_arrival(tmp_path):
    async def go():
        c, p, sent = make(tmp_path)
        t0 = time.time()
        p.tick(t0)
        assert p.gate.state == G.WATCHING and p.camera.want
        p._active(True)                                   # frames flowing
        p.tracker.reset(t0 - 700)                         # nobody seen for ~12 min
        p._observed(Observation(t0, False))
        assert p.gate.state == G.SLEEPING and not p.camera.want   # released
        p._active(False)                                  # camera thread closed the device
        assert {"t": "camera", "on": False, "state": "sleeping"} in sent
        st = p.status()
        assert st["camera_state"] == "sleeping" and st["camera"].startswith("sleeping")
        # sleeping still counts time: the room goes "empty" -> doze
        p.tick(t0 + 250)
        assert p.tracker.empty and c.dozing
        # a door: verify, keeping what we knew (so it counts as an arrival)
        c.on_hear_sound(10.0)
        assert p.gate.state == G.VERIFYING and p.camera.want and p.camera.resume
        p._active(True)
        assert p.tracker.last_seen is None and p.tracker.unseen_for(time.time()) >= 690   # not reset by the wake
        now = time.time()
        p._observed(Observation(now, True, 0.4))
        p._observed(Observation(now + 1, True, 0.4))
        assert p.gate.state == G.WATCHING
        assert not c.dozing and c.inbox.get_nowait()[0] == "presence"
    asyncio.run(go())


def test_glue_verify_ignores_tv_speech(tmp_path):
    async def go():
        c, p, _ = make(tmp_path)
        t0 = time.time()
        p.tick(t0)
        p._active(True)
        p.tracker.reset(t0 - 400)
        p._observed(Observation(t0, False))
        assert p.gate.state == G.SLEEPING
        p._active(False)
        c.on_hear_start()                                 # voice activity (a TV?)
        assert p.gate.state == G.VERIFYING
        p._active(True)
        c.last_speech = time.time()                       # a transcript arrived
        for i in range(5):                                # but the camera sees nobody
            p._observed(Observation(time.time() + i, False))
        assert not p.tracker.present
        p.tick(time.time() + 60)                          # verify window over
        assert p.gate.state == G.SLEEPING and not p.camera.want
        c.on_hear_start()                                 # more TV: cooldown, stays asleep
        assert p.gate.state == G.SLEEPING
        c.wake_up()                                       # his name: look anyway
        assert p.gate.state == G.VERIFYING
    asyncio.run(go())


def test_glue_quiet_hours_off(tmp_path):
    c, p, _ = make(tmp_path)
    p.tick()
    assert p.camera.want
    c.in_quiet_hours = lambda: True
    p.tick()
    assert p.gate.state == G.OFF and not p.camera.want
    c.on_hear_sound(20.0)
    assert p.gate.state == G.OFF
    assert p.status()["camera"] == "off (quiet hours)"
