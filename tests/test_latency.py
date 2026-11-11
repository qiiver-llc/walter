import asyncio
import time

import numpy as np

from walter.config import Config
from walter.latency import TurnTiming
from walter.voice import Voice


def test_summary_lists_stages_in_order_with_deltas():
    t = TurnTiming(speech_end=100.0, turn_end=101.4, text=101.4)
    for key, at in [("brain", 101.5), ("first_token", 104.0), ("first_say", 104.3), ("audio", 105.5)]:
        t.mark(key, at)
    t.mark("audio", 999.0)   # only the first time counts
    t.synth_s = 1.1
    s = t.summary()
    assert s.startswith("latency 5.5s: turn end +1.4, text +0.0, brain +0.1, first token +2.5")
    assert "first sentence +0.3, audio +1.2 (synth 1.1s)" in s


def test_summary_without_audio():
    t = TurnTiming(speech_end=10.0, turn_end=10.7, text=10.9)
    t.mark("brain", 11.0)
    assert t.summary().startswith("latency (no audio) 1.0s:")


def make_voice(tmp_path):
    cfg = Config()
    cfg.voice.enabled = False
    cfg.voice.piper_model = str(tmp_path / "none.onnx")
    v = Voice(cfg, on_level=lambda x: None, on_cue=lambda k, val: None, on_speaking=lambda on: None)
    v.synth.kind = "mime"
    v.player.play = lambda *a, **k: None
    return v


def test_voice_reports_first_audio_and_synthesises_one_at_a_time(tmp_path):
    v = make_voice(tmp_path)
    running, overlap, order = [0], [False], []

    def synth(text):
        running[0] += 1
        overlap[0] |= running[0] > 1
        time.sleep(0.02)
        order.append(text)
        running[0] -= 1
        return np.zeros(10, np.int16), 16000, []
    v.synth.synth_timed = synth
    fired = []

    async def go():
        v.start()
        v.on_next_audio(lambda s: fired.append(s))
        for t in ["One.", "Two.", "Three."]:
            v.say(t)
        await asyncio.wait_for(v.wait_idle(), 5)
        for t in v._tasks:
            t.cancel()
    asyncio.run(go())
    assert order == ["One.", "Two.", "Three."] and not overlap[0]
    assert len(fired) == 1 and fired[0] is not None and fired[0] >= 0.0


def test_conductor_logs_one_latency_line_per_spoken_turn(caplog):
    import logging
    from walter.conductor import Conductor

    class FakeBrain:
        async def ask(self, text, model=None):
            yield ("text", "[happy] Honestly, I'd skip that one, the reviews ")
            yield ("text", "are mixed. Anyway.")
            yield ("done", "")

    class FakeVoice:
        generation = 0

        def __init__(self):
            self.said, self.cb = [], None

        def say(self, t):
            self.said.append(t)
            if self.cb:
                self.cb(0.4)
                self.cb = None

        def put(self, *a, **k):
            pass

        def on_next_audio(self, cb):
            self.cb = cb

        async def wait_idle(self):
            pass

    class FakeEars:
        last_timing = None

    async def go():
        c = Conductor(Config(), brain=FakeBrain(), send=lambda m: None)
        c.voice = FakeVoice()
        c.ears = FakeEars()
        now = time.monotonic()
        c.ears.last_timing = {"speech_end": now - 1.0, "turn_end": now - 0.2, "text": now - 0.1}
        c.on_heard("Walter, should I watch that new film?")
        source, text, meta = c.inbox.get_nowait()
        assert "timing" in meta
        await c._respond(source, text, meta)
        return c
    with caplog.at_level(logging.INFO, logger="walter.latency"):
        c = asyncio.run(go())
    # a question: the cached instant ack goes first, then the reply
    assert c.voice.said[0] in c.acks.options and c.voice.said[1] == "Honestly, I'd skip that one,"
    lines = [r.getMessage() for r in caplog.records if r.name == "walter.latency"]
    assert len(lines) == 1 and lines[0].startswith("latency ") and "synth 0.4s)" in lines[0] and "ack 0.2s after turn end" in lines[0]
