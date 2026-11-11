"""Focus mode: time boxes, now/next, the inbox review (walter/focus.py)."""
import datetime as dt
import json
import time

import pytest

from walter import focus as F
from walter.dashboard import ListStore


def lists(tmp_path):
    return ListStore(tmp_path / "list.json", tmp_path / "face-list.json")


def test_parse_minutes():
    assert F.parse_minutes("25m") == 25
    assert F.parse_minutes("25") == 25
    assert F.parse_minutes("1h30m") == 90
    assert F.parse_minutes("1h") == 60
    assert F.parse_minutes("90s") == 1.5
    assert F.parse_minutes("25 min") == 25
    for bad in ("", "soon", "0", "9h"):
        with pytest.raises(ValueError):
            F.parse_minutes(bad)


def test_box_lifecycle_warn_checkin_and_log():
    fs = F.FocusStore()
    t0 = 1_000_000.0
    box = fs.start("network app", 25, now=t0)
    assert box["status"] == "running" and box["end"] == t0 + 1500
    face = json.loads(fs.face_out.read_text())
    assert face["box"]["task"] == "network app"
    assert fs.tick(now=t0 + 60) == []
    ev = fs.tick(now=t0 + 20 * 60 + 5)
    assert [e["kind"] for e in ev] == ["warn"] and "Five minutes left on network app" in ev[0]["line"]
    assert fs.tick(now=t0 + 21 * 60) == []          # once only
    ev = fs.tick(now=t0 + 25 * 60 + 1)
    assert [e["kind"] for e in ev] == ["checkin"] and "Done, more time, or park it?" in ev[0]["line"]
    assert fs.box()["status"] == "checkin"
    assert fs.tick(now=t0 + 26 * 60) == []
    # five more minutes from now (time was up)
    b = fs.extend(5, now=t0 + 26 * 60)
    assert b["status"] == "running" and b["end"] == t0 + 31 * 60 and b["warned"]
    e = fs.finish("done", now=t0 + 30 * 60)
    assert e["minutes"] == 30 and e["outcome"] == "done" and fs.box() is None
    assert json.loads(fs.face_out.read_text())["box"] is None
    assert fs.log()[-1]["task"] == "network app"


def test_unanswered_checkin_closes_itself_with_the_planned_time():
    fs = F.FocusStore()
    t0 = 2_000_000.0
    fs.start("taxes", 10, now=t0)
    fs.tick(now=t0 + 601)
    ev = fs.tick(now=t0 + 601 + F.CHECKIN_EXPIRE_S + 5)
    assert ev[0]["kind"] == "expired" and fs.box() is None
    assert fs.log()[-1]["minutes"] == pytest.approx(10, abs=0.1)


def test_starting_a_new_box_closes_the_old_one():
    fs = F.FocusStore()
    fs.start("a", 25, now=100.0)
    fs.start("b", 25, now=700.0)
    assert fs.box()["task"] == "b" and fs.log()[-1]["outcome"] == "switched"
    with pytest.raises(KeyError):
        F.FocusStore(fs.path.with_name("other.json")).finish("done")


def test_calendar_heads_up_once_while_a_box_runs():
    fs = F.FocusStore()
    now = time.time()
    fs.start("deep work", 60, now=now)
    start = dt.datetime.fromtimestamp(now + 8 * 60).replace(microsecond=0)
    evs = [{"title": "Dentist", "start": start.isoformat()}]
    out = fs.tick(now=now, events=evs)
    assert [e["kind"] for e in out] == ["event"] and "Dentist" in out[0]["line"] and "in 8 minutes" in out[0]["line"]
    assert fs.tick(now=now + 5, events=evs) == []


def test_now_next_box_then_top_task_and_event(tmp_path):
    st = lists(tmp_path)
    st.add("Call the vet")
    st.add("Network app")
    st.add("Milk", "shopping")
    now = time.time()
    fs = F.FocusStore()
    soon = dt.datetime.fromtimestamp(now + 3600).replace(second=0, microsecond=0)
    if soon.date() != dt.date.today():
        soon = None
    evs = [{"title": "Practice", "start": soon.isoformat()}] if soon else []
    nn = F.now_next(fs, st.items(), evs, now)
    assert nn["now"] == "Call the vet" and nn["left_min"] is None
    assert nn["next"] == (f"{F.hm(soon.timestamp())} Practice" if soon else "Network app")
    fs.start("Network app", 25, now=now)
    nn = F.now_next(fs, st.items(), [], now + 13 * 60)
    assert nn["now"] == "Network app" and round(nn["left_min"]) == 12 and nn["next"] == "Call the vet"
    assert F.strip_text(nn) == ("Now: Network app - 12 min left", "Next: Call the vet")
    assert "12 minutes left" in F.spoken_now_next(nn)


def test_done_ticks_off_the_matching_task_and_park_keeps_it(tmp_path):
    st = lists(tmp_path)
    st.add("Get the network application done")
    assert F.tick_off(st, "network application")["done_at"]
    assert F.park_on_list(st, "Budget")["text"] == "Budget"
    assert F.park_on_list(st, "budget")["text"] == "Budget"     # not twice
    assert len([i for i in st.items() if not i.get("done_at")]) == 1


def test_wrapup_summary_and_suggestion(tmp_path):
    entries = [{"task": "Network app", "minutes": 25, "outcome": "done", "start": 1},
               {"task": "network app", "minutes": 10, "outcome": "parked", "start": 2},
               {"task": "Budget", "minutes": 20, "outcome": "parked", "start": 3}]
    assert F.summarize_day(entries) == [("Network app", 35.0, True), ("Budget", 20.0, False)]
    st = lists(tmp_path)
    st.add("Budget")
    st.add("Other")
    assert F.suggest_first_box(st.items(), entries) == "Budget"
    assert F.suggest_first_box(st.items(), []) == "Budget"


def test_inbox_review_do_defer_delegate_drop(tmp_path):
    st = lists(tmp_path)
    a = st.add("call the vet", "inbox")
    b = st.add("fix the gate", "inbox")
    c = st.add("order filters", "inbox")
    d = st.add("old idea", "inbox")
    assert [i["text"] for i in F.inbox_items(st.items())] == ["call the vet", "fix the gate", "order filters", "old idea"]
    assert F.review_action(st, "do", a["id"]).startswith("to do: call the vet")
    assert "due" in F.review_action(st, "defer", b["id"], "tomorrow")
    assert F.review_action(st, "delegate", c["id"], "the plumber") == "delegated: Waiting on the plumber: order filters"
    assert F.review_action(st, "drop", d["id"]) == "dropped: old idea"
    secs = {i["text"]: i["section"] for i in st.items()}
    assert secs == {"call the vet": "tasks", "fix the gate": "tasks", "Waiting on the plumber: order filters": "tasks"}
    assert next(i for i in st.items() if i["text"] == "fix the gate")["due"] == \
        (dt.date.today() + dt.timedelta(days=1)).isoformat()
    assert F.inbox_items(st.items()) == []
    with pytest.raises(KeyError):
        F.review_action(st, "drop", 999)


def test_inbox_is_a_list_section(tmp_path):
    from walter import dashboard as D
    assert "inbox" in D.SECTIONS
    assert D.parse_quick("inbox: ring the bank") == ("inbox", "ring the bank")
    st = lists(tmp_path)
    assert st.add("x", "inbox")["section"] == "inbox"


def test_wrapup_has_focus_boxes_inbox_and_first_box():
    import runpy
    from pathlib import Path
    mod = runpy.run_path(str(Path(__file__).resolve().parent.parent / "bin" / "walter-wrapup"))
    now = dt.datetime(2026, 11, 5, 20, 30)
    items = [{"id": 1, "text": "Budget", "section": "tasks"},
             {"id": 2, "text": "Ring the bank", "section": "inbox"}]
    boxes = [{"task": "Network app", "minutes": 25, "outcome": "done", "start": 1},
             {"task": "Budget", "minutes": 20.4, "outcome": "parked", "start": 2}]
    w = mod["build"](now, items, [], boxes)
    assert "Focus: 45 min in 2 boxes: Network app 25 min (done); Budget 20 min" in w["lines"]
    assert "Inbox: 1 to sort (do, defer, delegate or drop)" in w["lines"]
    assert "Tomorrow's first box: 25 min on Budget" in w["lines"]
    assert "45 minutes of focus" in w["sentence"] and "First box tomorrow: Budget?" in w["sentence"]
    assert w["focus_min"] == 45 and w["first_box"] == "Budget"
    plain = mod["build"](now, [], [], [])
    assert not any(l.startswith(("Focus:", "Inbox:", "Tomorrow's first box")) for l in plain["lines"])


def test_demo_box_leaves_no_trace_in_the_log():
    """T52: the presentation's demo box shows like any box but is never logged or summarised."""
    fs = F.FocusStore()
    t0 = dt.datetime.combine(dt.date.today(), dt.time(12, 0)).timestamp()
    fs.start("real work", 25, now=t0)
    fs.finish("done", now=t0 + 600)
    for i in range(3):                       # stopped at the end of each show
        b = fs.start("Finish the pitch deck", 1, now=t0 + 1000 + i * 100, demo=True)
        assert b["demo"] and fs.box()["task"] == "Finish the pitch deck"
        e = fs.finish("stopped", now=t0 + 1010 + i * 100)
        assert e.get("demo")
    fs.start("Finish the pitch deck", 1, now=t0 + 2000, demo=True)   # ran out on its own
    fs.tick(now=t0 + 2061)
    assert fs.tick(now=t0 + 2061 + F.CHECKIN_EXPIRE_S + 5)[0]["kind"] == "expired"
    assert [e["task"] for e in fs.log()] == ["real work"]
    assert [e["task"] for e in fs.today(dt.date.today())] == ["real work"]
    assert [t for t, *_ in F.summarize_day(fs.today(dt.date.today()))] == ["real work"]
    # an older log that still holds a marked demo entry is filtered out of today's summary
    fs._append_log({"id": 99, "task": "Finish the pitch deck", "start": t0 + 3000, "stop": t0 + 3005,
                    "minutes": 0.1, "planned_min": 1, "outcome": "stopped", "demo": True})
    assert [e["task"] for e in fs.today(dt.date.today())] == ["real work"]
