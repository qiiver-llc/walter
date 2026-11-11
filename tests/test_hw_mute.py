"""The mic's own mute button (JLab GO Talk): no HID/ALSA event, just perfect
digital silence. A live mic, even in a silent room, always has some noise."""
import numpy as np

from walter.ears import FRAME, HwMuteDetector, dead_frame

from test_conductor import make
from test_live import make_ears

ZERO = np.zeros(FRAME, np.int16).tobytes()
rng = np.random.default_rng(7)


def quiet_room():
    # like the owner's JLab in a silent room: std ~0.6 LSB, mostly -1..1, peaks of 2-3
    return np.clip(np.round(rng.normal(0, 0.65, FRAME)), -3, 3).astype(np.int16).tobytes()


def speech():
    return (np.sin(np.arange(FRAME) / 5) * 3000).astype(np.int16).tobytes()


def feed(d, frame_fn, n):
    for _ in range(n):
        d.feed(frame_fn() if callable(frame_fn) else frame_fn)
    return d.muted


def test_dead_frame():
    assert dead_frame(ZERO)
    assert dead_frame(np.full(FRAME, -1, np.int16).tobytes())   # a tiny DC offset, no variance
    assert not dead_frame(np.full(FRAME, 50, np.int16).tobytes())  # big DC: not a muted mic
    assert not dead_frame(b"")
    one = np.zeros(FRAME, np.int16); one[100] = 1               # a single LSB of noise = alive
    assert not dead_frame(one.tobytes())
    assert not any(dead_frame(quiet_room()) for _ in range(2000))


def test_turns_on_after_0_8s_of_zeros_and_off_after_0_3s_of_signal():
    flips = []
    d = HwMuteDetector(flips.append)
    assert not feed(d, ZERO, 26)          # 0.78 s: not yet
    assert feed(d, ZERO, 1)               # 0.81 s: muted
    assert flips == [True]
    assert feed(d, ZERO, 100)             # stays muted, no repeat callbacks
    assert feed(d, speech, 9)             # 0.27 s of signal: still muted
    assert not feed(d, speech, 1)         # 0.3 s: cleared
    assert flips == [True, False]


def test_quiet_room_never_triggers():
    flips = []
    d = HwMuteDetector(flips.append)
    assert not feed(d, quiet_room, 2000)   # a minute of a silent room
    assert flips == []


def test_short_zero_gaps_and_blips_dont_flip():
    flips = []
    d = HwMuteDetector(flips.append)
    for _ in range(50):                    # stream glitches: a few zero frames between noise
        feed(d, ZERO, 20); feed(d, quiet_room, 1)
    assert flips == []
    feed(d, ZERO, 30)
    feed(d, speech, 3); feed(d, ZERO, 1)   # a click while muted doesn't unmute
    assert d.muted and flips == [True]


def test_ears_skip_capture_while_hw_muted_without_touching_user_mute():
    e, _ = make_ears()
    got = []
    e.on_hw_mute = got.append
    e._emit = lambda fn, *a: fn(*a)
    pcm = quiet_room()
    assert e._capture(pcm) == pcm
    for _ in range(30):
        e._hw.feed(ZERO)
    assert e.hw_muted.is_set() and got == [True]
    assert e._capture(pcm) == b""          # no VAD / whisper work on silence
    assert not e.user_muted.is_set()       # the person's (software) mute is untouched
    for _ in range(10):
        e._hw.feed(speech())
    assert not e.hw_muted.is_set() and got == [True, False]
    assert e._capture(pcm) == pcm
    e.stop()


def test_raw_tap_device_watches_real_mic_behind_echo_canceller():
    e, _ = make_ears()
    e.cfg.ears.device = ""
    assert e._raw_tap_device() is None     # ears read the raw mic: watch that stream
    e.cfg.ears.device = e.cfg.ears.aec_device
    assert e._raw_tap_device() == "default"  # AEC output may be gated: watch the real mic
    e.stop()


def test_conductor_broadcasts_hw_mute_and_leaves_software_mute_alone():
    c, sent = make()
    c.on_hw_mute(True)
    assert c.hw_muted and sent[-1] == {"t": "hwmute", "on": True}
    assert c.user_muted is False
    n = len(sent)
    c.on_hw_mute(True)                     # no duplicate broadcasts
    assert len(sent) == n
    c.on_hw_mute(False)
    assert not c.hw_muted and sent[-1] == {"t": "hwmute", "on": False}
