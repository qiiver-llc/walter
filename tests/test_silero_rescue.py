"""Silero rescue: long, loud segments Silero calls silence get a whisper pass."""
from types import SimpleNamespace

import numpy as np

from walter.config import Config
from walter.ears import FRAME, SR, Ears, loud_seconds, should_rescue


def pcm(seconds: float, level: float, loud_s: float = 0.0, loud_level: float = 0.0) -> bytes:
    a = np.full(int(SR * seconds), level, np.float32)
    a[: int(SR * loud_s)] = loud_level
    # alternate sign so it's a (crude) tone, not DC
    a *= np.where(np.arange(a.size) % 2, 1, -1)
    return a.astype(np.int16).tobytes()


def seg(text, no_speech=0.1, logprob=-0.3):
    return SimpleNamespace(text=text, no_speech_prob=no_speech, avg_logprob=logprob,
                           compression_ratio=1.2)


class FakeModel:
    """Silero (vad_filter) hears nothing; without it, whisper returns `segs`."""

    def __init__(self, segs):
        self.segs, self.calls = segs, []

    def transcribe(self, audio, **kw):
        self.calls.append(kw)
        if kw.get("vad_filter"):
            return iter([]), SimpleNamespace(duration_after_vad=0)
        return iter(self.segs), SimpleNamespace(duration_after_vad=None)


def ears(segs):
    e = Ears(Config(), on_text=lambda t: None, on_hear=lambda r: None, on_start=lambda: None)
    e.model = FakeModel(segs)
    return e


def test_defaults_are_a_bit_more_permissive():
    e = Config().ears
    assert e.silero_threshold <= 0.4 and e.silero_rescue


def test_loud_seconds():
    assert loud_seconds(pcm(4, 100, loud_s=1.5, loud_level=2000), 100, 2.5) == 1.5
    assert loud_seconds(pcm(4, 100), 100, 2.5) == 0.0
    # a noise-suppressed (near-silent) background never makes small sounds "loud"
    assert loud_seconds(pcm(4, 10, loud_s=2, loud_level=100), 5, 2.5) == 0.0
    assert loud_seconds(b"\0" * (FRAME - 1), 30, 2.5) == 0.0


def test_should_rescue_rules():
    e = Config().ears
    loud = pcm(5, 200, loud_s=2, loud_level=3000)
    assert should_rescue(loud, 200, False, e)[0]
    assert not should_rescue(loud, 200, True, e)[0]           # Walter was talking (echo)
    assert not should_rescue(loud, 0.0, False, e)[0]          # no background estimate yet
    assert not should_rescue(pcm(2, 200, 2, 3000), 200, False, e)[0]  # too short
    assert not should_rescue(pcm(5, 200, 0.5, 3000), 200, False, e)[0]  # barely loud
    assert not should_rescue(pcm(5, 800), 800, False, e)[0]    # steady hum
    e.silero_rescue = False
    assert not should_rescue(loud, 200, False, e)[0]


def test_rescue_keeps_real_speech():
    e = ears([seg(" Can you show me the weather?")])
    text, notes = e._recognise(pcm(6, 200, 3, 3000), final=True, floor=200)
    assert text == "Can you show me the weather?"
    assert "rescued" in notes[0] and "Silero" in notes[0]
    rescue_kw = e.model.calls[-1]
    assert "vad_filter" not in rescue_kw and "initial_prompt" not in rescue_kw
    assert rescue_kw.get("hotwords") is None


def test_rescue_lets_whisper_reject_noise():
    e = ears([seg(" Walter is a friendly", no_speech=0.6)])  # unsure: over the stricter limit
    text, notes = e._recognise(pcm(6, 200, 3, 3000), final=True, floor=200)
    assert text is None and "whisper agrees, no speech" in notes[0]
    e = ears([seg(" Thank you.")])                           # classic hallucination
    text, notes = e._recognise(pcm(6, 200, 3, 3000), final=True, floor=200)
    assert text is None and "whisper agrees" in notes[0]


def test_no_rescue_for_partials_quiet_or_echo():
    for kw in (dict(final=False, floor=200),                 # live partials: keep CPU low
               dict(final=True, floor=200, echo=True),       # Walter's own echo
               dict(final=True, floor=0.0)):                 # no background estimate
        e = ears([seg(" hello there")])
        text, notes = e._recognise(pcm(6, 200, 3, 3000), **kw)
        assert text is None and notes == ["rejected {dur} segment: Silero VAD found no speech"]
        assert len(e.model.calls) == 1
    e = ears([seg(" hello there")])                          # quiet: Silero's word stands
    text, notes = e._recognise(pcm(6, 200), final=True, floor=200)
    assert text is None and len(e.model.calls) == 1
