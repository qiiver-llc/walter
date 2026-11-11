"""Sports feed (walter/sports.py): recorded ESPN fixture JSON only, never the network."""
import copy
import datetime as dt
import json
from pathlib import Path

import pytest

from walter import sports as SP

FIX = Path(__file__).resolve().parent / "fixtures" / "espn"
# recorded 6 Nov 2026, teams renamed (fictional NFD and RVC): NFD 5-3 (last: L 10-17 vs WASH, next: Fri 8 PM ET at ILL), RVC 5-2
NOW = dt.datetime(2026, 11, 6, 18, 0, tzinfo=dt.timezone.utc).timestamp()   # 12:00 PM Central


def load(name):
    return json.loads((FIX / name).read_text())


def live_team():
    """The Northfield team document as if the Illinois game were in the 3rd quarter."""
    t = load("cfb-9001-team.json")
    c = t["team"]["nextEvent"][0]["competitions"][0]
    c["status"] = {"clock": 252, "displayClock": "4:12", "period": 3,
                   "type": {"name": "STATUS_IN_PROGRESS", "state": "in", "completed": False,
                            "shortDetail": "4:12 - 3rd"}}
    for x, s in zip(c["competitors"], ("10", "21")):
        x["score"] = {"value": float(s), "displayValue": s}
    return t


def final_team():
    t = live_team()
    c = t["team"]["nextEvent"][0]["competitions"][0]
    c["status"] = {"period": 4, "type": {"name": "STATUS_FINAL", "state": "post", "completed": True, "shortDetail": "Final"}}
    for x in c["competitors"]:
        x["winner"] = x["team"]["abbreviation"] == "NFD"
    return t


def fake_get(docs, calls=None):
    def get(url):
        calls is not None and calls.append(url)
        for key, doc in docs.items():
            if url.endswith(key):
                return copy.deepcopy(doc)
        raise OSError("offline: " + url)
    return get


def recorded():
    return {"college-football/teams/9001": load("cfb-9001-team.json"),
            "college-football/teams/9001/schedule": load("cfb-9001-schedule.json"),
            "nfl/teams/rvc": load("nfl-rvc-team.json"),
            "nfl/teams/rvc/schedule": load("nfl-rvc-schedule.json")}


def test_parse_team_specs():
    assert SP.parse_team("college-football/9001") == ("football", "college-football", "9001")
    assert SP.parse_team("NFL:RVC") == ("football", "nfl", "rvc")
    assert SP.parse_team("basketball/nba/lal") == ("basketball", "nba", "lal")
    assert SP.parse_team("nope") is None and SP.parse_team("cricket/x") is None


def test_summary_last_and_next_from_recorded_data():
    e = SP.summarize(load("cfb-9001-team.json"), load("cfb-9001-schedule.json"), "football", "college-football", NOW)
    assert e["abbr"] == "NFD" and e["record"] == "5-3" and e["color"] == "2a6f3b"
    assert e["live"] is None
    last, nxt = e["last"], e["next"]
    assert (last["result"], last["us"]["score"], last["them"]["score"], last["them"]["abbr"], last["home"]) == ("L", 10, 17, "WASH", True)
    assert last["detail"] == "Final"
    assert nxt["them"]["abbr"] == "ILL" and not nxt["home"] and nxt["start"] == "2026-11-07T01:00Z" and not nxt["tbd"]
    k = SP.summarize(load("nfl-rvc-team.json"), load("nfl-rvc-schedule.json"), "football", "nfl", NOW)
    assert (k["abbr"], k["last"]["result"], k["last"]["them"]["abbr"], k["next"]["them"]["abbr"], k["next"]["home"]) == ("RVC", "L", "DEN", "NYJ", True)


def test_overtime_final_and_tbd():
    s = load("nfl-rvc-schedule.json")
    e = SP.summarize(None, s, "football", "nfl", dt.datetime(2026, 9, 22, tzinfo=dt.timezone.utc).timestamp())
    assert e["last"]["detail"] == "Final/OT" and e["last"]["result"] == "W"
    e = SP.summarize(None, load("cfb-9001-schedule.json"), "football", "college-football",
                     dt.datetime(2026, 11, 15, tzinfo=dt.timezone.utc).timestamp())
    assert e["next"]["them"]["abbr"] == "OSU" and e["next"]["tbd"]


def test_live_game_wins_over_schedule():
    e = SP.summarize(live_team(), load("cfb-9001-schedule.json"), "football", "college-football", NOW + 8 * 3600)
    g = e["live"]
    assert g and g["state"] == "in" and g["detail"] == "Q3 4:12"
    assert (g["us"]["abbr"], g["us"]["score"], g["them"]["abbr"], g["them"]["score"]) == ("NFD", 21, "ILL", 10)
    assert e["next"]["them"]["abbr"] == "RUTG"   # the live game is not also "next"


def test_halftime_and_postponed():
    t = live_team()
    c = t["team"]["nextEvent"][0]["competitions"][0]
    c["status"]["type"]["name"] = "STATUS_HALFTIME"
    assert SP.parse_event(t["team"]["nextEvent"][0], "9001", "football")["detail"] == "Half"
    c["status"] = {"type": {"name": "STATUS_POSTPONED", "state": "post", "completed": False, "description": "Postponed"}}
    e = SP.parse_event(t["team"]["nextEvent"][0], "9001", "football")
    assert e["state"] == "off" and "result" not in e


def test_refresh_writes_face_file_and_live_window(tmp_path):
    out = tmp_path / "dashboard-sports.json"
    calls = []
    d = SP.refresh(["college-football/9001", "nfl/rvc"], now=NOW, get=fake_get(recorded(), calls), out=out, rows=2)
    assert d["ok"] and [t["abbr"] for t in d["teams"]] == ["NFD", "RVC"] and d["rows"] == 2
    assert d["tz"] == SP.local_tz_name() and not d["live"]
    assert len(calls) == 4
    assert json.loads(out.read_text())["teams"][0]["next"]["them"]["abbr"] == "ILL"
    # live-only far from kick-off: no network at all
    calls.clear()
    r = SP.refresh(["college-football/9001", "nfl/rvc"], live_only=True, now=NOW, get=fake_get({}, calls), out=out)
    assert r.get("skipped") and calls == []
    # 10 minutes before kick-off: the window opens (only the small team endpoint, only for NFD)
    kick = SP._ts("2026-11-07T01:00Z")
    assert SP.live_window(d, kick - 600) and not SP.live_window(d, kick - 3600)
    docs = recorded()
    docs["college-football/teams/9001"] = live_team()
    calls.clear()
    r = SP.refresh(["college-football/9001", "nfl/rvc"], live_only=True, now=kick + 5400, get=fake_get(docs, calls), out=out)
    assert calls == [SP.API + "/football/college-football/teams/9001"]
    fav = r["teams"][0]
    assert fav["live"]["us"]["score"] == 21 and fav["last"]["them"]["abbr"] == "WASH" and r["live"]
    assert r["teams"][1]["abbr"] == "RVC"
    # final whistle: the next live poll fetches the schedule again to get the result
    docs["college-football/teams/9001"] = final_team()
    sched = docs["college-football/teams/9001/schedule"]
    ev = next(e for e in sched["events"] if e["shortName"] == "NFD @ ILL")
    ev["competitions"] = final_team()["team"]["nextEvent"][0]["competitions"]
    calls.clear()
    r = SP.refresh(["college-football/9001", "nfl/rvc"], live_only=True, now=kick + 4 * 3600, get=fake_get(docs, calls), out=out)
    assert SP.API + "/football/college-football/teams/9001/schedule" in calls
    fav = r["teams"][0]
    assert fav["live"] is None and fav["last"]["them"]["abbr"] == "ILL" and fav["last"]["result"] == "W"
    assert fav["next"]["them"]["abbr"] == "RUTG"


def test_offline_keeps_last_good_data(tmp_path):
    out = tmp_path / "s.json"
    SP.refresh(["college-football/9001", "nfl/rvc"], now=NOW, get=fake_get(recorded()), out=out)
    good = json.loads(out.read_text())
    d = SP.refresh(["college-football/9001", "nfl/rvc"], now=NOW, get=fake_get({}), out=out)
    assert d["ok"] is False and d["updated"] == good["updated"] and len(d["teams"]) == 2 and "offline" in d["error"]
    # one team failing keeps the other fresh and the failed one's last entry
    docs = recorded()
    del docs["nfl/teams/rvc"]
    d = SP.refresh(["college-football/9001", "nfl/rvc"], now=NOW, get=fake_get(docs), out=out)
    assert d["ok"] and [t["abbr"] for t in d["teams"]] == ["NFD", "RVC"] and "nfl/rvc" in d["error"]
    # never fetched and offline: an empty, not-ok file (the face hides the row)
    d = SP.refresh(["nfl/rvc"], now=NOW, get=fake_get({}), out=tmp_path / "new.json")
    assert d["ok"] is False and d["teams"] == []


def test_no_teams_means_empty(tmp_path):
    d = SP.refresh([], now=NOW, get=fake_get({}), out=tmp_path / "s.json")
    assert d["ok"] and d["teams"] == []


def test_config_default_teams():
    """No teams by default (personal choice, set in ~/.walter/config.toml); new extras are off."""
    from walter.config import Config
    c = Config()
    assert c.dashboard.sports_teams == [] and c.dashboard.sports_rows == 2
    assert c.dashboard.calendar_colors == {} and c.dashboard.verse_line is False


def test_games_window_for_the_month_calendar():
    """Results and upcoming games across the month grid (about 5 weeks back and 6 ahead), compact."""
    e = SP.summarize(load("cfb-9001-team.json"), load("cfb-9001-schedule.json"), "football", "college-football", NOW)
    g = e["games"]
    assert g and all(NOW - SP.GAMES_BACK_S <= SP._ts(x["start"]) <= NOW + SP.GAMES_AHEAD_S for x in g)
    assert [SP._ts(x["start"]) for x in g] == sorted(SP._ts(x["start"]) for x in g)
    wash = next(x for x in g if x["them"]["abbr"] == "WASH")
    assert (wash["result"], wash["us"]["score"], wash["them"]["score"], wash["state"]) == ("L", 10, 17, "post")
    ill = next(x for x in g if x["them"]["abbr"] == "ILL")
    assert ill["state"] == "pre" and not ill["home"] and "result" not in ill


def test_live_only_pass_keeps_and_updates_games(tmp_path):
    out = tmp_path / "dashboard-sports.json"
    SP.refresh(["college-football/9001"], now=NOW, get=fake_get(recorded()), out=out)
    kick = SP._ts("2026-11-07T01:00Z")
    docs = recorded()
    docs["college-football/teams/9001"] = live_team()
    r = SP.refresh(["college-football/9001"], live_only=True, now=kick + 5400, get=fake_get(docs), out=out)
    g = r["teams"][0]["games"]
    ill = next(x for x in g if x["them"]["abbr"] == "ILL")
    assert ill["state"] == "in" and ill["us"]["score"] == 21 and ill["detail"] == "Q3 4:12"
    assert any(x["them"]["abbr"] == "WASH" and x["result"] == "L" for x in g)   # the stored result stays
