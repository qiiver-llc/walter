"""Laugh sounds: queued in order with speech, silenced by the person's mute."""
import asyncio
import wave

import numpy as np

from walter.config import Config
from walter.voice import Sfx, Voice


def write_wav(path, n=2205, sr=22050):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((np.ones(n) * 3000).astype(np.int16).tobytes())


def make_sounds(tmp_path):
    d = tmp_path / "laugh"
    d.mkdir(exist_ok=True)
    for name in ("soft-a", "soft-b", "soft-c", "big-a"):
        write_wav(d / f"{name}.wav")
    return tmp_path


def make_voice(tmp_path, **voice):
    cfg = Config()
    cfg.voice.enabled = False
    cfg.voice.piper_model = str(tmp_path / "none.onnx")
    for k, v in voice.items():
        setattr(cfg.voice, k, v)
    log = []
    v = Voice(cfg, on_level=lambda x: None,
              on_cue=lambda kind, val: log.append((kind, val)),
              on_speaking=lambda on: log.append(("speaking", on)))
    v.synth.kind = "mime"
    v.synth.synth_timed = lambda text: (np.zeros(10, np.int16), 22050, [])
    v.sfx = Sfx(make_sounds(tmp_path))
    played = []

    def play(audio, sr, on_level, silent, pause=0.12):
        played.append(("sfx" if pause == 0.06 else "say", silent, pause))
    v.player.play = play
    return v, log, played


async def run(v, events):
    v.start()
    for kind, val in events:
        if kind == "say":
            v.say(val)
        else:
            v.put(kind, val)
    await asyncio.wait_for(v.wait_idle(), 5)
    for t in v._tasks:
        t.cancel()


def test_laugh_plays_in_order_with_speech(tmp_path):
    v, log, played = make_voice(tmp_path)
    asyncio.run(run(v, [("say", "That's brilliant."), ("action", "laugh"),
                        ("say", "Right then.")]))
    assert [p[0] for p in played] == ["say", "sfx", "say"]
    assert played[1][2] == 0.06
    cues = [e for e in log if e[0] in ("caption", "action")]
    assert cues == [("caption", "That's brilliant."), ("action", "laugh"),
                    ("caption", "Right then.")]
    # one continuous speaking stretch, so the mic echo-mute covers the laugh
    assert [e for e in log if e[0] == "speaking"] == [("speaking", True), ("speaking", False)]


def test_laugh_alone_counts_as_speaking(tmp_path):
    v, log, played = make_voice(tmp_path)
    asyncio.run(run(v, [("action", "laugh")]))
    assert [p[0] for p in played] == ["sfx"]
    assert ("speaking", True) in log and log[-1] == ("speaking", False)


def test_hushed_plays_no_laugh_but_keeps_animation(tmp_path):
    v, log, played = make_voice(tmp_path)
    v.hushed = True
    asyncio.run(run(v, [("say", "Ha."), ("action", "laugh")]))
    assert [p[0] for p in played] == ["say"]
    assert ("action", "laugh") in log


def test_config_switch_off(tmp_path):
    v, log, played = make_voice(tmp_path, laugh_sounds=False)
    asyncio.run(run(v, [("action", "laugh:big")]))
    assert played == [] and log == [("action", "laugh")]


def test_silent_reply_skips_sound(tmp_path):
    v, log, played = make_voice(tmp_path)
    v.start = lambda: None
    v.put("action", "laugh", sound=False)
    assert v.inbox.qsize() == 1 and v.busy == 1


def test_big_laugh_maps_to_face_laugh_and_big_clip(tmp_path):
    sfx = Sfx(make_sounds(tmp_path))
    assert [f.name for f in sfx.candidates("laugh:big")] == ["big-a.wav"]
    assert len(sfx.candidates("laugh")) == 3
    v, log, played = make_voice(tmp_path)
    asyncio.run(run(v, [("action", "laugh:big")]))
    assert log[0] == ("action", "laugh") and [p[0] for p in played] == ["sfx"]


def test_never_same_clip_twice_in_a_row(tmp_path):
    sfx = Sfx(make_sounds(tmp_path))
    last = None
    for _ in range(200):
        sfx.pick("laugh")
        cur = sfx._last["laugh"]
        assert cur != last and cur.name.startswith("soft-")
        last = cur


def test_missing_sounds_is_harmless(tmp_path):
    assert Sfx(tmp_path / "nothing").pick("laugh") is None


def test_shipped_clips_are_valid():
    sfx = Sfx()
    soft, big = sfx.candidates("laugh"), sfx.candidates("laugh:big")
    assert len(soft) >= 3 and len(big) >= 1
    for f in soft + big:
        with wave.open(str(f)) as w:
            assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 22050)
            assert 0.4 <= w.getnframes() / 22050 <= 1.8
