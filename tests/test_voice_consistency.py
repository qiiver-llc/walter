"""One reply, one voice: a failed cloud sentence never switches voice mid-reply."""
import asyncio

import numpy as np

from walter.config import Config
from walter.voice import CAPTIONS_ONLY, Voice

CLOUD = np.ones(2205, np.int16) * 3000
LOCAL = np.zeros(5, np.int16)


class ScriptedSpeech:
    """Cloud voice whose answers are scripted call by call (None = failed)."""

    def __init__(self, script, tts="openai", room=""):
        self.tts = tts
        self.script = list(script)
        self.calls = []
        self.room = room

    def synth(self, text, sr=22050, cancelled=None, timeout=None):
        self.calls.append((text, timeout))
        ok = self.script.pop(0) if self.script else True
        return CLOUD.copy() if ok else None

    def tts_room(self, chars):
        return self.room

    def has_key(self):
        return True


def make_voice(script, **kw):
    cfg = Config()
    cfg.voice.enabled = False
    cfg.voice.piper_model = "/nonexistent.onnx"
    v = Voice(cfg, on_level=lambda x: None, on_cue=lambda k, val: None, on_speaking=lambda on: None)
    v.synth.kind = "piper"
    v.synth.synth_timed = lambda text: (LOCAL.copy(), 16000, [["local", 0, 0]])
    v.speech = ScriptedSpeech(script, **kw)
    return v


def kinds(v, sentences):
    return [v._timed_synth(t, gen=v.generation)[4] for t in sentences]


def test_blip_is_retried_once_with_the_short_timeout():
    v = make_voice([False, True])
    assert kinds(v, ["Hello there."]) == [True]
    assert v.speech.calls == [("Hello there.", None), ("Hello there.", 4.0)]


def test_cloud_down_from_the_start_means_the_whole_reply_in_piper():
    v = make_voice([False, False, True, True])   # cloud would recover for sentence 2...
    assert kinds(v, ["One.", "Two.", "Three."]) == [False, False, False]
    assert len(v.speech.calls) == 2              # ...but the reply stays in piper: no flip back
    assert v.reply_voice() == "local"


def test_failure_mid_reply_is_captioned_not_switched():
    v = make_voice([True, False, False, True])
    assert kinds(v, ["One.", "Two.", "Three."]) == [True, CAPTIONS_ONLY, True]
    assert v.reply_voice() == "openai"


def test_a_new_reply_after_a_gap_tries_the_cloud_again():
    v = make_voice([False, False, True])
    assert kinds(v, ["One."]) == [False]
    v._reply_t -= v.REPLY_GAP_S + 1
    assert kinds(v, ["Later."]) == [True]


def test_no_retry_when_capped():
    v = make_voice([False], room="daily cap reached")
    assert kinds(v, ["One."]) == [False]
    assert len(v.speech.calls) == 1


def test_retry_can_be_turned_off():
    v = make_voice([False])
    v.cfg.speech.tts_retry_timeout_s = 0
    kinds(v, ["One."])
    assert len(v.speech.calls) == 1


def test_forced_provider_demo_ignores_the_reply_voice():
    v = make_voice([False, False, True])
    kinds(v, ["One."])                          # reply locked to piper
    assert v._timed_synth("Demo.", "openai")[4] is True


def test_warming_an_ack_never_caches_piper_audio_for_the_cloud_voice():
    v = make_voice([False, True])
    v.cache_lines([])                           # (nothing)
    v._cacheable.add("One sec.")
    v._warm_one(["One sec."])                   # cloud fails: nothing cached, no retry
    assert v._clip_cache == {} and len(v.speech.calls) == 1
    assert v.reply_voice() is None              # warming isn't a reply
    v._warm_one(["One sec."])
    (key, clip), = v._clip_cache.items()
    assert key[0] == "openai" and clip[3] is True


def play_all(v, lines):
    played = []
    v.player.play = lambda audio, sr, lvl, silent, pause=0.12: played.append(
        ("silent" if silent else ("cloud" if len(audio) == len(CLOUD) else "local")))

    async def go():
        v.start()
        for t in lines:
            v.say(t)
        await asyncio.sleep(0.05)
        await asyncio.wait_for(v.wait_idle(), 5)
    asyncio.run(go())
    return played


def test_cached_cloud_ack_not_played_when_the_reply_went_to_piper():
    v = make_voice([True, False, False])
    v._cacheable.add("One sec.")
    v._warm_one(["One sec."])                   # cached in the cloud voice
    v._reply_voice, v._reply_t = "local", __import__("time").monotonic()
    assert play_all(v, ["One sec.", "Here you go."]) == ["local", "local"]


def test_queue_plays_one_voice_per_reply():
    v = make_voice([True, False, False, True])
    assert play_all(v, ["One.", "Two.", "Three."]) == ["cloud", "silent", "cloud"]


def test_cached_ack_then_failing_answer_does_not_mix():
    """'One sec.' from the cache (cloud), then the answer's cloud call fails: the
    answer is captioned rather than spoken in the other voice."""
    v = make_voice([True, False, False])
    v._cacheable.add("One sec.")
    v._warm_one(["One sec."])
    assert play_all(v, ["One sec.", "The answer."]) == ["cloud", "silent"]
