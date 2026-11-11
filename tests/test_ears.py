from types import SimpleNamespace

import numpy as np

from walter.config import Config
from walter.ears import FRAME, NoiseFloor, frame_rms, segment_rejection


def frame(level):
    return (np.ones(FRAME) * level).astype(np.int16).tobytes()


def test_noise_floor_adapts_and_gates():
    nf = NoiseFloor(1.5)
    assert nf.loud_enough(1.0)  # warming up: never gates
    for _ in range(200):
        nf.add(frame_rms(frame(400)))
    assert 390 < nf.floor < 410
    assert not nf.loud_enough(500)
    assert nf.loud_enough(700)


def test_noise_gate_off():
    nf = NoiseFloor(0)
    for _ in range(200):
        nf.add(1000.0)
    assert nf.loud_enough(1.0)


def test_segment_rejection():
    e = Config().ears
    seg = lambda **k: SimpleNamespace(**{"no_speech_prob": 0.1, "avg_logprob": -0.3,
                                         "compression_ratio": 1.2, **k})
    assert segment_rejection(seg(), e) == ""
    assert "no_speech" in segment_rejection(seg(no_speech_prob=0.9), e)
    assert "logprob" in segment_rejection(seg(avg_logprob=-1.5), e)
    assert "compression" in segment_rejection(seg(compression_ratio=3.0), e)


# ---- from walter-v1 (adapted to the merged ears) ----
from walter.ears import FRAME_MS, clean_text  # noqa: E402
from walter.turn import looks_unfinished  # noqa: E402


def test_clean_text():
    assert clean_text(" Thank you. ") == ""
    assert clean_text("[Music]") == ""
    assert clean_text("(wind blowing)") == ""
    assert clean_text("Hey Walter, what's   the weather?") == "Hey Walter, what's the weather?"


def test_sounds_unfinished():
    assert looks_unfinished("So I was thinking that")
    assert looks_unfinished("I need to buy eggs, milk,")
    assert looks_unfinished("Can you remind me to call my mom and")
    assert not looks_unfinished("What time is it?")
    assert not looks_unfinished("Turn off the lights.")


def test_old_config_keys_still_load(tmp_path):
    from walter.config import load
    p = tmp_path / "config.toml"
    p.write_text("[ears]\nnoise_margin_db = 9\nlive_transcripts = false\n"
                 "partial_every_s = 2.0\npause_ms = 1500\n")
    e = load(p).ears
    assert e.live_partials is False and e.partial_interval_s == 2.0
    assert e.end_silence_unfinished_ms == 1500
