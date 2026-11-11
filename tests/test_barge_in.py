"""Barge-in and context switching (aec.py + conductor), no real audio."""
import asyncio
import re
import threading
import time

from walter import aec
from walter.conductor import STOP_RE, Conductor
from walter.config import Config

WAKE = re.compile(r"\b(walter|wolter)\b", re.I)


# ------------------------------------------------------------ pure logic
def test_echo_of_his_own_words_is_not_a_barge_in():
    said = ["It should be mostly sunny with a light breeze from the south."]
    ok, why = aec.barge_decision("mostly sunny light breeze south", said, "aec", WAKE, STOP_RE)
    assert not ok and "echo" in why


def test_new_words_barge_in_with_aec():
    said = ["It should be mostly sunny with a light breeze from the south."]
    ok, why = aec.barge_decision("actually what time is the game tonight", said, "aec", WAKE, STOP_RE)
    assert ok and why == "voice"


def test_backchannels_and_single_words_dont_barge_in():
    for t in ("okay", "uh huh, thanks", "yeah right", "tomorrow"):
        assert not aec.barge_decision(t, ["Some long reply."], "aec", WAKE, STOP_RE)[0], t


def test_wake_mode_needs_his_name():
    said = ["Here's the forecast for the week."]
    assert not aec.barge_decision("what time is the game tonight", said, "wake", WAKE, STOP_RE)[0]
    assert aec.barge_decision("Walter, what time is the game", said, "wake", WAKE, STOP_RE)[0]
    assert aec.barge_decision("stop", said, "wake", WAKE, STOP_RE)[0]


def test_his_own_name_in_his_reply_is_not_a_wake_word():
    said = ["I'm Walter, your wall buddy."]
    assert not aec.barge_decision("Walter wall buddy", said, "wake", WAKE, STOP_RE)[0]
    assert aec.barge_decision("Walter, how's the oven timer", said, "wake", WAKE, STOP_RE)[0]


def test_interruption_note_format():
    asked = "What's the weather going to be like this weekend? " * 5
    note = aec.interruption_note("voice", asked, ["It's going to be sunny on Saturday.", "Sunday looks"])
    assert note.startswith("[They interrupted you while you were replying to what they asked: \"What's the weather")
    assert note.endswith("]")
    # asked text is clipped to ~100 characters
    first = note.split('"')[1]
    assert len(first) <= 104 and first.endswith("...")
    assert "Sunday looks" in note and "never heard" in note and "don't repeat" in note
    quiet = aec.interruption_note("heartbeat", "", [])
    assert "heartbeat" in quiet and "None of that reply had been spoken yet" in quiet


def test_resolve_modes_and_devices():
    cfg = Config()
    cfg.ears.barge_in = "auto"
    assert aec.resolve(cfg, available=lambda: False) == "wake"
    assert cfg.ears.device == "" and cfg.voice.device == ""
    assert aec.resolve(cfg, available=lambda: True) == "aec"
    assert cfg.ears.device == "walter_aec_mic" and cfg.voice.device == "walter_aec_speaker"
    cfg2 = Config()
    cfg2.ears.barge_in = "off"
    assert aec.resolve(cfg2, available=lambda: True) == "off" and cfg2.ears.device == ""
    cfg3 = Config()
    cfg3.ears.device = "my usb mic"
    assert aec.resolve(cfg3, available=lambda: True) == "aec" and cfg3.ears.device == "my usb mic"


# ------------------------------------------------------------- conductor
class FakeVoice:
    def __init__(self):
        self.said, self.generation, self.stops = [], 0, 0
        self.hushed = False
        self.idle = asyncio.Event()
        self.idle.set()

    def say(self, t):
        self.said.append(t)

    def put(self, k, v=None, sound=True):
        pass

    def stop(self):
        self.generation += 1
        self.stops += 1

    async def wait_idle(self):
        pass


class FakeBrain:
    def __init__(self, chunks=()):
        self.interrupts = 0
        self.asked = []
        self.chunks = list(chunks)

    async def interrupt(self):
        self.interrupts += 1

    async def ask(self, text, model=None):
        self.asked.append(text)
        for c in self.chunks:
            if callable(c):
                c()
                continue
            yield ("text", c)
            await asyncio.sleep(0)
        yield ("done", "")


class FakeEars:
    def __init__(self):
        self.muted, self.speaking, self.user_muted = threading.Event(), threading.Event(), threading.Event()


def make(mode="aec", brain=None):
    sent = []
    c = Conductor(Config(), brain=brain or FakeBrain(), send=sent.append)
    c.voice = FakeVoice()
    c.ears = FakeEars()
    c.barge_mode = mode
    return c, sent


def speaking(c, *sentences, source="voice", asked="What's the weather this weekend?"):
    """Put the conductor mid-reply: a turn running, Walter saying `sentences`."""
    c.busy = True
    c._turn = {"source": source, "text": asked, "said": []}
    c.set_state("speaking")
    for s in sentences:
        c.on_caption(s, [], 1.0)


def test_mic_stays_open_while_speaking_with_barge_in():
    async def go():
        c, _ = make("aec")
        c.set_state("speaking")
        assert c.ears.speaking.is_set() and not c.ears.muted.is_set()
        c2, _ = make("off")
        c2.set_state("speaking")
        assert c2.ears.muted.is_set() and not c2.ears.speaking.is_set()
    asyncio.run(go())


def test_interrupt_while_thinking_sends_combined_message():
    async def go():
        c, _ = make("aec")
        c.busy = True
        c._turn = {"source": "voice", "text": "Find me a good lasagne recipe", "said": []}
        c.set_state("thinking")
        c.followup_until = time.time() + 10
        c.on_hear_start()
        c.on_heard("actually, what time is it in Tokyo?")
        await asyncio.sleep(0)
        assert c.brain.interrupts == 1
        assert c.voice.stops >= 1
        src, text, *meta = c.inbox.get_nowait()
        assert src == "voice" and text == "actually, what time is it in Tokyo?"
        # nothing of the old reply was said: it stays pending and is answered with this one
        assert [p["text"] for p in c.pending] == ["Find me a good lasagne recipe"]
        msg = c._context_notes(src, {}) + c._frame(src, text, meta[0] if meta else None)
        first, rest = msg.split("\n", 1)
        assert first.startswith("[Still unanswered") and "Find me a good lasagne recipe" in first
        assert rest.startswith("[heard through the microphone") and rest.endswith("what time is it in Tokyo?")
    asyncio.run(go())


def test_barge_in_while_speaking_stops_at_once_and_does_not_repeat():
    async def go():
        c, _ = make("aec")
        speaking(c, "It's going to be sunny on Saturday.", "Sunday looks a bit wetter, with")
        c.on_hear_start()
        assert c._utt_over
        c.on_hear_partial("hang on, did the kids' game get moved")
        await asyncio.sleep(0)
        assert c.voice.stops >= 1 and c.brain.interrupts == 1 and c.state == "listening"
        c.on_hear_end()
        c.on_heard("hang on, did the kids' game get moved to Sunday?")
        src, text, meta = c.inbox.get_nowait()
        msg = c._frame(src, text, meta)
        assert "replying to what they asked: \"What's the weather this weekend?\"" in msg
        assert "Sunday looks a bit wetter, with" in msg and "don't repeat" in msg
        assert msg.endswith("did the kids' game get moved to Sunday?")
        assert c.brain.interrupts == 1   # not interrupted twice
    asyncio.run(go())


def test_own_echo_while_speaking_is_ignored():
    async def go():
        c, _ = make("aec")
        speaking(c, "It should be mostly sunny with a light breeze from the south.")
        c.on_hear_start()
        c.on_hear_partial("mostly sunny with a light breeze")
        c.on_hear_end()
        c.on_heard("mostly sunny with a light breeze from the south")
        await asyncio.sleep(0)
        assert c.voice.stops == 0 and c.brain.interrupts == 0 and c.inbox.empty()
        assert c.state == "speaking"
    asyncio.run(go())


def test_wake_mode_only_name_interrupts():
    async def go():
        c, _ = make("wake")
        speaking(c, "Here's what I found about the election results.")
        c.on_hear_start()
        c.on_hear_partial("can you pass the remote")
        assert c.voice.stops == 0
        c.on_hear_partial("Walter, can you set a timer")
        await asyncio.sleep(0)
        assert c.voice.stops >= 1 and c.state == "listening"
    asyncio.run(go())


def test_stop_while_speaking_just_stops():
    async def go():
        c, _ = make("aec")
        speaking(c, "Long story about the history of tea.")
        c.on_hear_start()
        c.on_heard("stop")
        await asyncio.sleep(0)
        assert c.voice.stops >= 1 and c.inbox.empty()
    asyncio.run(go())


def test_backchannel_while_busy_does_not_interrupt():
    async def go():
        c, _ = make("aec")
        c.busy = True
        c._turn = {"source": "voice", "text": "Check the calendar", "said": []}
        c.set_state("thinking")
        c.followup_until = time.time() + 10
        c.on_hear_start()
        c.on_heard("okay, thanks")
        await asyncio.sleep(0)
        assert c.brain.interrupts == 0 and c.inbox.empty()
    asyncio.run(go())


def test_answer_overlapping_his_last_words_still_counts():
    async def go():
        c, _ = make("aec")
        speaking(c, "Want me to set a timer?")
        c.on_hear_start()            # they start answering as he finishes
        c.busy = False
        c._turn = None
        c.on_speaking(False)         # he finished; follow-up window open
        c._talk_until = 0
        c.on_heard("yes please")
        src, text, *meta = c.inbox.get_nowait()
        assert text == "yes please" and not meta   # a normal follow-up, no interruption note
    asyncio.run(go())


def test_queued_speech_is_coalesced_and_other_items_keep_their_place():
    async def go():
        c, _ = make("aec")
        c.inbox.put_nowait(("task", "Task #3 done"))
        c.inbox.put_nowait(("voice", "and also the dentist", {"speaker": "speaker: unknown"}))
        text, meta = c._coalesce("remind me about the vet", {"interrupted": "[note]"})
        assert text == "remind me about the vet\nand also the dentist"
        assert meta["interrupted"] == "[note]" and meta["speaker"] == "speaker: unknown"
        assert c.inbox.get_nowait() == ("task", "Task #3 done") and c.inbox.empty()
    asyncio.run(go())


def test_interrupted_reply_leftovers_are_not_spoken():
    async def go():
        c, _ = make("aec")
        brain = FakeBrain(["[happy] The first sentence is said. ", lambda: c.voice.stop(),
                           "The rest should never be spoken"])
        c.brain = brain
        await c._respond("voice", "tell me a story")
        assert any("first sentence" in s for s in c.voice.said)
        assert not any("never be spoken" in s for s in c.voice.said)
    asyncio.run(go())


def test_run_loop_tracks_what_was_said_for_the_note():
    async def go():
        c, _ = make("aec")
        notes = []

        def cut_in():
            if notes:
                return   # only the first turn gets cut off
            c.on_caption("Once upon a time there was a blob.", [], 1.0)
            c.submit("text", "wait, what's for dinner?")
            notes.append(c.inbox._queue[-1])

        c.brain = FakeBrain(["Once upon a time there was a blob. ", cut_in, "And then..."])
        c.inbox.put_nowait(("voice", "tell me a story"))
        task = asyncio.create_task(c.run())
        for _ in range(50):
            await asyncio.sleep(0)
        task.cancel()
        src, text, meta = notes[0]
        assert 'replying to what they asked: "tell me a story"' in meta["interrupted"]
        assert "Once upon a time there was a blob." in meta["interrupted"]
        assert c.brain.interrupts == 1
        assert len(c.brain.asked) == 2 and "[They interrupted you" in c.brain.asked[1]
    asyncio.run(go())


# ------------------------------------------------- self-echo (cloud ears)
NETWORK = "The network application is on for five this afternoon."


def test_self_echo_matches_garbled_copies_but_not_new_requests():
    said = [NETWORK]
    for heard in ("The network application is on for five this afternoon.",
                  "the network app location is on for 5 this afternoon",
                  "network application is on for five"):
        assert aec.is_self_echo(heard, said), heard
        assert aec.barge_decision(heard, said, "aec", WAKE, STOP_RE) == (False, "self-echo")
    for heard in ("no, tell me about the weather", "actually what time is the game tonight"):
        assert not aec.is_self_echo(heard, said), heard
    assert not aec.is_self_echo("Walter, stop", said)   # short texts are never self-echo
    assert not aec.is_self_echo("five this afternoon", said)
    assert not aec.is_self_echo(NETWORK, [])


def test_new_words_are_fuzzy():
    said = [NETWORK]
    assert aec.new_words("the network app location for 5", said) == ["app", "location", "5"]
    assert aec.new_words("the netwerk aplication this afternoon", said) == []


def test_cloud_echo_of_his_own_sentence_is_dropped_not_an_interruption():
    async def go():
        c, _ = make("aec")
        speaking(c, NETWORK)
        c.on_hear_start()
        c.on_hear_partial("the network app location is on for")      # local partial, garbled
        c.on_hear_partial("the network app location is on for five this after")
        c.on_hear_end()
        c.on_heard("The network application is on for 5 this afternoon.")   # cloud final
        await asyncio.sleep(0)
        assert c.voice.stops == 0 and c.brain.interrupts == 0 and c.inbox.empty()
        assert c.state == "speaking"
        assert c.self_echo_dropped() == 1      # once per utterance
    asyncio.run(go())


def test_echo_in_an_utterance_that_began_before_he_spoke_does_not_stop_him():
    """'barge-in (they were already talking)': the utterance opened just before
    he started (follow-up window), then only his own voice filled it."""
    async def go():
        c, _ = make("aec")
        c.set_state("listening")
        c.followup_until = time.time() + 10
        c.on_hear_start()
        assert not c._utt_over and c._utt_pre
        speaking(c, NETWORK)
        c.on_hear_partial("the network application is on for five")
        await asyncio.sleep(0)
        assert c.voice.stops == 0 and c.state == "speaking"
        c.on_hear_end()
        c.on_heard("The network application is on for five this afternoon.")
        assert c.inbox.empty() and c.voice.stops == 0
        assert c.self_echo_dropped() == 1
    asyncio.run(go())


def test_echo_that_already_stopped_him_is_not_sent_to_the_brain():
    async def go():
        c, _ = make("aec")
        speaking(c, NETWORK)
        c.on_hear_start()
        c.barge_in("voice")           # (say an older partial slipped through)
        c.on_hear_end()
        c.on_heard("the network application is on for five this afternoon")
        assert c.inbox.empty() and c._barge_note is None
        assert c.self_echo["finals"] == 1
    asyncio.run(go())


def test_real_interruptions_still_stop_him():
    async def go():
        for words, final in (("Walter, stop", "Walter, stop"),
                             ("no, tell me about the weather", "no, tell me about the weather")):
            c, _ = make("aec")
            speaking(c, NETWORK)
            c.on_hear_start()
            c.on_hear_partial(words)
            await asyncio.sleep(0)
            assert c.voice.stops >= 1 and c.state == "listening", words
            c.on_hear_end()
            c.on_heard(final)
            await asyncio.sleep(0)
            if "weather" in final:
                src, text, meta = c.inbox.get_nowait()
                assert text == final and "interrupted" in meta
            else:
                assert c.inbox.empty()      # "Walter, stop" just stops
            assert c.self_echo_dropped() == 0
    asyncio.run(go())


def test_already_talking_real_words_still_stop_him():
    async def go():
        c, _ = make("aec")
        c.set_state("listening")
        c.followup_until = time.time() + 10
        c.on_hear_start()
        speaking(c, NETWORK)
        c.on_hear_partial("no wait, tell me about the weather")
        await asyncio.sleep(0)
        assert c.voice.stops >= 1 and c.state == "listening"
    asyncio.run(go())
