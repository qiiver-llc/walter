"""Focus mode in the conductor: voice commands, held worker reports, the
5-minute warning / check-in, and the note at the top of turns."""
import asyncio
import datetime as dt
import re
import time

from walter import focus as F
from walter import quick
from walter.dashboard import ListStore

from test_barge_in import make

WAKE = re.compile(r"\b(walter)\b", re.I)


def m(text):
    return quick.match(text, WAKE)


def test_voice_patterns():
    c = m("Walter, focus on the network app for 25 minutes")
    assert c.kind == "focus_start" and c.args == {"task": "Network app", "minutes": 25}
    assert m("start a 25 minute box on the network app").args == {"task": "Network app", "minutes": 25}
    assert m("start a twenty-five minute focus box on email").args["minutes"] == 25
    assert m("focus on taxes for half an hour").args["minutes"] == 30
    assert m("Walter, 25 minutes on the network app").kind == "focus_start"
    assert m("two minutes on the eggs") is None          # a kitchen timer, not a box
    for t in ("I'm done", "done", "I'm done with it", "that's done"):
        assert m(t).kind == "focus_done", t
    assert m("five more minutes").args == {"minutes": 5}
    assert m("give me another 15 minutes").args == {"minutes": 15}
    assert m("more time").args["minutes"] == 10
    assert m("park it").kind == "focus_park"
    assert m("stop the timer").kind == "focus_stop"
    for t in ("what's next", "Walter, what's next?", "how much time is left"):
        assert m(t).kind == "focus_next", t
    assert m("add call the bank to my inbox").args == {"text": "Call the bank", "section": "inbox"}
    assert m("I'm done but check the oven") is None


def setup(tmp_path):
    c, sent = make("aec")
    store = ListStore(tmp_path / "list.json", tmp_path / "face-list.json")
    c.list_store = lambda: store
    return c, store


def test_fast_path_runs_a_box_end_to_end(tmp_path):
    async def go():
        c, store = setup(tmp_path)
        store.add("Network app")
        c.submit("voice", "Walter, focus on the network app for 25 minutes")
        assert c.inbox.empty() and F.FocusStore().running()
        assert "Network app" in c.voice.said[-1]
        assert "Focus box running: Network app, 25 min left" in c._context_notes("heartbeat", {})
        c.submit("voice", "five more minutes")
        assert F.FocusStore().box()["planned_min"] == 30
        c.submit("voice", "what's next")
        assert "Network app" in c.voice.said[-1]
        c.submit("voice", "I'm done")
        assert F.FocusStore().box() is None and F.FocusStore().log()[-1]["outcome"] == "done"
        assert all(i.get("done_at") for i in store.items())   # ticked off the list
        # no box: "I'm done" / "five more minutes" are the brain's job
        c.submit("voice", "I'm done")
        c.submit("voice", "five more minutes")
        assert c.inbox.qsize() == 2
    asyncio.run(go())


def test_park_puts_it_on_the_list(tmp_path):
    async def go():
        c, store = setup(tmp_path)
        c.submit("voice", "start a 25 minute box on the budget")
        c.submit("voice", "park it")
        assert [i["text"] for i in store.items()] == ["Budget"]
        assert F.FocusStore().log()[-1]["outcome"] == "parked"
    asyncio.run(go())


def test_worker_reports_wait_for_the_box_but_reminders_dont(tmp_path):
    async def go():
        c, _ = setup(tmp_path)
        c.gap_s = 0
        c.last_heard = c.last_speech = 0
        fs = F.FocusStore()
        fs.start("deep work", 25)
        c.inbox.put_nowait(("task", 'Task #7 "research" done.\nAll good.'))
        c.inbox.put_nowait(("reminder", "the tea is ready"))
        item = await asyncio.wait_for(c._next_item(), 2)
        assert item[0] == "reminder"
        assert [h[1][0] for h in c._held] == ["task"]
        try:
            await asyncio.wait_for(c._next_item(), 0.6)
            assert False, "the worker report should wait for the box"
        except asyncio.TimeoutError:
            pass
        fs.finish("done")
        item = await asyncio.wait_for(c._next_item(), 2)
        assert item[0] == "task"
    asyncio.run(go())


def test_warning_and_checkin_spoken_once_and_away_when_unheard(tmp_path, monkeypatch):
    c, _ = setup(tmp_path)
    fs = F.FocusStore()
    t0 = time.time()
    fs.start("network app", 25, now=t0)
    c._focus_step(t0 + 21.5 * 60, events=[])
    assert c.voice.said[-1].endswith("minutes left on network app.")
    n = len(c.voice.said)
    c._focus_step(t0 + 22 * 60, events=[])
    assert len(c.voice.said) == n
    # muted at the end: the check-in goes to the away feed instead of being said
    away = []
    monkeypatch.setattr(c, "note_away", lambda *a, **k: away.append(a))
    c.user_muted = True
    c._focus_step(t0 + 25 * 60 + 1, events=[])
    assert len(c.voice.said) == n and away and "Done, more time, or park it?" in away[0][1]
    assert "time's up" in c._context_notes("voice", {})


def test_checkin_waits_for_a_quiet_moment(tmp_path):
    c, _ = setup(tmp_path)
    fs = F.FocusStore()
    t0 = time.time()
    fs.start("x task", 10, now=t0 - 601)
    c.busy = True
    c._focus_step(t0, events=[])
    assert not c.voice.said and c._focus_due
    c.busy = False
    c._focus_step(t0 + 5, events=[])
    assert "Done, more time, or park it?" in c.voice.said[-1]
    assert c.followup_until > time.time()


def test_heads_up_for_a_calendar_event(tmp_path):
    c, _ = setup(tmp_path)
    now = time.time()
    F.FocusStore().start("deep work", 60, now=now)
    start = dt.datetime.fromtimestamp(now + 9 * 60).replace(microsecond=0).isoformat()
    c._focus_step(now, events=[{"title": "Dentist", "start": start}])
    assert "Heads up: Dentist" in c.voice.said[-1]


def test_done_is_just_a_word_without_a_box(tmp_path):
    c, _ = setup(tmp_path)
    assert c._fast_match("I'm done") is None and c._fast_match("five more minutes") is None
    assert c._fast_match("what's next").kind == "focus_next"
    F.FocusStore().start("x", 10)
    assert c._fast_match("I'm done").kind == "focus_done"
