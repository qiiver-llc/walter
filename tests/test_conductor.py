import asyncio
import time

from walter.config import Config
from walter.conductor import Conductor


class FakeVoice:
    def __init__(self):
        self.said = []
        self.generation = 0

    def say(self, t):
        self.said.append(t)

    def put(self, k, v=None):
        pass

    def stop(self):
        self.generation += 1


def make():
    sent = []
    c = Conductor(Config(), brain=None, send=sent.append)
    c.voice = FakeVoice()
    return c, sent


def run(coro_fn):
    return asyncio.run(coro_fn())


def test_ignores_room_chatter():
    async def go():
        c, _ = make()
        c.on_heard("pass me the salt please")
        assert c.inbox.empty()
    run(go)


def test_wake_word_submits():
    async def go():
        c, _ = make()
        c.on_heard("Hey Walter, what's the weather like?")
        assert c.inbox.get_nowait() == ("voice", "Hey Walter, what's the weather like?")
    run(go)


def test_bare_name_gets_instant_ack_without_brain():
    async def go():
        c, _ = make()
        c.on_heard("Hey Walter.")
        assert c.inbox.empty()
        assert c.voice.said and c.state == "listening"
    run(go)


def test_followup_window_needs_no_name():
    async def go():
        c, _ = make()
        c.followup_until = time.time() + 5
        c.on_heard("and what about tomorrow")
        assert not c.inbox.empty()
    run(go)


def test_quiet_hours_wrap_midnight():
    c, _ = make()
    c.cfg.life.quiet_hours = [22, 8]
    import datetime as dt
    h = dt.datetime.now().hour
    assert c.in_quiet_hours() == (h >= 22 or h < 8)


def test_wakes_from_sleep_on_his_name_mid_sentence():
    async def go():
        c, sent = make()
        c.sleeping = True
        c.state = "sleeping"
        c.on_hear_start()
        c.on_hear_partial("Hey Walter, can you")        # live transcript, still talking
        assert not c.sleeping and c.state == "listening"
        c.on_heard("Hey Walter, can you turn the music down")
        assert c.inbox.get_nowait() == ("voice", "Hey Walter, can you turn the music down")
    run(go)


def test_long_answer_still_counts_after_followup_window():
    async def go():
        c, _ = make()
        c.followup_until = time.time() + 1       # he just asked something
        c.state = "idle"
        c.on_hear_start()                        # you start a long answer...
        c.followup_until -= 5                    # ...that outlasts the original window
        c.on_hear_partial("well I think we should probably")
        c.on_heard("well I think we should probably go on Saturday instead")
        assert not c.inbox.empty()
    run(go)


def test_followup_counts_from_when_speech_started():
    # Long answer begun inside the window, transcribed after it closed.
    async def go():
        c, _ = make()
        c.utterance_started = time.time() - 8
        c.followup_until = time.time() - 2
        c.on_heard("that's almost too low, bring it up a little")
        assert not c.inbox.empty()
        assert c.utterance_started is None
    run(go)


def test_speech_started_after_window_is_ignored():
    async def go():
        c, _ = make()
        c.followup_until = time.time() - 10
        c.utterance_started = time.time() - 3
        c.on_heard("pass me the salt please")
        assert c.inbox.empty()
    run(go)


def test_dropped_noise_clears_utterance_in_progress():
    async def go():
        c, _ = make()
        c.on_hear_start()
        assert c.utterance_started is not None
        c.on_hear_dropped()
        assert c.utterance_started is None
    run(go)


def test_ignored_log_before_any_followup_window():
    c, _ = make()
    assert c._followup_gap(time.time()) == "no follow-up window yet"
    c.followup_until = time.time() - 30
    assert c._followup_gap(time.time()) == "30s after follow-up window"


UNKNOWN = {"name": None, "score": 0.2, "threshold": 0.5, "candidate": "Alex"}
KNOWN = {"name": "Alex", "score": 0.8, "threshold": 0.5, "candidate": "Alex"}


def _heard_as(c, match, text):
    c.on_hear_speaker(match)
    c.on_heard(text)


def test_unknown_speaker_chatter_in_followup_is_ignored():
    async def go():
        c, _ = make()
        _heard_as(c, KNOWN, "Hey Walter, what's the weather like?")
        c.inbox.get_nowait()
        c.followup_until = time.time() + 5
        _heard_as(c, UNKNOWN, "yeah I told her that yesterday")
        assert c.inbox.empty()
    run(go)


def test_known_speaker_continues_in_followup():
    async def go():
        c, _ = make()
        _heard_as(c, KNOWN, "Hey Walter, what's the weather like?")
        c.inbox.get_nowait()
        c.followup_until = time.time() + 5
        _heard_as(c, KNOWN, "and what about tomorrow")
        assert not c.inbox.empty()
    run(go)


def test_unknown_speaker_clear_request_is_answered():
    async def go():
        c, _ = make()
        _heard_as(c, KNOWN, "Hey Walter, what's the weather like?")
        c.inbox.get_nowait()
        c.followup_until = time.time() + 5
        _heard_as(c, UNKNOWN, "what about tomorrow?")
        assert not c.inbox.empty()
    run(go)


def test_unknown_speaker_using_name_then_continuing():
    async def go():
        c, _ = make()
        _heard_as(c, UNKNOWN, "Hey Walter, what's the weather like?")
        assert not c.inbox.empty()
        c.inbox.get_nowait()
        c.followup_until = time.time() + 5
        _heard_as(c, UNKNOWN, "and tomorrow then")
        assert not c.inbox.empty()
    run(go)


def test_unknown_voice_but_partner_in_view_continues():
    class P:
        recognise = True

        def who_now(self, w=None):
            return ["Alex"]

        def __getattr__(self, name):   # any other presence hook: a no-op
            return lambda *a, **k: None

    async def go():
        c, _ = make()
        c.presence = P()
        _heard_as(c, KNOWN, "Hey Walter, what's the weather like?")
        c.inbox.get_nowait()
        c.followup_until = time.time() + 5
        _heard_as(c, UNKNOWN, "and tomorrow then")
        assert not c.inbox.empty()
    run(go)


def test_no_voice_info_keeps_old_followup_rule():
    async def go():
        c, _ = make()
        c.followup_until = time.time() + 5
        c.on_heard("and tomorrow then")
        assert not c.inbox.empty()
    run(go)


def test_unknown_voice_after_my_name_with_no_known_partner_is_answered():
    async def go():
        c, _ = make()
        c.followup_until = time.time() + 5       # my name / my reply just opened the window
        c._conv_speaker = None                   # nobody known is my partner (weak voice match)
        _heard_as(c, UNKNOWN, "I also want you to talk about how the voice stays local")
        assert not c.inbox.empty()
    run(go)


# ---------------------------------------------------------------- T53: room chatter
def _chatter_conductor():
    from tests.test_barge_in import FakeBrain, FakeEars, FakeVoice as BargeVoice
    c = Conductor(Config(), brain=FakeBrain(["[silent]"]), send=lambda m: None)
    c.voice = BargeVoice()
    c.ears = FakeEars()
    return c


async def _turn(c):
    """Run the one queued turn through the (fake, always [silent]) brain, as run() would."""
    item = c.inbox.get_nowait()
    source, text, meta = item[0], item[1], (item[2] if len(item) > 2 else None)
    c._turn = {"source": source, "text": text, "said": []}
    await c._respond(source, text, meta)
    c._turn, c.busy = None, False
    return meta


CHATTER = ["yeah the parking out there was a nightmare", "so then he moved the whole thing to Tuesday",
           "honestly the coffee here is better than last year", "that panel ran long again",
           "we should grab food after this"]


def test_room_chatter_gets_no_ack_and_no_engagement():
    async def go():
        c = _chatter_conductor()
        c.followup_until = time.time() + 0.3      # a window left open by my last reply, about to close
        c._conv_speaker = None                    # nobody known is my partner
        _heard_as(c, UNKNOWN, CHATTER[0])         # passes (the weak-voice relaxation)...
        assert not c.inbox.empty() and c.voice.said == []   # ...but no spoken "One sec."
        before = c.followup_until
        meta = await _turn(c)
        assert meta.get("quiet_ack") and c.voice.said == []
        assert c.followup_until <= before          # a [silent] reply doesn't extend the window
        await asyncio.sleep(0.8)                   # the window runs out: I rest, not "listening"
        assert not c.engaged()
        for line in CHATTER[1:]:
            _heard_as(c, UNKNOWN, line)
            assert c.inbox.empty() and c.voice.said == []
    run(go)


def test_three_silent_chatter_lines_then_my_name_is_needed():
    async def go():
        c = _chatter_conductor()
        for line in CHATTER[:3]:
            c.followup_until = time.time() + 5    # keep the window open: only the run counter stops it
            c._conv_speaker = None
            _heard_as(c, UNKNOWN, line)
            await _turn(c)
        assert c._chatter_run == 3 and c.voice.said == []
        c.followup_until = time.time() + 5
        _heard_as(c, UNKNOWN, CHATTER[3])
        assert c.inbox.empty()                    # my name is needed now
        _heard_as(c, UNKNOWN, "Hey Walter, what time is it in London?")
        assert not c.inbox.empty() and c._chatter_run == 0   # my name works (and resets the run)
    run(go)


def test_owner_weak_voice_follow_up_after_my_name_still_answered():
    async def go():
        c = _chatter_conductor()
        c._chatter_run = 3                        # after a noisy stretch
        _heard_as(c, UNKNOWN, "Hey Walter.")      # the owner, weak voice match: my name
        assert c._chatter_run == 0
        c.followup_until = time.time() + 5
        c._conv_speaker = None
        _heard_as(c, UNKNOWN, "I also want you to talk about how the voice stays local")
        assert not c.inbox.empty()
    run(go)


def test_known_voice_clears_the_chatter_run():
    async def go():
        c = _chatter_conductor()
        c._chatter_run = 5
        c.followup_until = time.time() + 5
        _heard_as(c, KNOWN, "and what about tomorrow")
        assert not c.inbox.empty() and c._chatter_run == 0
    run(go)
