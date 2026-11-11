"""Never lose a request, answer instantly (UX audit items 1 and 2, 5 Nov 2026).

Replays the 08:40 demo storm (side chatter and "Walter, are you there?" each
cancelled the turn in flight, so nothing was ever answered), pending requests,
mute letting a request finish quietly, the local fast path and acknowledgements.
"""
import asyncio
import time

from walter import quick
from walter.dashboard import ListStore

from test_barge_in import FakeBrain, make


class ToolBrain(FakeBrain):
    """Calls a tool before answering, like a calendar lookup."""

    async def ask(self, text, model=None):
        self.asked.append(text)
        yield ("tool", "calendar")
        await asyncio.sleep(0)
        for c in self.chunks:
            yield ("text", c)
        yield ("done", "")


class FakeReminders:
    def __init__(self):
        self.items = []

    def add(self, due, text, kind="remind", repeat=None):
        self.items.append((due, text))
        return {"id": len(self.items)}


def thinking(c, asked="Walter, what's the weather this weekend?"):
    c.busy = True
    c._turn = {"source": "voice", "text": asked, "said": []}
    c.set_state("thinking")
    c.followup_until = time.time() + 10


def hear(c, text):
    c.on_hear_start()
    c.on_hear_end()
    c.on_heard(text)


def test_demo_storm_chatter_and_are_you_there_never_cancel():
    async def go():
        c, _ = make("aec")
        thinking(c)
        for t in ["There goes the Wi-Fi again.", "This is all on the cloudiers.",
                  "It shows me like workers it's working on.", "All kinds of stuff."]:
            hear(c, t)
        hear(c, "Walter, are you there?")
        hear(c, "Can you hear me, Walter?")
        await asyncio.sleep(0)
        assert c.brain.interrupts == 0 and c.voice.stops == 0 and c.inbox.empty()
        assert len(c.voice.said) == 2          # "I'm here, still working on it." etc., at once
        assert c.request_counts["here_answered"] == 2
        assert c.request_counts["asides_kept"] == 4
        # the chatter rides along with the next turn as context
        notes = c._context_notes("voice", {})
        assert "Also heard in the room" in notes and "Wi-Fi" in notes
    asyncio.run(go())


def test_addressed_request_while_thinking_keeps_the_old_one_pending_until_answered():
    async def go():
        c, _ = make("aec")
        c.brain = FakeBrain(["Sunny on Saturday, and Bob's game is at ten."])
        thinking(c, "Walter, what's the weather this weekend?")
        hear(c, "Walter, and when is the game on Saturday?")
        await asyncio.sleep(0)
        assert c.brain.interrupts == 1
        assert [p["text"] for p in c.pending] == ["Walter, what's the weather this weekend?"]
        src, text, *meta = c.inbox.get_nowait()
        c.busy = False
        await c._respond(src, text, meta[0] if meta else None)
        sent = c.brain.asked[-1]
        assert "Still unanswered" in sent and "weather this weekend" in sent
        assert "when is the game" in sent
        assert c.pending == [] and c.request_counts["pending_answered"] == 1
    asyncio.run(go())


def test_never_mind_replaces_it_and_stop_drops_it():
    async def go():
        c, _ = make("aec")
        thinking(c, "Walter, research lawn mowers")
        hear(c, "Walter, never mind, what's the weather like?")
        assert c.pending == [] and c.request_counts["replaced"] == 1
        c2, _ = make("aec")
        thinking(c2, "Walter, research lawn mowers")
        hear(c2, "stop")
        await asyncio.sleep(0)
        assert c2.pending == [] and c2.request_counts["stopped"] == 1 and c2.brain.interrupts == 1
    asyncio.run(go())


def test_pending_requests_expire_with_a_count():
    c, _ = make("aec")
    c._keep_pending("look up the score")
    c.pending[0]["at"] -= 3600
    assert c._context_notes("voice", {}) == ""
    assert c.request_counts["pending_expired"] == 1


def test_mute_mid_turn_finishes_quietly_and_lands_away():
    async def go():
        c, _ = make("aec")
        landed = []
        c.away_sink = lambda title, body: landed.append((title, body))
        c.brain = FakeBrain(["[happy] First part. ", lambda: c.set_user_mute(True),
                             "Second part, after the mute.", " Third."])
        await c._respond("voice", "what's on my calendar today?")
        assert c.brain.interrupts == 0                     # not cancelled
        assert c.request_counts["finished_quietly"] == 1
        assert landed and landed[0][0].startswith('You asked: "what\'s on my calendar')
        assert "Second part, after the mute." in landed[0][1] and "Third." in landed[0][1]
        assert "Second part, after the mute." not in c.voice.said   # nothing said after the mute
    asyncio.run(go())


def test_queued_requests_survive_mute_and_go_quietly_while_asleep():
    async def go():
        c, _ = make("aec")
        landed = []
        c.away_sink = lambda title, body: landed.append((title, body))
        c.inbox.put_nowait(("voice", "add it up for me"))
        c.inbox.put_nowait(("heartbeat", ""))
        c.go_to_sleep()
        assert [i[0] for i in c.inbox._queue] == ["voice"]
        c.brain = FakeBrain(["It comes to forty two."])
        src, text = c.inbox.get_nowait()
        await c._respond(src, text)
        assert c.voice.said == [] and landed == [('You asked: "add it up for me"', "It comes to forty two.")]
        assert c.state == "sleeping"
    asyncio.run(go())


# ------------------------------------------------------------ fast path
def test_fast_path_list_add_and_show_without_the_brain(tmp_path):
    async def go():
        c, _ = make("aec")
        store = ListStore(tmp_path / "dash.json", tmp_path / "face.json")
        c.list_store = lambda: store
        c.followup_until = time.time() + 10
        hear(c, "Walter, add eggs to my shopping list.")
        assert c.inbox.empty() and c.brain.asked == []
        assert [(i["text"], i["section"]) for i in store.items()] == [("Eggs", "shopping")]
        assert any("Eggs" in s for s in c.voice.said)
        hear(c, "Walter, what's on my list?")
        assert c.voice.said[-1] == "You've got 1 thing: Eggs."
        # the brain hears about it on its next turn, once
        c.brain = FakeBrain(["Sure."])
        await c._respond("voice", "Walter, thanks")
        assert "local fast path" in c.brain.asked[-1] and '"Eggs" to their shopping list' in c.brain.asked[-1]
        await c._respond("voice", "Walter, and?")
        assert "local fast path" not in c.brain.asked[-1]
    asyncio.run(go())


def test_fast_path_while_busy_does_not_cancel_the_turn():
    async def go():
        c, _ = make("aec")
        c.reminders = FakeReminders()
        vols = []
        c._run_volume = vols.append
        thinking(c)
        hear(c, "Walter, remind me in 10 minutes to check the oven")
        hear(c, "Walter, volume up")
        hear(c, "what time is it?")
        assert c.brain.interrupts == 0 and c.inbox.empty()
        assert c.reminders.items[0][1] == "Check the oven"
        assert abs(c.reminders.items[0][0] - (time.time() + 600)) < 5
        assert vols == [["up"]]
        assert c.voice.said[-1].rstrip(".").endswith(("AM", "PM"))
        assert c.request_counts["fast_path"] == 3
    asyncio.run(go())


def test_fast_path_falls_through_when_it_cant_do_it():
    c, _ = make("aec")       # no reminders object: the brain sets it
    c.followup_until = time.time() + 10
    hear(c, "Walter, remind me in 10 minutes to check the oven")
    assert c.inbox.get_nowait()[0] == "voice"


def test_fast_mute_and_sleep():
    async def go():
        c, _ = make("aec")
        c.followup_until = time.time() + 10
        hear(c, "Walter, mute")
        assert c.user_muted and c.inbox.empty()
        c.set_user_mute(False)
        hear(c, "Walter, go to sleep")
        assert c.sleeping and c.user_muted
    asyncio.run(go())


# ------------------------------------------------------------ acknowledgements
def test_ack_for_a_tool_turn_and_brain_is_told():
    async def go():
        c, _ = make("aec")
        c.brain = FakeBrain(["Two things today."])
        await c._respond("voice", "Walter, what's on my calendar today?")
        assert c.voice.said[0] in quick.ACK_LINES and c.voice.said[1] == "Two things today."
        assert "instant acknowledgement" in c.brain.asked[-1]
        c.voice.said.clear()
        await c._respond("voice", "Walter, thanks, you're great")
        assert c.voice.said == ["Two things today."]       # chat: no ack
    asyncio.run(go())


def test_ack_on_first_tool_call_if_not_predicted():
    async def go():
        c, _ = make("aec")
        c.brain = ToolBrain(["Found it."])
        await c._respond("voice", "Walter, who won the race")
        assert c.voice.said[0] in quick.ACK_LINES and c.voice.said[-1] == "Found it."
    asyncio.run(go())


def test_early_ack_at_end_of_turn_from_live_words_and_not_twice():
    async def go():
        c, _ = make("aec")
        c.brain = FakeBrain(["Dentist at three."])
        c.on_hear_start()
        c.on_hear_partial("Walter, what's on my calendar for tomorrow")
        c.on_hear_end()
        assert len(c.voice.said) == 1 and c.voice.said[0] in quick.ACK_LINES   # before the transcript
        c.on_heard("Walter, what's on my calendar for tomorrow?")
        src, text, *meta = c.inbox.get_nowait()
        await c._respond(src, text, meta[0] if meta else None)
        assert len(c.voice.said) == 2 and c.voice.said[1] == "Dentist at three."
        assert "instant acknowledgement" in c.brain.asked[-1]
    asyncio.run(go())


def test_no_early_ack_for_chatter_fast_commands_or_when_muted():
    c, _ = make("aec")
    c.followup_until = time.time() + 10
    for words in ["there goes the wifi again", "the weather here is crazy today",
                  "Walter, volume up", "Walter, are you there"]:
        c.on_hear_start()
        c.on_hear_partial(words)
        c.on_hear_end()
    assert c.voice.said == []


def test_ack_clips_are_cached_per_voice_and_instant(tmp_path):
    import concurrent.futures
    from test_speech import FakeSpeech, make_voice
    import numpy as np
    v = make_voice(tmp_path)
    calls = []
    v.synth.synth_timed = lambda text: (calls.append(text), (np.zeros(5, np.int16), 16000, [[text, 0, 0]]))[1]
    v.cache_lines(["One sec."])
    for _ in range(3):
        v._synth_pool.submit(lambda: None).result(5)      # warm-up done
    assert calls == ["One sec."]
    assert v._cached("One sec.", None)[1] == 16000
    v._timed_synth("One sec.")                           # (a cache miss path still stores)
    assert v._cached("Something else.", None) is None
    v.speech = FakeSpeech(audio=(np.ones(100) * 50).astype(np.int16))
    assert v._cached("One sec.", None) is None            # the cloud voice: its own clip
    v.cfg.voice.length_scale = 0.8
    assert v._cached("One sec.", "local") is None         # a voice setting changed: re-synthesise
    assert isinstance(v._synth_pool, concurrent.futures.ThreadPoolExecutor)


def test_latency_line_reports_the_ack():
    from walter.latency import TurnTiming
    t = TurnTiming(speech_end=10.0, turn_end=10.6, text=11.5)
    t.mark("ack", at=11.2)
    t.mark("brain", at=11.6)
    t.mark("first_token", at=14.0)
    t.mark("audio", at=15.0)
    s = t.summary()
    assert "ack 0.6s after turn end" in s and s.startswith("latency 5.0s")
