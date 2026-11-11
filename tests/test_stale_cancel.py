"""Stale requests are cancelled, and late results never play or arrive out of order.

Covers: interrupting a turn while thinking / while speaking, a late brain
result after an interrupt (real Brain + fake_claude, which only notices the
interrupt after the turn like a mid-tool Claude), cloud TTS/STT calls cut off
mid-flight (the fake server sees the connection drop), task reports held until
a gap in the conversation, in order, and folded into the turn that asks about them.
"""
import asyncio
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from walter.brain import Brain
from walter.conductor import REPORTS_NOTE
from walter.config import Config

from test_barge_in import FakeBrain, make
from test_speech import FakeOpenAI, make_speech, make_voice

FAKE_CLAUDE = Path(__file__).with_name("fake_claude.py")


@pytest.fixture
def fake():
    f = FakeOpenAI()
    yield f
    f.close()


def real_brain(tmp_path, monkeypatch) -> Brain:
    monkeypatch.setattr(Brain, "_load_session", lambda self: None)   # don't read ~/.walter
    exe = tmp_path / "claude"
    exe.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_CLAUDE} \"$@\"\n")
    exe.chmod(0o755)
    cfg = Config()
    cfg.brain.claude_bin = str(exe)
    cfg.brain.home = str(tmp_path / "home")
    cfg.brain.fast_model = cfg.brain.deep_model = "sonnet"
    b = Brain(cfg)
    b.state_file = tmp_path / "brain.json"
    return b


async def text_of(gen) -> str:
    return "".join(v for k, v in [x async for x in gen] if k == "text")


# ------------------------------------------------------------------ brain
def test_abandoned_turn_late_result_is_dropped_not_read_as_next_reply(tmp_path, monkeypatch):
    async def go():
        b = real_brain(tmp_path, monkeypatch)
        try:
            gen = b.ask("tell me a SLOWSTORY")
            first = await gen.__anext__()
            while first[0] != "text":
                first = await gen.__anext__()
            assert "Once upon a time" in first[1]
            await gen.aclose()                       # the reply went stale
            assert b.orphans == 1 and b.abandoned == 1
            t0 = time.monotonic()
            got = await text_of(b.ask("say: Tokyo is fourteen hours ahead."))
            assert got == "Tokyo is fourteen hours ahead."   # only this turn's own words
            assert "Story part" not in got
            assert b.orphans == 0 and b.dropped_late > 5
            assert time.monotonic() - t0 < 5
            # and the turn after that is untouched
            assert await text_of(b.ask("say: Next.")) == "Next."
        finally:
            await b.close()
    asyncio.run(go())


def test_interrupt_while_thinking_frees_the_loop_and_nothing_leaks(tmp_path, monkeypatch):
    """New words while the brain is still on a long reply: the old turn is abandoned at
    once (no waiting for it to finish), and none of its words reach the next turn."""
    async def go():
        c, _ = make("aec", brain=real_brain(tmp_path, monkeypatch))
        try:
            c.followup_until = time.time() + 30
            t0 = time.monotonic()
            old = asyncio.create_task(c._respond("voice", "tell me a SLOWSTORY"))
            for _ in range(200):                     # wait until the story is streaming
                await asyncio.sleep(0.02)
                if c.voice.said:
                    break
            # (already speaking: an interruption, not an unanswered request)
            c._turn = {"source": "voice", "text": "tell me a SLOWSTORY", "said": c.voice.said[:1]}
            c.submit("text", "say: Pasta tonight.")   # they cut in
            await asyncio.wait_for(old, 2)
            assert time.monotonic() - t0 < 1.6       # the full story takes ~0.7 s more
            src, text, meta = c.inbox.get_nowait()
            assert "interrupted" in meta
            said_before = list(c.voice.said)
            await c._respond(src, text, meta)
            new = c.voice.said[len(said_before):]
            assert new == ["Pasta tonight."]
            assert not any("Story part 11" in s for s in c.voice.said)
            assert c.stale_counts["brain_turns"] == 1
            assert c.cancelled_stale()["brain_late_events"] > 0
        finally:
            await c.brain.close()
    asyncio.run(go())


def test_interrupt_while_speaking_drops_the_rest(tmp_path, monkeypatch):
    async def go():
        c, _ = make("aec")
        stop_now = lambda: c.voice.stop()            # barge-in mid-reply
        c.brain = FakeBrain(["[happy] First bit is said. ", stop_now,
                             "Second bit must not be said. ", "Nor this."])
        await c._respond("voice", "tell me something")
        assert c.voice.said == ["First bit is said."]
    asyncio.run(go())


# ------------------------------------------------------------------ cloud TTS
def test_abort_cuts_in_flight_cloud_tts_and_counts_only_what_arrived(tmp_path, fake):
    sp = make_speech(tmp_path, fake, tts_timeout_s=10)
    fake.mode = "drip"
    out = {}
    th = threading.Thread(target=lambda: out.update(a=sp.synth("A fairly long sentence to say.")))
    th.start()
    time.sleep(0.4)
    t0 = time.monotonic()
    assert sp.abort("tts") == 1
    th.join(2)
    assert not th.is_alive() and time.monotonic() - t0 < 1.0
    assert out["a"] is None
    assert fake.aborted.wait(2)                       # the server saw the connection go
    assert sp.stats["tts_aborted"] == 1 and sp.stats["tts_fallback"] == 0
    day = sp.status()["today"]
    assert 0 < day["tts_audio_s"] < 1.5               # ~0.4 s received, not the full ~4 s
    assert day["tts_aborted"] == 1


def test_voice_stop_cancels_cloud_tts_and_prefetched_sentences(tmp_path, fake):
    sp = make_speech(tmp_path, fake, tts_timeout_s=10)
    sp.tts = "openai"
    fake.mode = "drip"
    v = make_voice(tmp_path)
    v.speech = sp
    piper = []
    v.synth.synth_timed = lambda text: (piper.append(text), (np.zeros(5, np.int16), 16000, []))[1]
    played = []
    v.player.play = lambda audio, sr, lvl, silent, pause=0.12: played.append(len(audio))

    async def go():
        v.start()
        for s in ("One.", "Two.", "Three.", "Four."):
            v.say(s)
        await asyncio.sleep(0.4)                     # sentence one is synthesising
        v.stop()
        await asyncio.wait_for(v.wait_idle(), 2)
        await asyncio.sleep(0.3)
    asyncio.run(go())
    assert played == [] and piper == []              # nothing stale played, no piper fallback
    assert fake.aborted.wait(2)
    assert len(fake.requests) == 1                   # prefetched sentences were never sent
    assert v.stale_counts["cloud"] == 1 and v.stale_total() >= 3


# ------------------------------------------------------------------ cloud STT
def test_mute_cancels_pending_cloud_transcription(tmp_path, fake):
    sp = make_speech(tmp_path, fake, stt_timeout_s=10)
    fake.mode = "drip"

    async def go():
        c, _ = make("aec")
        c.speech = sp
        loop = asyncio.get_running_loop()
        job = loop.run_in_executor(None, sp.transcribe, b"\0" * 32000, "")
        await asyncio.sleep(0.4)
        t0 = time.monotonic()
        c.set_user_mute(True)
        got = await asyncio.wait_for(job, 2)
        assert got is None and time.monotonic() - t0 < 1.0
    asyncio.run(go())
    assert fake.aborted.wait(2)
    assert sp.stats["stt_aborted"] == 1 and sp.stats["stt_fallback"] == 0


def test_ears_drop_a_transcript_if_muted_during_cloud_call():
    from test_live import make_ears
    from test_speech import FakeSpeech
    e, got = make_ears()

    class MutingSpeech(FakeSpeech):
        def transcribe(self, pcm, prompt=""):
            e.user_muted.set()                        # Space pressed while the call was out
            return None                               # (aborted)
    e.speech = MutingSpeech()
    e.cloud_gate = lambda text: True
    assert not e._process(b"\0" * 2 * 16000 * 2, 60, 30.0)
    assert not got["text"]
    e.stop()


# ------------------------------------------------------------------ held reports
def report(n, title):
    return ("task", f'Task #{n} "{title}" done.\nWorker\'s report:\nAll finished, {n}.')


def run_conductor(c):
    return asyncio.create_task(c.run())


def test_task_report_waits_for_a_gap_and_keeps_order():
    async def go():
        c, _ = make("aec")
        c.gap_s = 0.4
        c.last_heard = time.time()                    # the owner is mid-conversation
        c.inbox.put_nowait(report(1, "Compare vacuum cleaners"))
        c.inbox.put_nowait(("reminder", "the tea is ready"))
        c.inbox.put_nowait(report(2, "Fix the dashboard clock"))
        task = run_conductor(c)
        for _ in range(8):                            # he keeps talking: nothing barges in
            c.last_heard = time.time()
            await asyncio.sleep(0.1)
        assert c.brain.asked == [] and len(c._held) == 3
        c.inbox.put_nowait(("voice", "what's the weather?"))   # his own words go first
        await asyncio.sleep(0.1)
        assert len(c.brain.asked) == 1 and c.brain.asked[0].endswith("what's the weather?")
        c.last_heard = time.time()
        for _ in range(30):                           # then a gap: all three, in order
            await asyncio.sleep(0.1)
            if len(c.brain.asked) == 4:
                break
        task.cancel()
        later = c.brain.asked[1:]
        assert len(later) == 4 - 1
        assert "Task #1" in later[0] and "the tea is ready" in later[1] and "Task #2" in later[2]
        assert c.stale_counts["reports_deferred"] == 3
    asyncio.run(go())


def test_report_is_said_at_once_when_the_room_is_quiet():
    async def go():
        c, _ = make("aec")
        c.gap_s = 0.4
        c.last_heard = c.last_speech = 0.0
        c.inbox.put_nowait(report(3, "Book the vet"))
        task = run_conductor(c)
        await asyncio.sleep(0.15)
        task.cancel()
        assert len(c.brain.asked) == 1 and "Task #3" in c.brain.asked[0]
        assert c.stale_counts["reports_deferred"] == 0
    asyncio.run(go())


def test_report_jon_asks_about_is_answered_in_that_turn_and_not_repeated():
    async def go():
        c, _ = make("aec")
        c.gap_s = 0.3
        c.last_heard = time.time()
        c.inbox.put_nowait(report(7, "Find a lasagne recipe"))
        c.inbox.put_nowait(report(8, "Compare vacuum cleaners"))
        task = run_conductor(c)
        await asyncio.sleep(0.1)
        assert c.brain.asked == []
        c.inbox.put_nowait(("voice", "is the lasagne thing done yet?"))
        await asyncio.sleep(0.1)
        first = c.brain.asked[0]
        assert REPORTS_NOTE in first and "Task #7" in first and "Task #8" not in first
        c.last_heard = c.last_speech = 0.0
        c._talk_until = 0.0
        for _ in range(20):
            await asyncio.sleep(0.1)
            if len(c.brain.asked) == 2:
                break
        await asyncio.sleep(0.3)
        task.cancel()
        assert len(c.brain.asked) == 2 and "Task #8" in c.brain.asked[1]   # #7 not announced again
        assert c.stale_counts["reports_folded"] == 1
    asyncio.run(go())


def test_unrelated_question_does_not_take_the_report():
    async def go():
        c, _ = make("aec")
        c._held = [(time.time(), report(7, "Find a lasagne recipe"))]
        assert c._fold_reports("what time is it in Tokyo?", None) is None
        assert c._fold_reports("set a timer for the lasagne", None) is None   # no status question
        meta = c._fold_reports("any updates on task 7?", {"speaker": "x"})
        assert meta["speaker"] == "x" and "Task #7" in meta["reports"] and not c._held
    asyncio.run(go())


# ------------------------------------------------------------------ queued turns
def test_stale_heartbeat_is_dropped_when_he_talks():
    async def go():
        c, _ = make("aec")
        c.inbox.put_nowait(("heartbeat", ""))
        c.inbox.put_nowait(("voice", "and the dentist"))
        c.inbox.put_nowait(report(4, "x"))
        text, _ = c._coalesce("remind me about the vet", None)
        assert text == "remind me about the vet\nand the dentist"
        assert c.inbox.get_nowait()[0] == "task" and c.inbox.empty()
        assert c.stale_counts["queued_turns"] == 1
    asyncio.run(go())


def test_mute_sleep_and_mode_switch_cancel_pending():
    async def go():
        c, _ = make("aec")
        c.busy = True
        c.inbox.put_nowait(("voice", "one"))
        c.inbox.put_nowait(report(5, "y"))
        c.set_user_mute(True)
        await asyncio.sleep(0)
        assert c.brain.interrupts == 1 and c.voice.stops >= 1
        # their queued request and workers' reports are kept (requests finish quietly)
        assert [i[0] for i in c.inbox._queue] == ["voice", "task"]
        # a mode switch drops what's queued but leaves the turn that made the switch alone
        c.inbox.put_nowait(("text", "two"))
        stops = c.voice.stops
        c.cancel_pending("mode switched to classic", keep_current=True)
        await asyncio.sleep(0)
        assert c.voice.stops == stops and c.brain.interrupts == 1
        assert [i[0] for i in c.inbox._queue] == ["task"]
        st = c.cancelled_stale()
        assert st["queued_turns"] == 2 and st["total"] >= 2
    asyncio.run(go())
