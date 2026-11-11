"""Companion app Schedule screen (walter/app_schedule.py) + pausing routines in conductor.Reminders."""
import asyncio
import datetime as dt
import json
from types import SimpleNamespace

import pytest

from walter import app_schedule as S
from walter.conductor import Reminders
from tests.test_app import PROXY, iso, make_app  # noqa: F401  (iso is a fixture)


def ts(*a):
    return dt.datetime(*a).timestamp()


NOW = ts(2026, 11, 11, 10, 0)    # a Wednesday


class FakeLive:
    def __init__(self, life):
        self.life, self.calls = life, []

    def set(self, key, value):
        self.calls.append((key, value))
        setattr(self.life, key.split(".", 1)[1], value)
        return {"ok": True, "applied": [key]}


@pytest.fixture
def rem(tmp_path):
    return Reminders(tmp_path / "reminders.json", conductor=None)


@pytest.fixture
def life():
    return SimpleNamespace(heartbeat_minutes=30, quiet_hours=[22, 8], sleep_review=True)


@pytest.fixture
def sc(rem, life):
    lv = FakeLive(life)
    s = S.Schedule(rem, SimpleNamespace(life=life), live=lambda: lv, now=lambda: NOW,
                   timers=lambda: [{"name": "walter-usage", "description": "usage", "every": "every 5min",
                                    "next": NOW + 60, "last": NOW - 240}])
    s.fake_live = lv
    return s


def apply(sc, body):
    return sc.apply(S.parse(body))


# ---- list
def test_list_groups(sc, rem):
    rem.add(NOW + 600, "tea is ready", "remind")
    rem.add(NOW + 900, "check the build", "do")
    rem.add(None, "Morning briefing: weather\nsecond line", "do", {"days": [0, 1, 2, 3, 4], "time": "08:00"})
    rem.add(None, "SELF-IMPROVEMENT LOOP (quiet)", "do", {"days": list(range(7)), "time": "03:00"})
    st = sc.state()
    assert [r["text"] for r in st["reminders"]] == ["tea is ready"]
    assert [r["text"] for r in st["later"]] == ["check the build"]
    rt = st["routines"]
    assert [r["time"] for r in rt] == ["03:00", "08:00"]          # by time of day
    assert rt[1]["days_label"] == "weekdays" and rt[0]["days_label"] == "daily"
    assert rt[1]["text"] == "Morning briefing: weather\nsecond line"   # full text, the app trims it
    assert rt[1]["enabled"] and rt[1]["role"] == "briefing" and rt[0]["role"] == "self_review"
    assert st["loops"]["heartbeat"] == {"minutes": 30, "min": 5, "max": 120, "quiet_hours": [22, 8],
                                         "custom_prompt": False, "editable": False}
    assert st["loops"]["sleep_review"] == {"on": True}
    assert st["loops"]["routines"] == {"self_review": rt[0]["id"], "briefing": rt[1]["id"]}
    assert st["timers"][0]["name"] == "walter-usage"


# ---- add + validation
def test_add_reminder_later_routine(sc, rem):
    out = apply(sc, {"action": "add", "type": "reminder", "text": "  call the vet ", "time": "18:30"})
    it = rem.get(out["id"])
    assert it["kind"] == "remind" and it["text"] == "call the vet" and it["due"] == ts(2026, 11, 11, 18, 30)
    out = apply(sc, {"action": "add", "type": "later", "text": "check CI", "time": "09:00"})
    assert rem.get(out["id"])["due"] == ts(2026, 11, 12, 9, 0)          # 09:00 already passed -> tomorrow
    out = apply(sc, {"action": "add", "type": "reminder", "text": "dentist", "time": "7:05", "date": "2026-12-01"})
    assert rem.get(out["id"])["due"] == ts(2026, 12, 1, 7, 5)
    out = apply(sc, {"action": "add", "type": "routine", "text": "inbox review", "time": "16:30", "days": "mon,Wed,friday"})
    it = rem.get(out["id"])
    assert it["kind"] == "do" and it["repeat"] == {"days": [0, 2, 4], "time": "16:30"}


@pytest.mark.parametrize("body", [
    {"action": "add", "type": "routine", "text": "x", "time": "25:00", "days": "daily"},
    {"action": "add", "type": "routine", "text": "x", "time": "7:5", "days": "daily"},
    {"action": "add", "type": "routine", "text": "x", "time": "07:30", "days": "mon,funday"},
    {"action": "add", "type": "routine", "text": "x", "time": "07:30", "days": ""},
    {"action": "add", "type": "routine", "text": "x", "time": "07:30", "days": [0, 1]},
    {"action": "add", "type": "routine", "text": "x", "time": "07:30", "days": "daily", "kind": "rm"},
    {"action": "add", "type": "reminder", "text": "", "time": "07:30"},
    {"action": "add", "type": "reminder", "text": "x" * 2001, "time": "07:30"},
    {"action": "add", "type": "reminder", "text": "bell\x07", "time": "07:30"},
    {"action": "add", "type": "reminder", "text": 5, "time": "07:30"},
    {"action": "add", "type": "reminder", "text": "x", "time": "07:30", "date": "11/12/2026"},
    {"action": "add", "type": "timer", "text": "x", "time": "07:30"},
    {"action": "launch"}, [], "x",
    {"action": "edit", "id": "3", "text": "x"}, {"action": "edit", "id": True, "text": "x"},
    {"action": "edit", "id": 3},
    {"action": "delete", "id": 3, "confirm": "yes"},
    {"action": "heartbeat", "minutes": 4}, {"action": "heartbeat", "minutes": 121},
    {"action": "heartbeat", "minutes": 10.5}, {"action": "heartbeat", "minutes": "10"},
    {"action": "heartbeat", "minutes": True}, {"action": "sleep_review", "on": "false"},
])
def test_bad_input_rejected(body):
    with pytest.raises(S.ScheduleError):
        S.parse(body)


def test_dates_must_be_future_and_real(sc):
    for date in ("2026-11-10", "2026-02-30", "2028-01-01"):
        with pytest.raises(S.ScheduleError):
            apply(sc, {"action": "add", "type": "reminder", "text": "x", "time": "08:00", "date": date})


def test_text_limit_is_2000(sc, rem):
    out = apply(sc, {"action": "add", "type": "routine", "text": "y" * 2000, "time": "08:00", "days": "daily"})
    assert len(rem.get(out["id"])["text"]) == 2000


# ---- edit
def test_edit_routine_and_reminder(sc, rem):
    r = rem.add(None, "briefing", "do", {"days": [0], "time": "08:00"})
    apply(sc, {"action": "edit", "id": r["id"], "time": "07:15", "days": "weekends", "text": "new briefing"})
    it = rem.get(r["id"])
    assert it["repeat"] == {"days": [5, 6], "time": "07:15"} and it["text"] == "new briefing"
    assert dt.datetime.fromtimestamp(it["due"]).weekday() in (5, 6)
    apply(sc, {"action": "edit", "id": r["id"], "time": "06:00"})        # days kept
    assert rem.get(r["id"])["repeat"] == {"days": [5, 6], "time": "06:00"}
    one = rem.add(NOW + 60, "tea", "remind")
    apply(sc, {"action": "edit", "id": one["id"], "time": "21:00", "date": "2026-11-20"})
    assert rem.get(one["id"])["due"] == ts(2026, 11, 20, 21, 0)
    with pytest.raises(S.ScheduleError):
        apply(sc, {"action": "edit", "id": one["id"], "days": "daily"})
    with pytest.raises(S.ScheduleError):
        apply(sc, {"action": "edit", "id": r["id"], "date": "2026-11-20", "time": "08:00"})
    with pytest.raises(S.ScheduleError) as e:
        apply(sc, {"action": "edit", "id": 999, "text": "x"})
    assert e.value.status == 404
    # persisted
    assert "new briefing" in rem.path.read_text()


# ---- pause / resume, and the runner skips paused ones
def test_pause_resume(sc, rem):
    r = rem.add(None, "briefing", "do", {"days": list(range(7)), "time": "08:00"})
    apply(sc, {"action": "pause", "id": r["id"]})
    assert rem.get(r["id"])["enabled"] is False
    assert not sc.state()["routines"][0]["enabled"]
    assert json.loads(rem.path.read_text())[0]["enabled"] is False
    apply(sc, {"action": "resume", "id": r["id"]})
    assert "enabled" not in rem.get(r["id"])
    one = rem.add(NOW + 60, "tea", "remind")
    with pytest.raises(S.ScheduleError):
        apply(sc, {"action": "pause", "id": one["id"]})


def test_runner_skips_a_paused_routine(rem):
    a = rem.add(None, "paused one", "do", {"days": list(range(7)), "time": "07:30"})
    b = rem.add(None, "live one", "do", {"days": list(range(7)), "time": "07:30"})
    rem.set_enabled(a["id"], False)
    for i in rem.items:
        i["due"] = ts(2026, 11, 2, 7, 30)
    fired = rem.fire_due(ts(2026, 11, 2, 7, 31))
    assert [f["text"] for f in fired] == ["live one"]
    # still listed, rolled forward to tomorrow, still paused
    pa = rem.get(a["id"])
    assert pa["due"] == ts(2026, 11, 3, 7, 30) and pa["enabled"] is False
    assert rem.get(b["id"])["due"] == ts(2026, 11, 3, 7, 30)


# ---- delete needs confirm
def test_delete_needs_confirm(sc, rem):
    r = rem.add(NOW + 60, "tea", "remind")
    with pytest.raises(S.ScheduleError) as e:
        apply(sc, {"action": "delete", "id": r["id"]})
    assert e.value.code == "confirm" and e.value.status == 409 and rem.get(r["id"])
    apply(sc, {"action": "delete", "id": r["id"], "confirm": True})
    assert rem.get(r["id"]) is None


# ---- loops
def test_heartbeat_and_sleep_review_go_through_live_config(sc, life):
    apply(sc, {"action": "heartbeat", "minutes": 45})
    apply(sc, {"action": "sleep_review", "on": False})
    assert sc.fake_live.calls == [("life.heartbeat_minutes", 45), ("life.sleep_review", False)]
    st = sc.state()
    assert st["loops"]["heartbeat"]["minutes"] == 45 and st["loops"]["sleep_review"]["on"] is False
    for m in (5, 120):
        apply(sc, {"action": "heartbeat", "minutes": m})


def test_heartbeat_through_the_real_config_file(tmp_path, rem):
    from walter import config as C
    from walter.live import Live
    path = tmp_path / "config.toml"
    path.write_text("[life]\nheartbeat_minutes = 30  # keep me\n")
    cfg = C.load(path)
    lv = Live(cfg, path)
    s = S.Schedule(rem, cfg, live=lambda: lv, timers=lambda: [])
    s.apply(S.parse({"action": "heartbeat", "minutes": 15}))
    s.apply(S.parse({"action": "sleep_review", "on": False}))
    assert cfg.life.heartbeat_minutes == 15 and cfg.life.sleep_review is False   # applied live
    text = path.read_text()
    assert "heartbeat_minutes = 15  # keep me" in text and "sleep_review = false" in text


def test_no_live_config_is_503(rem, life):
    s = S.Schedule(rem, SimpleNamespace(life=life), live=lambda: None, timers=lambda: [])
    with pytest.raises(S.ScheduleError) as e:
        s.apply(S.parse({"action": "heartbeat", "minutes": 10}))
    assert e.value.status == 503


# ---- system timers: read-only, parsed from systemctl
def test_read_timers_parses_systemctl():
    listing = json.dumps([{"next": 1794415779009965, "last": 1794415764002425, "unit": "walter-build.timer"},
                          {"next": 0, "last": 0, "unit": "bad name;rm.timer"}])
    show = ("Description=Walter: refresh the build view\n"
            "TimersMonotonic={ OnUnitActiveUSec=15s ; next_elapse=2h }\n"
            "TimersMonotonic={ OnBootUSec=40s ; next_elapse=40s }\n")

    def run(args):
        return listing if "list-timers" in args else show
    rows = S.read_timers(run)
    assert rows == [{"name": "walter-build", "description": "Walter: refresh the build view",
                     "every": "every 15s", "next": 1794415779.0, "last": 1794415764.0}]
    assert S.read_timers(lambda a: "garbage") == []


def test_timers_cannot_be_edited():
    for body in ({"action": "edit", "id": "walter-usage", "text": "x"},
                 {"action": "pause", "id": "walter-usage"}, {"action": "delete", "id": "walter-usage", "confirm": True},
                 {"action": "timer", "name": "walter-usage"}, {"action": "config", "key": "brain.model", "value": "x"}):
        with pytest.raises(S.ScheduleError):
            S.parse(body)


def test_rate_limit(rem):
    t = [0.0]
    s = S.Schedule(rem, None, timers=lambda: [], mono=lambda: t[0])
    assert all(s.rate_ok() for _ in range(S.RATE_N))
    assert not s.rate_ok()
    t[0] = S.RATE_S + 1
    assert s.rate_ok()


# ---- over HTTP: auth, body limits, the live store
def test_http_auth_and_actions(iso, tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    rem = Reminders(tmp_path / "reminders.json", conductor=None)
    rem.add(None, "briefing", "do", {"days": [0, 1, 2, 3, 4], "time": "08:00"})

    async def go():
        app, apphub, _, _ = make_app(iso, reminders=rem)
        async with TestClient(TestServer(app)) as c:
            assert (await c.get("/api/app/schedule", headers=PROXY)).status == 401
            r = await c.post("/api/app/schedule", json={"action": "delete", "id": 1, "confirm": True}, headers=PROXY)
            assert r.status == 401 and rem.get(1)
            code = apphub.store.start_pairing()["code"]
            token = (await (await c.post("/api/app/pair", json={"code": code, "name": "Phone"},
                                         headers=PROXY)).json())["token"]
            auth = {**PROXY, "Authorization": f"Bearer {token}"}
            j = await (await c.get("/api/app/schedule", headers=auth)).json()
            assert [x["text"] for x in j["routines"]] == ["briefing"] and isinstance(j["timers"], list)
            r = await c.post("/api/app/schedule", json={"action": "pause", "id": 1}, headers=auth)
            j = await r.json()
            assert r.status == 200 and j["ok"] and j["routines"][0]["enabled"] is False
            r = await c.post("/api/app/schedule", json={"action": "delete", "id": 1}, headers=auth)
            assert r.status == 409 and (await r.json())["code"] == "confirm"
            r = await c.post("/api/app/schedule", json={"action": "add", "type": "routine", "text": "x",
                                                        "time": "99:00", "days": "daily"}, headers=auth)
            assert r.status == 400
            r = await c.post("/api/app/schedule", data="nope", headers={**auth, "Content-Type": "application/json"})
            assert r.status == 400
            r = await c.post("/api/app/schedule", json={"action": "add", "type": "reminder", "text": "x" * 300_000,
                                                        "time": "08:00"}, headers=auth)
            assert r.status == 413
            r = await c.post("/api/app/schedule", json={"action": "heartbeat", "minutes": 30}, headers=auth)
            assert r.status == 503 or r.status == 200      # a MagicMock conductor has no real live config
            r = await c.post("/api/app/schedule", json={"action": "delete", "id": 1, "confirm": True}, headers=auth)
            assert r.status == 200 and rem.get(1) is None
    asyncio.run(go())
