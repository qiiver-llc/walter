"""While Walter talks, barge-in checks hear only the newest audio and are gated by
how loud his own echo usually is (ears.py _barge_check); whisper runs niced."""
import os
import threading
import time

import numpy as np

from walter.config import Config
from walter.ears import ECHO_MIN_FRAMES, FRAME, FRAME_MS, Ears, frame_rms, load_whisper

from test_live import FakeModel, SyncLoop, wait_idle


def tone(level: float, n: int) -> list[bytes]:
    """n frames of a sine at roughly this RMS."""
    t = np.arange(FRAME) / 16000
    f = (np.sin(2 * np.pi * 220 * t) * level * np.sqrt(2)).astype(np.int16).tobytes()
    return [f] * n


class LenModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.lens = []

    def transcribe(self, audio, **kw):
        self.lens.append(len(audio) / 16000)
        return super().transcribe(audio, **kw)


def make(echo_level=None):
    got = {"partial": []}
    e = Ears(Config(), on_text=lambda t: None, on_hear=lambda v: None, on_start=lambda: None,
             on_partial=got["partial"].append)
    e.model, e.loop, e._running = LenModel(), SyncLoop(), True
    threading.Thread(target=e._partial_worker, daemon=True).start()
    if echo_level is not None:
        e._echo_hist.extend([echo_level] * (ECHO_MIN_FRAMES + 10))
    e.speaking.set()
    e._open_utterance()
    return e, got


def test_tone_level():
    assert abs(frame_rms(tone(1000, 1)[0]) - 1000) < 20


def test_barge_check_hears_only_the_last_window():
    e, got = make()                         # no echo level yet: not gated
    voiced = tone(800, 400)                 # 12 s of "echo"
    e._maybe_partial(voiced)
    wait_idle(e)
    assert e.model.lens == [e.cfg.ears.barge_in_window_s]
    assert got["partial"]                   # the conductor still gets text to judge
    job = e._latest
    assert job.trimmed and job.frames == 400
    assert e._close_utterance(400) is None  # a tail is never reused as the final transcript
    e.stop()


def test_short_utterance_is_not_trimmed():
    e, _ = make()
    e._maybe_partial(tone(800, 50))         # 1.5 s: shorter than the window
    wait_idle(e)
    assert abs(e.model.lens[0] - 1.5) < 1e-6 and not e._latest.trimmed
    e.stop()


def test_quiet_echo_is_skipped_loud_voice_checked_at_once():
    e, _ = make(echo_level=500)
    e._barge_check_at = time.monotonic()    # a check just ran
    e._maybe_partial(tone(550, 60))         # as loud as his echo: skipped
    wait_idle(e)
    assert e.model.lens == [] and e.barge_skipped == 1
    e._maybe_partial(tone(500, 54) + tone(1500, 6))   # just the onset of a word: not yet
    assert e._pending is None and e._current is None and e.model.lens == []
    e._maybe_partial(tone(500, 40) + tone(1500, 20))  # ~0.6 s of someone close to the mic: now
    wait_idle(e)
    assert len(e.model.lens) == 1
    e.stop()


def test_quiet_audio_still_rechecked_now_and_then():
    e, _ = make(echo_level=500)
    e._barge_check_at = time.monotonic() - e.cfg.ears.barge_in_recheck_s - 0.1
    e._maybe_partial(tone(550, 60))
    wait_idle(e)
    assert len(e.model.lens) == 1           # the safety net for soft voices
    e.stop()


def test_gate_off_and_long_echo_still_checked():
    e, _ = make(echo_level=500)
    e.cfg.ears.barge_in_energy_ratio = 0
    e._barge_check_at = time.monotonic()
    voiced = tone(500, 700)                 # 21 s: past partial_max_s, still checked while he talks
    e._maybe_partial(voiced)
    wait_idle(e)
    assert e.model.lens == [e.cfg.ears.barge_in_window_s]
    e.stop()


def test_not_speaking_uses_whole_utterance():
    e, _ = make(echo_level=5000)
    e.speaking.clear()
    e._maybe_partial(tone(300, 200))        # 6 s, quiet: a normal live partial, whole buffer
    wait_idle(e)
    assert abs(e.model.lens[0] - 6.0) < 1e-6 and not e._latest.trimmed
    e.stop()


def test_interruption_detected_within_a_second():
    """Simulate frames arriving in real time: the owner starts talking loudly over
    the echo; a barge-in check must start within ~1 s of his first word."""
    e, _ = make(echo_level=500)
    voiced = tone(500, 100)                 # 3 s of echo already in the utterance
    e._barge_check_at = time.monotonic()
    e._maybe_partial(voiced)               # just echo: skipped
    wait_idle(e)
    start = len(e.model.lens)
    loud = tone(1600, 1)[0]
    for i in range(1, 60):
        voiced = voiced + [loud]
        e._maybe_partial(voiced)
        wait_idle(e)
        e._rest_until = 0.0
        if len(e.model.lens) > start:
            break
    assert len(e.model.lens) > start and i * FRAME_MS <= 1000, i
    e.stop()


def _nice_of_busy_threads(model, audio):
    def cpu(t):
        s = open(f"/proc/self/task/{t}/stat").read().rsplit(")", 1)[1].split()
        return int(s[11]) + int(s[12]), int(s[16])
    before = {t: cpu(t)[0] for t in os.listdir("/proc/self/task")}
    for _ in range(3):
        list(model.transcribe(audio, beam_size=1, language="en")[0])
    out = []
    for t in os.listdir("/proc/self/task"):
        try:
            used, ni = cpu(t)
        except OSError:
            continue
        if used > before.get(t, 0):
            out.append((used - before.get(t, 0), ni))
    return out


def test_whisper_runs_niced():
    import pytest
    base = os.getpriority(os.PRIO_PROCESS, threading.get_native_id())  # workers already run at nice 10
    try:
        import faster_whisper  # noqa: F401
        m = load_whisper("base.en", "int8", 2, nice=10)
    except Exception as ex:
        pytest.skip(f"no whisper model available: {ex}")
    audio = (np.random.default_rng(0).standard_normal(16000 * 3) * 0.05).astype(np.float32)
    busy = _nice_of_busy_threads(m, audio)
    # the decoder threads (nearly all the work) are niced; numpy's shared
    # feature-extraction threads aren't, and neither is the caller
    niced = sum(u for u, n in busy if n >= 10)
    assert niced >= 0.7 * sum(u for u, _ in busy), busy
    assert os.getpriority(os.PRIO_PROCESS, threading.get_native_id()) == base  # the caller is not niced


def test_recheck_interval_counts_from_the_end_of_the_last_check():
    """On a busy CPU a check can take 3 s; the quiet recheck must not then fire
    straight away again (that made it continuous)."""
    e, _ = make(echo_level=500)
    e._barge_check_at = time.monotonic() - 10      # long ago: recheck due
    e._maybe_partial(tone(550, 60))
    wait_idle(e)
    assert len(e.model.lens) == 1
    assert time.monotonic() - e._barge_check_at < 1.0   # stamped when it finished
    e._rest_until = 0.0
    e._maybe_partial(tone(550, 120))
    wait_idle(e)
    assert len(e.model.lens) == 1 and e.barge_skipped >= 1
    e.stop()


def test_loud_voice_skips_the_breather_quiet_one_keeps_it():
    e, _ = make(echo_level=500)
    e._barge_check_at = time.monotonic() - 10      # recheck due
    e._rest_until = time.monotonic() + 5           # a pass just finished
    e._maybe_partial(tone(550, 60))                # quiet: waits for the breather
    assert e._pending is None and e._current is None and e.model.lens == []
    e._maybe_partial(tone(500, 40) + tone(1500, 20))   # someone talking over him: at once
    wait_idle(e)
    assert len(e.model.lens) == 1
    e.stop()
