"""Kokoro trial voice: front end, one local engine per reply, piper fallback."""
from pathlib import Path

import numpy as np
import pytest

from walter.config import Config
from walter.kokoro import Kokoro, load_lexicon, load_tokens
from walter.voice import CAPTIONS_ONLY, Voice

KOKORO = np.ones(2205, np.int16) * 2000
PIPER = np.zeros(5, np.int16)


def test_tokens_file_keeps_the_space_symbol(tmp_path):
    p = tmp_path / "tokens.txt"
    p.write_text("; 1\n  16\na 43\n", encoding="utf-8")
    assert load_tokens(p) == {";": 1, " ": 16, "a": 43}


def test_lexicon_joins_phones_and_keeps_the_first_entry(tmp_path):
    p = tmp_path / "lex.txt"
    p.write_text("degree d ə ɡ ɹ ˈ i\ndegree x\n", encoding="utf-8")
    assert load_lexicon(p) == {"degree": "dəɡɹˈi"}


def bare_kokoro(lexicon, espeak):
    k = Kokoro.__new__(Kokoro)
    k.lexicon = lexicon
    k.vocab = {c: i for i, c in enumerate(" ,.!?abdeiknoszəɡɹˈðɐ", 1)}
    k._espeak_word = espeak
    return k


def test_unknown_words_go_to_espeak_instead_of_being_dropped():
    asked = []
    k = bare_kokoro({"so": "sO"}, lambda w: asked.append(w) or "dɡɹˈiz")
    ph = k.phonemes("So, degrees!")
    assert asked == ["degrees"]
    assert ph == "sO, dɡɹˈiz!"


def test_function_words_use_the_weak_form():
    k = bare_kokoro({"a": "A"}, lambda w: "")
    assert k.phonemes("a") == "ɐ"


def make_voice(kokoro_ok, engine="kokoro"):
    cfg = Config()
    cfg.voice.enabled = False
    cfg.voice.piper_model = "/nonexistent.onnx"
    v = Voice(cfg, on_level=lambda x: None, on_cue=lambda k, val: None, on_speaking=lambda on: None)
    v.synth.kind = "piper"
    v.synth.synth_timed = lambda text: (PIPER.copy(), 22050, [["p", 0, 0]])
    script = list(kokoro_ok)
    calls = []

    def kokoro_timed(text, kokoro=None, voice=None):
        calls.append((text, voice))
        if script and not script.pop(0):
            raise RuntimeError("boom")
        return KOKORO.copy(), 22050, [["k", 0, 0]]
    v.synth.kokoro_timed = kokoro_timed
    v.synth.kokoro = object() if engine == "kokoro" else None
    v.calls = calls
    return v


def said(v, sentences):
    out = []
    for t in sentences:
        audio, _sr, _w, _s, flag = v._timed_synth(t, gen=v.generation)
        out.append(CAPTIONS_ONLY if flag == CAPTIONS_ONLY else
                   "kokoro" if np.array_equal(audio, KOKORO) else "piper")
    return out


def test_kokoro_says_the_whole_reply_when_it_works():
    v = make_voice([True, True])
    assert said(v, ["One.", "Two."]) == ["kokoro", "kokoro"]


def test_kokoro_down_at_the_start_means_the_whole_reply_in_piper():
    v = make_voice([False, True, True])
    assert said(v, ["One.", "Two.", "Three."]) == ["piper", "piper", "piper"]
    assert len(v.calls) == 1        # no flip back to kokoro mid-reply


def test_kokoro_failing_mid_reply_is_captioned_not_switched():
    v = make_voice([True, False, True])
    assert said(v, ["One.", "Two.", "Three."]) == ["kokoro", CAPTIONS_ONLY, "kokoro"]


def test_a_new_reply_tries_kokoro_again():
    v = make_voice([False, True])
    assert said(v, ["One."]) == ["piper"]
    v._reply_t -= v.REPLY_GAP_S + 1
    assert said(v, ["Later."]) == ["kokoro"]


def test_three_failures_in_a_row_unload_kokoro():
    v = make_voice([False, False, False, True])
    for i in range(3):
        v._reply_t -= v.REPLY_GAP_S + 1
        assert said(v, [f"Try {i}."]) == ["piper"]
    assert v.synth.kokoro is None and v.synth.local_engine == "piper"


def test_piper_engine_never_touches_kokoro():
    v = make_voice([], engine="piper")
    assert said(v, ["One."]) == ["piper"]
    assert v.calls == []


def test_demo_forces_a_named_kokoro_voice_and_piper():
    v = make_voice([True], engine="kokoro")
    a = v._timed_synth("Hi.", provider="kokoro:am_puck")
    b = v._timed_synth("Hi.", provider="piper")
    assert np.array_equal(a[0], KOKORO) and np.array_equal(b[0], PIPER)
    assert v.calls == [("Hi.", "am_puck")]


def test_default_config_stays_piper():
    assert Config().voice.engine == "piper"


MODEL = Path(Config().voice.kokoro_dir).expanduser()


@pytest.mark.skipif(not (MODEL / "voices.bin").exists(), reason="Kokoro model not downloaded")
def test_real_kokoro_speaks_a_sentence():
    k = Kokoro(MODEL, "am_michael", threads=2)
    audio, sr = k.synth("Hello there.")
    assert sr == 24000 and len(audio) > sr * 0.3
    assert float(np.abs(audio).max()) > 0.01
