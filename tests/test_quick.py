"""Fast path, intent checks and acknowledgements (walter/quick.py)."""
import datetime as dt
import re

from walter import quick as q

WAKE = re.compile(r"\b(walter)\b", re.I)
NOW = dt.datetime(2026, 11, 5, 14, 0)


def m(text):
    return q.match(text, WAKE, NOW)


def test_list_add_and_notes():
    assert m("Walter, add eggs to my shopping list.").args == {"text": "Eggs", "section": "shopping"}
    assert m("Hey Walter, can you put call the vet on my list please?").args == \
        {"text": "Call the vet", "section": "tasks"}
    assert m("Make a note: buy oil").args == {"text": "Buy oil", "section": "notes"}
    assert m("note that the gate code changed").args["section"] == "notes"


def test_list_show():
    assert m("Walter, what's on my list?").kind == "list_show"
    assert m("read me my shopping list").args == {"section": "shopping"}


def test_reminders_in_and_at():
    c = m("remind me in 20 minutes to check the oven")
    assert c.kind == "remind" and c.args["text"] == "Check the oven" and c.args["in_s"] == 1200
    assert m("remind me to stir the soup in five minutes").args["in_s"] == 300
    c = m("Walter remind me to take the bins out at 5")
    assert c.args["at"] == "5:00 PM" and dt.datetime.fromtimestamp(c.args["due"]).hour == 17
    c = m("remind me at 7:30 am to water the plants")
    when = dt.datetime.fromtimestamp(c.args["due"])
    assert (when.day, when.hour, when.minute) == (6, 7, 30)   # tomorrow morning


def test_volume_mute_sleep_ears_time_here():
    assert m("volume up").args == {"args": ["up"]}
    assert m("Walter, louder").args == {"args": ["up"]}
    assert m("set the volume to 40 percent").args == {"args": ["set", "40"]}
    assert m("volume to 400") is None
    assert m("mute").kind == "mute" and m("Walter, go to sleep").kind == "sleep"
    assert m("use local ears").args == {"provider": "local"}
    assert m("ears cloud").args == {"provider": "openai"}
    assert m("What time is it?").kind == "time"
    assert m("Walter, are you there?").kind == "here"
    assert m("Can you hear me, Walter?").kind == "here"


def test_unsure_falls_through_to_the_brain():
    for t in ["add it to my list", "add eggs to my list and then tell me the weather",
              "what time is it in Tokyo?", "What is the weather?", "remind me later to call",
              "mute the TV", "Walter, hear me out on this", "remind me in 90 hours to x",
              "Turn it up. Then play some music."]:
        assert m(t) is None, t


def test_clear_requests_vs_room_chatter():
    # side chatter cancelled his turn every time
    for t in ["There goes the Wi-Fi again.", "It shows me like workers it's working on.",
              "All kinds of stuff.", "This is all on the cloudiers.", "Now we're gonna do some stuff.",
              "No, he's obviously...", "It's a problem, I think.",
              "Something's going on here. Anyways, it is really freaking awesome when it works. It's "
              "a full thanks, this row. Oh, right. Yeah. Why not the entire computer so it has "
              "access to everything?"]:
        assert not q.is_clear_request(t), t
    for t in ["actually, what time is it in Tokyo?", "check the calendar for tomorrow",
              "can you look up the score", "What's the weather tomorrow?", "add milk to the list"]:
        assert q.is_clear_request(t), t


def test_presence_check_and_replaced():
    assert q.is_presence_check("Walter, are you there?", WAKE)
    assert q.is_presence_check("Can you hear me, Walter?", WAKE)
    assert not q.is_presence_check("Walter, are you there yet with the weather?", WAKE)
    assert q.replaces_previous("never mind, what's the time")
    assert q.replaces_previous("forget that, tell me a joke")
    assert not q.replaces_previous("and what's the weather")


def test_acks_only_for_work_and_never_twice_in_a_row():
    assert q.needs_ack("what's the weather tomorrow")
    assert not q.needs_ack("say: pasta tonight")
    assert q.needs_ack("check my calendar for Friday")
    assert q.needs_ack("why is the sky blue", deep=True)
    assert not q.needs_ack("thanks Walter")
    assert not q.needs_ack("tell me a joke")
    assert not q.needs_ack("how are you")
    p = q.Picker(q.ACK_LINES)
    picks = [p.pick() for _ in range(200)]
    assert all(a != b for a, b in zip(picks, picks[1:]))
    assert len(set(picks)) > 3


def test_spoken_helpers():
    assert q.spoken_duration(1200) == "in 20 minutes"
    assert q.spoken_duration(3600) == "in an hour"
    assert q.spoken_list(["Eggs"]) == "You've got 1 thing: Eggs."
    assert q.spoken_list(["a", "b", "c"]) == "You've got 3 things: a, b, and c."
    assert q.spoken_list(list("abcdefg")) == "You've got 7 things: a, b, c, d, e, and 2 more."
