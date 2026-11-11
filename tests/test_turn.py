from types import SimpleNamespace

import pytest

from walter.turn import COMPLETE, UNFINISHED, UNKNOWN, classify, looks_unfinished, silence_needed_ms

WAKE = ["walter", "wolter", "walther", "hey walt"]


@pytest.mark.parametrize("text", [
    "Can you check the weather and",
    "Can you check the weather and...",
    "What's on my calendar for, um",
    "Remind me to call the",
    "I was thinking that maybe,",
    "Turn on the lights because",
    "Set a timer for ten minutes or",
    "So the thing is, uh.",            # whisper often adds a full stop anyway
    "I want to buy the.",
    "Tell me about…",
    "Play some music like",
    "What I need is a list of: ",
    "Can you, you know",
    "Hey Walter,",
    "Hey Walter.",
    "Walter",
    "Hey Walt",
    "Okay so I'm gonna",
    "Let me think",
])
def test_unfinished(text):
    assert looks_unfinished(text, WAKE), classify(text, WAKE)


@pytest.mark.parametrize("text", [
    "What's the weather tomorrow?",
    "Turn off the kitchen lights.",
    "Thanks, that's all!",
    "Who am I?",
    "What's this?",
    "I think so.",
    "Where is it from?",
    "Hey Walter, what time is it?",
    "Walter, set a timer for ten minutes.",
])
def test_complete(text):
    assert classify(text, WAKE)[0] == COMPLETE, classify(text, WAKE)


@pytest.mark.parametrize("text", ["", "   ", None, "set a timer for ten minutes", "..."[:0]])
def test_unknown_when_no_evidence(text):
    assert classify(text, WAKE)[0] == UNKNOWN


def test_mid_sentence_wake_word_is_not_just_the_wake_word():
    assert classify("Tell Walter's sister hello.", WAKE)[0] == COMPLETE


def cfg(**kw):
    base = dict(adaptive_end_of_turn=True, end_silence_ms=1000, end_silence_complete_ms=700,
                end_silence_unfinished_ms=2000, wake_words=WAKE)
    base.update(kw)
    return SimpleNamespace(**base)


def test_silence_needed():
    assert silence_needed_ms("What time is it?", cfg())[:2] == (700, COMPLETE)
    assert silence_needed_ms("What time is it and", cfg())[:2] == (2000, UNFINISHED)
    assert silence_needed_ms("what time is it", cfg())[:2] == (1000, UNKNOWN)
    assert silence_needed_ms("What time is it and", cfg(adaptive_end_of_turn=False))[0] == 1000


# --- end_of_turn decision -------------------------------------------------
from walter.turn import end_of_turn  # noqa: E402


def test_end_of_turn_complete_ends_fast():
    assert end_of_turn(690, "What time is it?", cfg())[0] is False
    assert end_of_turn(700, "What time is it?", cfg())[:2] == (True, COMPLETE)


def test_end_of_turn_unfinished_waits_long():
    assert end_of_turn(1500, "Remind me to call the", cfg())[0] is False
    assert end_of_turn(2000, "Remind me to call the", cfg())[:2] == (True, UNFINISHED)


def test_end_of_turn_waits_for_pending_transcript_then_gives_up():
    # waits for the transcript being made, but never more than ~1.2 s of silence
    assert end_of_turn(1100, None, cfg(), pending=True)[0] is False
    assert end_of_turn(1200, None, cfg(), pending=True)[:3] == (True, UNKNOWN, "transcript not ready in time")


def test_end_of_turn_without_transcript_uses_plain_timeout():
    assert end_of_turn(999, None, cfg())[0] is False
    assert end_of_turn(1000, None, cfg())[0] is True


def test_end_of_turn_adaptive_off():
    off = cfg(adaptive_end_of_turn=False)
    assert end_of_turn(1000, "and", off)[0] is True
    assert end_of_turn(900, "What?", off)[0] is False


# --- Ears._turn_over: which transcript is trusted ---------------------------
def make_ears():
    from walter.config import Config
    from walter.ears import Ears
    return Ears(Config(), on_text=lambda t: None, on_hear=lambda v: None, on_start=lambda: None)


def done_job(text, frames, final=False):
    from walter.ears import _Job
    j = _Job(0, b"", frames, final)
    j.text = text
    j.done.set()
    return j


def test_turn_over_uses_current_partial():
    e = make_ears()
    e._latest = done_job("Turn off the lights.", 100)
    assert e._turn_over(24, 100)[:2] == (True, COMPLETE)      # 0.72 s
    e._latest = done_job("Turn off the lights and", 100)
    assert e._turn_over(24, 100)[:2] == (False, UNFINISHED)
    assert e._turn_over(67, 100)[0] is True                    # 2.0 s


def test_turn_over_ignores_stale_partial():
    e = make_ears()
    e._latest = done_job("Turn off the lights.", 50)          # they kept talking after it
    end, verdict, _, src = e._turn_over(24, 100)
    assert (end, verdict) == (False, UNKNOWN) and src == "stale partial"
    assert e._turn_over(27, 100)[0] is True                    # plain 800 ms timeout


def test_turn_over_waits_for_early_final():
    from walter.ears import _Job
    e = make_ears()
    e._latest = _Job(0, b"", 110, True)                        # still transcribing
    assert e._turn_over(37, 100)[0] is False                   # 1.1 s: worth waiting
    e._latest.text = "Is it going to rain tomorrow?"
    e._latest.done.set()
    assert e._turn_over(37, 100)[:2] == (True, COMPLETE)
