"""Karaoke captions: per-word timings for what Walter says."""
import asyncio

import numpy as np

from walter.config import Config
from walter.voice import (Voice, layout_words, map_words, reading_timings, resample,
                          speech_bounds)


def test_layout_is_ordered_and_proportional():
    words = ["Hi", "there,", "extraordinary", "friend."]
    out = layout_words(words, [2, 4, 12, 5], 0.5, 3.0)
    assert [w for w, _, _ in out] == words
    starts = [s for _, s, _ in out]
    assert starts == sorted(starts) and out[0][1] == 0.5
    assert abs(out[-1][2] - 3.0) < 1e-3
    for _, s, e in out:
        assert e >= s
    lens = [e - s for _, s, e in out]
    assert lens[2] > lens[1] > lens[0]
    # the comma after "there," leaves a little gap before the next word
    assert out[2][1] > out[1][2]


def test_layout_abbreviations_are_not_pauses():
    out = layout_words(["9", "a.m.", "on", "Nov.", "4th."], [4, 3, 2, 3, 4], 0, 2)
    for a, b in zip(out, out[1:]):
        assert abs(b[1] - a[2]) < 1e-6


def test_map_words_follows_real_speech():
    # spoken: "three"(4) "dollars"(6) gap "fifty"(5)  vs caption "$3.50," "please"
    spans = [(4, 0.1, 0.4), (6, 0.45, 0.9), (5, 1.4, 1.8)]
    out = map_words(["$3.50,", "now"], [15, 5], spans + [(5, 2.0, 2.3)])
    assert out[0][0] == "$3.50," and out[0][1] == 0.1 and abs(out[0][2] - 1.8) < 1e-3
    assert abs(out[1][1] - 2.0) < 1e-3 and abs(out[1][2] - 2.3) < 1e-3


def test_map_words_tolerates_count_mismatch():
    spans = [(3, 0.0, 0.3), (3, 0.4, 0.7), (3, 0.8, 1.1)]
    out = map_words(["a", "b"], [2, 2], spans)
    assert out[0][1] == 0.0 and abs(out[-1][2] - 1.1) < 1e-3
    assert out[0][2] <= out[1][1] + 1e-6
    assert map_words(["a"], [1], []) == []
    assert map_words([], [], spans) == []


def test_reading_timings_natural_pace():
    text = "This is a sentence of about ten words for reading."
    words, dur = reading_timings(text)
    assert [w for w, _, _ in words] == text.split()
    wpm = len(words) / dur * 60
    assert 140 < wpm < 240
    assert words[-1][2] <= dur + 1e-6
    assert reading_timings("") == ([], 0.0)


def test_speech_bounds_trims_silence():
    sr = 16000
    a = np.zeros(sr, np.int16)
    a[4000:12000] = 5000
    s, e = speech_bounds(a, sr)
    assert abs(s - 0.25) < 0.02 and abs(e - 0.75) < 0.02


def test_resample_keeps_tone_and_length():
    sr = 22050
    t = np.arange(sr) / sr
    x = np.sin(2 * np.pi * 300 * t).astype(np.float32)
    y = resample(x, 0.95, 1.0)
    assert abs(len(y) - round(sr / 0.95)) <= 1
    # a 300 Hz tone stretched by 1/0.95 becomes 285 Hz
    f = np.argmax(np.abs(np.fft.rfft(y[1000:-1000]))) * sr / len(y[1000:-1000])
    assert abs(f - 285) < 3
    assert np.abs(y[500:-500]).max() < 1.05


def make_voice(tmp_path):
    cfg = Config()
    cfg.voice.enabled = False
    cfg.voice.piper_model = str(tmp_path / "none.onnx")
    caps = []
    v = Voice(cfg, on_level=lambda x: None, on_cue=lambda k, val: None,
              on_speaking=lambda on: None)
    v.synth.kind = "mime"
    v.on_caption = lambda text, words, dur: caps.append((text, words, dur))
    plays = []
    v.player.play = lambda audio, sr, lvl, silent, pause=0.12: plays.append((len(audio) / sr, silent))
    return v, caps, plays


async def drive(v, texts):
    v.start()
    for t in texts:
        v.say(t)
    await asyncio.wait_for(v.wait_idle(), 5)
    for t in v._tasks:
        t.cancel()


def test_voice_sends_timed_caption_matching_audio(tmp_path):
    v, caps, plays = make_voice(tmp_path)
    v.synth.synth_timed = lambda text: (np.zeros(22050 * 2, np.int16), 22050,
                                        layout_words(text.split(), [3] * len(text.split()), 0.1, 1.9))
    asyncio.run(drive(v, ["One two three.", "Four five."]))
    assert [c[0] for c in caps] == ["One two three.", "Four five."]
    assert [w[0] for w in caps[0][1]] == ["One", "two", "three."]
    assert caps[0][2] == 2.0 and plays[0] == (2.0, True)


def test_muted_captions_are_paced_for_reading(tmp_path):
    v, caps, plays = make_voice(tmp_path)
    v.hushed = True
    called = []
    v.synth.synth_timed = lambda text: called.append(text)  # must not synthesise
    asyncio.run(drive(v, ["Just a quiet little caption for you."]))
    assert called == []
    text, words, dur = caps[0]
    assert [w[0] for w in words] == text.split() and dur > 1.0
    assert plays[0][1] is True and abs(plays[0][0] - dur) < 0.35


def test_map_words_one_to_one_when_counts_match():
    # caption "9:32 a.m." is spoken as "nine thirty two AM"
    spans = [(4, 0.0, 0.3), (5, 0.35, 0.7), (3, 0.72, 0.9), (4, 1.0, 1.3)]
    out = map_words(["9:32", "—", "a.m."], [10, 0, 2], spans, counts=[3, 0, 1])
    assert out == [["9:32", 0.0, 0.9], ["—", 0.9, 0.9], ["a.m.", 1.0, 1.3]]
