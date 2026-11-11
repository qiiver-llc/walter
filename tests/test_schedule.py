import datetime as dt
from pathlib import Path

from walter.conductor import Reminders, next_occurrence


def ts(*a):
    return dt.datetime(*a).timestamp()


def test_next_occurrence_same_day_later():
    # Mon 2026-11-02 07:00 -> weekdays 07:30 -> same day
    assert next_occurrence(list(range(5)), "07:30", ts(2026, 11, 2, 7, 0)) == ts(2026, 11, 2, 7, 30)


def test_next_occurrence_skips_weekend():
    # Fri 2026-11-06 08:00, weekdays 07:30 -> Mon 2026-11-09
    assert next_occurrence(list(range(5)), "07:30", ts(2026, 11, 6, 8, 0)) == ts(2026, 11, 9, 7, 30)


def test_next_occurrence_strictly_after():
    t = ts(2026, 11, 2, 7, 30)
    assert next_occurrence([0], "07:30", t) == ts(2026, 11, 9, 7, 30)


def test_fire_due_reschedules_routines(tmp_path: Path):
    r = Reminders(tmp_path / "r.json", conductor=None)
    once = r.add(ts(2026, 11, 2, 7, 0), "tea", "remind")
    routine = r.add(None, "briefing", "do", {"days": list(range(7)), "time": "07:30"})
    routine["due"] = ts(2026, 11, 2, 7, 30)
    fired = r.fire_due(ts(2026, 11, 2, 7, 31))
    assert {f["text"] for f in fired} == {"tea", "briefing"}
    assert [i["text"] for i in r.items] == ["briefing"]
    assert r.items[0]["due"] == ts(2026, 11, 3, 7, 30)
    # persisted
    assert "briefing" in (tmp_path / "r.json").read_text()
    assert once["id"] != routine["id"]
