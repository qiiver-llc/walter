"""Live partial transcription (ears) and the face's listening indicator (conductor)."""
import threading
import time
from types import SimpleNamespace

from walter.config import Config
from walter.conductor import Conductor
from walter.ears import FRAME, Ears

from test_conductor import FakeVoice, run


class FakeModel:
    def __init__(self):
        self.calls = 0

    def transcribe(self, audio, **kw):
        self.calls += 1
        seg = SimpleNamespace(text=" Hey Walter, what's the time?", no_speech_prob=0.1,
                              avg_logprob=-0.2, compression_ratio=1.1)
        return iter([seg]), SimpleNamespace(duration_after_vad=1.0)


class SyncLoop:
    def call_soon_threadsafe(self, fn, *a):
        fn(*a)


def make_ears():
    got = {"text": [], "partial": []}
    e = Ears(Config(), on_text=got["text"].append, on_hear=lambda v: None, on_start=lambda: None,
             on_partial=got["partial"].append)
    e.model, e.loop, e._running = FakeModel(), SyncLoop(), True
    threading.Thread(target=e._partial_worker, daemon=True).start()
    return e, got


def wait_idle(e):
    for _ in range(200):
        if e._pending is None and e._current is None and not e._chunkq:
            return
        time.sleep(0.01)


def test_partials_and_reused_final():
    e, got = make_ears()
    voiced = [b"\0" * FRAME * 2] * 60     # 1.8 s
    e._open_utterance()
    e._maybe_partial(voiced)
    wait_idle(e)
    assert got["partial"] == ["Hey Walter, what's the time?"]
    assert e.last_partial == ("Hey Walter, what's the time?", 60)
    # silence starts: early final covering all the speech
    e._submit(voiced + [b"\0" * FRAME * 2] * 10, final=True)
    reuse = e._close_utterance(60)
    assert reuse is not None
    assert e._process(b"\0" * FRAME * 2 * 90, 60, 30.0, reuse)
    assert got["text"] == ["Hey Walter, what's the time?"]
    assert e.model.calls == 2              # the final cost no extra whisper run
    e.stop()


def test_no_reuse_when_speech_came_after_the_job():
    e, got = make_ears()
    voiced = [b"\0" * FRAME * 2] * 60
    e._open_utterance()
    e._maybe_partial(voiced)
    wait_idle(e)
    assert e._close_utterance(90) is None  # they kept talking after that partial
    e.stop()


def test_partial_skipped_while_busy_or_too_soon():
    e, _ = make_ears()
    e._running = False                     # worker idle: jobs stay pending
    e._open_utterance()
    e._maybe_partial([b""] * 60)
    first = e._pending
    assert first is not None
    e._maybe_partial([b""] * 120)          # previous one still pending: skip
    assert e._pending is first
    e._pending = None
    e._maybe_partial([b""] * 70)           # only 0.3 s newer than the last: skip
    assert e._pending is None


def make_conductor():
    sent = []
    c = Conductor(Config(), brain=None, send=sent.append)
    c.voice = FakeVoice()
    return c, sent


def phases(sent):
    return [m["phase"] for m in sent if m["t"] == "listen"]


def test_indicator_waits_for_wake_word_when_idle():
    async def go():
        c, sent = make_conductor()
        c.on_hear_start()
        c.on_hear_partial("pass me the")
        assert phases(sent) == [] and not [m for m in sent if m["t"] == "caption"]
        c.on_hear_partial("hey walter what's the")
        assert phases(sent) == ["open"]
        assert sent[-1] == {"t": "caption", "who": "you", "text": "hey walter what's the", "partial": True}
        c.on_hear_end()
        c.on_heard("Hey Walter, what's the weather?")
        assert phases(sent) == ["open", "heard", "sent"]
        c.on_hear_partial("late partial")  # after the end: ignored
        assert sent[-1]["t"] != "caption" or not sent[-1].get("partial")
    run(go)


def test_indicator_hidden_when_ignored_or_dropped():
    async def go():
        c, sent = make_conductor()
        c.followup_until = time.time() + 5   # just spoke: show at once
        c.on_hear_start()
        c.on_hear_end()
        c.on_hear_dropped()
        assert phases(sent) == ["open", "heard", "off"]
        c.followup_until = time.time() - 10
        sent.clear()
        c.state = "idle"
        c.on_hear_start()
        c.on_hear_end()
        c.on_heard("pass me the salt please")
        assert phases(sent) == ["off"]
    run(go)


def test_early_final_skipped_when_partial_covers_speech():
    e, _ = make_ears()
    e._running = False
    e._open_utterance()
    e._submit([b""] * 60, final=False)
    first = e._pending
    e._early_final([b""] * 70, 60)         # the partial already ends where speech ended
    assert e._pending is first
    e._early_final([b""] * 80, 70)         # more speech since: transcribe again
    assert e._pending is not first and e._pending.final


def wait_for(cond):
    for _ in range(200):
        if cond():
            return
        time.sleep(0.01)


class PiecesModel:
    """Returns the given texts in order, one per whisper pass."""

    def __init__(self, texts):
        self.texts, self.kw = list(texts), []

    def transcribe(self, audio, **kw):
        self.kw.append(kw)
        seg = SimpleNamespace(text=" " + self.texts.pop(0), no_speech_prob=0.1,
                              avg_logprob=-0.2, compression_ratio=1.1)
        return iter([seg]), SimpleNamespace(duration_after_vad=1.0)


def test_long_turn_is_stitched_together():
    e, got = make_ears()
    e.model = PiecesModel(["Walter, I was thinking about the trip,", "and we should leave at nine",
                           "so remind me tonight."])
    piece = [b"\0" * FRAME * 2] * 400       # 12 s
    e._open_utterance()
    e._submit_chunk(piece)
    wait_for(lambda: got["partial"])
    assert got["partial"][-1] == "Walter, I was thinking about the trip,"   # shown while still talking
    e._submit_chunk(piece)
    wait_for(lambda: len(got["partial"]) >= 2)
    assert got["partial"][-1] == "Walter, I was thinking about the trip, and we should leave at nine"
    e._finish_turn([b"\0" * FRAME * 2] * 60, 20, 900, 30.0)
    assert got["text"] == ["Walter, I was thinking about the trip, and we should leave at nine "
                           "so remind me tonight."]
    assert all(kw["beam_size"] == e.cfg.ears.beam_size for kw in e.model.kw)   # finals use beam search
    e.stop()


def test_long_turn_short_tail_is_not_rejected():
    e, got = make_ears()
    e.model = PiecesModel(["Walter, tell me a long story about dragons and castles"])
    e._open_utterance()
    e._submit_chunk([b"\0" * FRAME * 2] * 400)
    wait_idle(e)
    e._finish_turn([b"\0" * FRAME * 2] * 5, 5, 400, 30.0)   # only a breath after the piece
    assert got["text"] == ["Walter, tell me a long story about dragons and castles"]
    assert len(e.model.kw) == 1
    e.stop()


def test_partials_use_fast_decoder():
    e, got = make_ears()
    e.model = PiecesModel(["Hey Walter, what's"])
    e._open_utterance()
    e._maybe_partial([b"\0" * FRAME * 2] * 60)
    wait_for(lambda: got["partial"])
    assert got["partial"] == ["Hey Walter, what's"]
    assert e.model.kw[0]["beam_size"] == 1 and e.model.kw[0]["hotwords"].startswith("Walter")
    assert "GitHub" in e.model.kw[0]["initial_prompt"]       # vocabulary hints
    e.stop()


def test_final_model_hears_the_final_and_partials_are_not_reused():
    e, got = make_ears()
    e.model = PiecesModel(["Hey Walter, call Shivon"])
    e.final_model = PiecesModel(["Hey Walter, call Siobhan."])
    voiced = [b"\0" * FRAME * 2] * 60
    e._open_utterance()
    e._maybe_partial(voiced)
    wait_for(lambda: got["partial"])
    assert got["partial"] == ["Hey Walter, call Shivon"]      # fast model, live
    # silence: the partial covers all the speech, but the final model must still run
    with e._whisper_lock:                                      # final model "still working"
        e._early_final(voiced + [b"\0" * FRAME * 2] * 10, 60)
        end, _, _, src = e._turn_over(10, 60)
    assert src.startswith("partial (final pending)")           # judged by the partial meanwhile
    reuse = e._close_utterance(60)
    assert reuse is not None and reuse.final
    assert e._process(b"\0" * FRAME * 2 * 90, 60, 30.0, reuse)
    assert got["text"] == ["Hey Walter, call Siobhan."]
    assert len(e.model.kw) == 1 and len(e.final_model.kw) == 1
    assert e.final_model.kw[0]["beam_size"] == e.cfg.ears.beam_size
    e.stop()


def test_custom_hotwords():
    e, _ = make_ears()
    e.cfg.ears.hotwords = ["Walter", "Zorblax"]
    e.model = PiecesModel(["Hey Walter"])
    e._recognise(b"\0" * FRAME * 2 * 30)
    assert e.model.kw[0]["hotwords"] == "Walter Zorblax"
    assert e.model.kw[0]["initial_prompt"].endswith("Names: Zorblax.")
    e.stop()
