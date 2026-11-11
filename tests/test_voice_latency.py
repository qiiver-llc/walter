"""[voice] output_latency_s: a bigger PipeWire buffer for Walter's voice, and a
stop still cuts him off at once (abort, not drain)."""
import threading
import types

import numpy as np

from walter.config import VoiceCfg
from walter.voice import Player, output_latency


def test_output_latency_kwargs():
    assert output_latency(VoiceCfg()) == {"latency": 0.2}
    assert output_latency(types.SimpleNamespace(output_latency_s=0)) == {}
    assert output_latency(types.SimpleNamespace()) == {}


class FakeStream:
    def __init__(self, log, **kw):
        self.log, self.kw = log, kw
        log.append(("open", kw))

    def start(self): pass
    def write(self, chunk): return False
    def stop(self): self.log.append("stop")
    def abort(self): self.log.append("abort")
    def close(self): self.log.append("close")


def _player(log, latency=0.2):
    p = Player.__new__(Player)
    p.cfg = types.SimpleNamespace(voice=types.SimpleNamespace(device="", output_latency_s=latency))
    p.stop_flag = threading.Event()
    p.sd = types.SimpleNamespace(OutputStream=lambda **kw: FakeStream(log, **kw))
    return p


def test_play_passes_latency_and_drains_normally():
    log = []
    _player(log).play(np.zeros(2205, np.int16), 22050, lambda v: None, silent=False)
    assert log[0][1]["latency"] == 0.2
    assert log[-2:] == ["stop", "close"]


def test_stop_mid_sentence_aborts():
    log = []
    p = _player(log)
    p.stop_flag.set()
    p.play(np.zeros(22050, np.int16), 22050, lambda v: None, silent=False)
    assert log[-2:] == ["abort", "close"]
