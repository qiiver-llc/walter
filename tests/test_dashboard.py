"""Dashboard mode: the list store, shorthand, due dates, events, the private-IP gate and
the /api/dashboard route. Never touches ~/.walter or the real face files."""
import asyncio
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from walter import dashboard as D

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """Point every default path at tmp_path."""
    monkeypatch.setenv("WALTER_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(D, "FACE_DIR", tmp_path / "face")
    return tmp_path


def store(tmp_path):
    return D.ListStore(tmp_path / "data" / "dashboard.json", tmp_path / "face" / "dashboard-list.json", "Our list")


# ---------------------------------------------------------------- list store
def test_add_done_remove_and_face_mirror(tmp_path):
    st = store(tmp_path)
    a = st.add("buy milk", "shopping")
    b = st.add("  call   the\nplumber ", "tasks", "2026-11-06")
    assert b["text"] == "call the plumber" and b["due"] == "2026-11-06"
    assert [i["id"] for i in st.items()] == [a["id"], b["id"]]
    st.done(2)
    assert st.items()[1]["done_at"]
    st.done(2, on=False)
    assert "done_at" not in st.items()[1]
    st.remove({"id": a["id"]})
    assert [i["text"] for i in st.items()] == ["call the plumber"]
    face = json.loads((tmp_path / "face" / "dashboard-list.json").read_text())
    assert face["title"] == "Our list" and len(face["items"]) == 1
    assert os.stat(tmp_path / "face" / "dashboard-list.json").st_mtime == D.FIXED_MTIME
    assert oct(os.stat(tmp_path / "data" / "dashboard.json").st_mode & 0o777) == "0o600"


def test_ids_never_reused_after_remove(tmp_path):
    st = store(tmp_path)
    st.add("one"); two = st.add("two")
    st.remove(2)
    assert st.add("three")["id"] == two["id"] + 1


def test_validation(tmp_path):
    st = store(tmp_path)
    with pytest.raises(ValueError):
        st.add("   ")
    with pytest.raises(ValueError):
        st.add("x", "garage")
    with pytest.raises(ValueError):
        st.add("x", due="next blue moon")
    with pytest.raises(KeyError):
        st.done(5)
    with pytest.raises(KeyError):
        st.remove({"id": 99})
    long = st.add("a" * 1000)
    assert len(long["text"]) == D.MAX_TEXT
    assert st.add("bell\x07 and\ttab")["text"] == "bell and tab"


def test_list_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "MAX_ITEMS", 3)
    st = store(tmp_path)
    for i in range(3):
        st.add(f"item {i}")
    with pytest.raises(ValueError):
        st.add("one too many")


def test_old_done_items_are_purged(tmp_path):
    st = store(tmp_path)
    st.add("old"); st.add("keep")
    data = json.loads(st.path.read_text())
    data["items"][0]["done_at"] = 1.0  # ticked off in 1970
    st.path.write_text(json.dumps(data))
    assert [i["text"] for i in st.items()] == ["keep"]


def test_section_aliases(tmp_path):
    st = store(tmp_path)
    assert st.add("eggs", "groceries")["section"] == "shopping"
    assert st.add("idea", "note")["section"] == "notes"


def test_publish_keeps_qr_and_hours(tmp_path):
    st = store(tmp_path)
    st.publish(extra={"qr": "data:x", "quiet": [22, 8], "dark": [23, 5], "url": "http://u"})
    st.add("thing")
    face = json.loads(st.face_out.read_text())
    assert face["qr"] == "data:x" and face["quiet"] == [22, 8] and face["items"][0]["text"] == "thing"


# ---------------------------------------------------------------- shorthand + dates
@pytest.mark.parametrize("text,want", [
    ("todo buy milk", ("tasks", "buy milk")),
    ("Shopping: eggs", ("shopping", "eggs")),
    ("note - gate code changed", ("notes", "gate code changed")),
    ("add: call the vet", ("tasks", "call the vet")),
    ("groceries bread", ("shopping", "bread")),
    ("water the plants", ("tasks", "water the plants")),
    ("notebook for school", ("tasks", "notebook for school")),
])
def test_parse_quick(text, want):
    assert D.parse_quick(text) == want


WED = dt.date(2026, 11, 4)


@pytest.mark.parametrize("s,want", [
    ("today", "2026-11-04"), ("tomorrow", "2026-11-05"), ("friday", "2026-11-06"),
    ("wed", "2026-11-11"), ("2026-12-01", "2026-12-01"), ("11/20", "2026-11-20"),
    ("1/3", "2027-01-03"), ("friday 5pm", "2026-11-06T17:00"), ("tomorrow at 9:30 am", "2026-11-05T09:30"),
    ("17:30", "2026-11-04T17:30"), ("12am", "2026-11-04T00:00"), ("2026-11-07T08:15", "2026-11-07T08:15"),
])
def test_parse_due(s, want):
    assert D.parse_due(s, WED) == want


@pytest.mark.parametrize("s", ["wedding", "someday", "25:00", "friday 13pm"])
def test_parse_due_rejects(s):
    with pytest.raises(ValueError):
        D.parse_due(s, WED)


# ---------------------------------------------------------------- private IPs
@pytest.mark.parametrize("ip,ok", [
    ("127.0.0.1", True), ("192.168.1.20", True), ("10.4.5.6", True), ("172.16.0.9", True),
    ("172.31.255.1", True), ("169.254.3.3", True), ("::1", True), ("fe80::1%wlan0", True),
    ("fd12:3456::1", True), ("::ffff:192.168.0.5", True),
    ("172.32.0.1", False), ("8.8.8.8", False), ("100.64.0.1", False), ("2001:4860::8888", False),
    ("::ffff:8.8.8.8", False), ("", False), (None, False), ("not-an-ip", False),
])
def test_is_private_ip(ip, ok):
    assert D.is_private_ip(ip) is ok


def test_lan_gate():
    from walter.server import lan_gate_ok
    assert lan_gate_ok("/api/tasks", "127.0.0.1")
    assert lan_gate_ok("/dashboard/add", "192.168.1.30")
    assert lan_gate_ok("/api/dashboard", "10.0.0.2")
    assert not lan_gate_ok("/api/tasks", "192.168.1.30")    # the rest of the API stays local
    assert not lan_gate_ok("/ws", "192.168.1.30")
    assert not lan_gate_ok("/", "192.168.1.30")
    assert not lan_gate_ok("/dashboard/add", "8.8.8.8")


def test_bind_host():
    from walter.config import Config
    from walter.server import bind_host
    c = Config()
    assert bind_host(c) == "0.0.0.0"           # loopback + phone page: listen on the LAN (gated)
    c.dashboard.phone_page = False
    assert bind_host(c) == "0.0.0.0"           # the companion app still needs the LAN (gated)
    c.app.enabled = False
    assert bind_host(c) == "127.0.0.1"
    c.app.enabled = True
    c.dashboard.phone_page = True
    c.server.host = "192.168.1.5"
    assert bind_host(c) == "192.168.1.5"


def test_check_request():
    from walter.server import dash_check_request
    j = {"Content-Type": "application/json"}
    assert dash_check_request(j, "h:8765", 50) == (0, "")
    assert dash_check_request({**j, "Origin": "http://h:8765"}, "h:8765", 50)[0] == 0
    assert dash_check_request({**j, "Origin": "http://evil.example"}, "h:8765", 50)[0] == 403
    assert dash_check_request({**j, "Origin": "null"}, "h:8765", 50)[0] == 403
    assert dash_check_request({"Content-Type": "application/x-www-form-urlencoded"}, "h:8765", 50)[0] == 415
    assert dash_check_request(j, "h:8765", 10_000)[0] == 413


# ---------------------------------------------------------------- events
def test_events_validated_and_sorted(iso):
    ev = D.set_events({"events": [
        {"title": "Late", "start": "2026-11-04T19:00:00-06:00", "calendar": "Main"},
        {"summary": "Trash day", "start": "2026-11-04"},
        {"title": "Early", "start": "2026-11-04T08:00:00Z", "end": "2026-11-04T09:00:00Z", "location": "x" * 99},
    ]})
    assert [e["title"] for e in ev] == ["Trash day", "Early", "Late"]
    assert ev[0]["all_day"] and len(ev[1]["location"]) == 60
    out = json.loads((iso / "face" / "dashboard-events.json").read_text())
    assert out["events"] == ev and out["updated"]


@pytest.mark.parametrize("bad", [
    "nope", [{"start": "2026-11-04"}], [{"title": "x", "start": "soon"}], [{"title": "x", "start": "2026-11-04", "end": "later"}], [1],
])
def test_events_rejected(iso, bad):
    with pytest.raises(ValueError):
        D.validate_events(bad)


# ---------------------------------------------------------------- outdoor events (weather chips)
@pytest.mark.parametrize("title,location,want", [
    ("Soccer practice", "", True), ("JV Football @ Westfield", "", True), ("Playoffs", "", True),
    ("Lunch", "City park", True), ("Horse show at the farm", "", True), ("Farmers market", "", True),
    ("Games night", "", True), ("Dentist", "", False), ("Run payroll", "", False),
    ("Parking garage meeting", "", False), ("Marketing review", "", False), ("Unfair trade call", "", False),
])
def test_outdoor_keyword_rule(title, location, want):
    assert D.is_outdoor({"title": title, "location": location}) is want


def test_outdoor_override_and_custom_keywords(iso):
    ev = D.set_events([
        {"title": "Dinner on the patio", "start": "2026-11-04T18:00:00", "outdoor": True},
        {"title": "Football playoffs (watch at home)", "start": "2026-11-04T19:00:00", "outdoor": False},
        {"title": "Soccer", "start": "2026-11-04T17:00:00", "outdoor": "yes"},   # not a bool: rule decides
        {"title": "Dentist", "start": "2026-11-04T09:00:00"},
    ])
    got = {e["title"]: e["outdoor"] for e in ev}
    assert got == {"Dinner on the patio": True, "Football playoffs (watch at home)": False, "Soccer": True, "Dentist": False}
    assert D.is_outdoor({"title": "Dentist"}, ["dentist"]) and not D.is_outdoor({"title": "Soccer"}, [])
    # a keyword edit reaches the stored events at the next refresh; overrides still win
    assert D.retag_events(["dentist"]) == 2
    out = json.loads((iso / "face" / "dashboard-events.json").read_text())["events"]
    assert {e["title"]: e["outdoor"] for e in out}["Dentist"] is True
    assert {e["title"]: e["outdoor"] for e in out}["Football playoffs (watch at home)"] is False
    assert D.retag_events(["dentist"], path=iso / "missing.json") == 0


def test_default_keywords_come_from_config():
    from walter.config import DEFAULT_OUTDOOR_KEYWORDS, DashboardCfg
    assert DashboardCfg().outdoor_keywords == DEFAULT_OUTDOOR_KEYWORDS
    assert "football" in DEFAULT_OUTDOOR_KEYWORDS and "rodeo" in DEFAULT_OUTDOOR_KEYWORDS


def test_parse_hourly():
    h = D.parse_forecast({"hourly": {"time": ["2026-11-04T00:00", "2026-11-04T01:00"], "temperature_2m": [40.1, 39.5],
                                     "precipitation_probability": [10, 35], "weather_code": [3, 61], "is_day": [0, 0]}})["hourly"]
    assert h == {"time": ["2026-11-04T00:00", "2026-11-04T01:00"], "temp": [40.1, 39.5], "rain": [10, 35],
                 "code": [3, 61], "is_day": [0, 0]}
    assert D.parse_forecast({})["hourly"] == {}
    assert D.parse_hourly({"time": ["2026-11-04T00:00"]})["rain"] == [None]


def test_parse_days_for_the_month_calendar():
    daily = {"time": [f"2026-11-{d:02d}" for d in range(4, 12)], "weather_code": [3, 61, 0, 2, 71, 45, 95, 1],
             "temperature_2m_max": [50, 44, 47, 52, 33, 40, 61, 58], "temperature_2m_min": [39, 35, 30, 31, 22, 28, 45, 40],
             "precipitation_probability_max": [5, 60, 0, 10, 80, 20, 90, 0]}
    days = D.parse_forecast({"daily": daily})["days"]
    assert len(days) == 8 and days[0] == {"date": "2026-11-04", "high": 50, "low": 39, "code": 3, "rain": 5}
    assert days[-1]["date"] == "2026-11-11" and days[6]["rain"] == 90
    many = {"time": [(dt.date(2026, 11, 1) + dt.timedelta(days=i)).isoformat() for i in range(20)]}
    assert len(D.parse_days(many)) == 16
    short = D.parse_days({"time": ["2026-11-04", "2026-11-05"], "temperature_2m_max": [50]})
    assert short[1] == {"date": "2026-11-05", "high": None, "low": None, "code": None, "rain": None}
    assert D.parse_forecast({})["days"] == [] and D.parse_days({"time": None}) == []


def test_hourly_kept_to_48_hours():
    t = [f"2026-11-{4 + i // 24:02d}T{i % 24:02d}:00" for i in range(7 * 24)]
    h = D.parse_hourly({"time": t, "temperature_2m": list(range(7 * 24))})
    assert len(h["time"]) == 48 and h["temp"][-1] == 47 and len(h["rain"]) == 48


def test_fetch_asks_for_7_days(monkeypatch):
    urls = []
    monkeypatch.setattr(D, "_get_json", lambda url, *a, **k: urls.append(url) or {})
    D.fetch_weather(47.61, -122.33)
    assert "forecast_days=16" in urls[0] and len(urls) == 2  # forecast + alerts, no extra calls


# ---------------------------------------------------------------- weather parsing (no network)
def test_parse_forecast_and_alerts():
    f = {"current": {"temperature_2m": 49.2, "apparent_temperature": 43, "weather_code": 61, "is_day": 1,
                     "wind_speed_10m": 11.4, "wind_gusts_10m": 22, "wind_direction_10m": 315},
         "daily": {"time": ["2026-11-04", "2026-11-05"], "weather_code": [3, 71], "temperature_2m_max": [50, 44],
                   "temperature_2m_min": [39, 35], "precipitation_probability_max": [5, 60],
                   "sunrise": ["a", "b"], "sunset": ["c", "d"]}}
    w = D.parse_forecast(f)
    assert w["text"] == "Light rain" and w["wind_dir"] == "NW" and w["tomorrow"]["text"] == "Light snow"
    a = D.parse_alerts({"features": [
        {"properties": {"event": "Wind Advisory", "severity": "Moderate", "headline": "h", "ends": "2026-11-04T21:00:00-06:00"}},
        {"properties": {"event": "Test", "status": "Test"}}]})
    assert [x["event"] for x in a] == ["Wind Advisory"]


# ---------------------------------------------------------------- the route
def test_api_route(iso):
    from aiohttp.test_utils import TestClient, TestServer
    from walter.config import Config
    from walter.server import Hub, build_app

    async def go():
        cfg = Config()
        cfg.dashboard.list_title = "Our list"
        hub = Hub()
        app = build_app(cfg, hub, MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock(), MagicMock())
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/dashboard/add")
            assert r.status == 200 and "Content-Security-Policy" in r.headers
            assert "<form" in await r.text()
            r = await c.post("/api/dashboard", json={"action": "add", "text": "shopping: eggs", "quick": True})
            j = await r.json()
            assert r.status == 200 and j["item"]["section"] == "shopping" and j["item"]["text"] == "eggs"
            r = await c.post("/api/dashboard", json={"action": "add", "text": "pay bill", "section": "tasks", "due": "tomorrow"})
            iid = (await r.json())["item"]["id"]
            r = await c.post("/api/dashboard", json={"action": "done", "id": iid})
            assert (await r.json())["item"]["done_at"]
            r = await c.post("/api/dashboard", json={"action": "remove", "id": iid})
            assert r.status == 200
            for bad in ({"action": "add", "text": ""}, {"action": "add", "text": 5}, {"action": "done", "id": "1"},
                        {"action": "done", "id": True}, {"action": "explode"}, {"action": "remove", "id": 999},
                        {"action": "add", "text": "x", "section": "garage"}, [1, 2]):
                r = await c.post("/api/dashboard", json=bad)
                assert r.status == 400, bad
            r = await c.post("/api/dashboard", data="action=add&text=x",
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
            assert r.status == 415
            r = await c.post("/api/dashboard", data=json.dumps({"action": "add", "text": "x" * 5000}),
                             headers={"Content-Type": "application/json"})
            assert r.status == 413
            r = await c.post("/api/dashboard", json={"action": "add", "text": "x"}, headers={"Origin": "http://evil.example"})
            assert r.status == 403
            r = await c.get("/api/dashboard")
            j = await r.json()
            assert j["title"] == "Our list" and [i["text"] for i in j["items"]] == ["eggs"]
    asyncio.run(go())
    assert json.loads((iso / "data" / "dashboard.json").read_text())["items"][0]["text"] == "eggs"


def test_route_refuses_public_peers(iso, monkeypatch):
    """A request that (somehow) arrives from a public address gets nothing."""
    from walter import server
    monkeypatch.setattr(server.dash, "is_private_ip", lambda ip: False)
    from aiohttp.test_utils import TestClient, TestServer
    from walter.config import Config

    async def go():
        app = server.build_app(Config(), server.Hub(), *[MagicMock()] * 6)
        async with TestClient(TestServer(app)) as c:
            assert (await c.get("/dashboard/add")).status == 403
            assert (await c.post("/api/dashboard", json={"action": "add", "text": "x"})).status == 403
    asyncio.run(go())


# ---------------------------------------------------------------- CLI
def test_cli(tmp_path):
    env = {**os.environ, "WALTER_DATA": str(tmp_path)}
    # keep the real face file untouched: run a copy of the CLI against a copied face dir
    run = lambda *a: subprocess.run([sys.executable, "-c",
        "import sys, runpy; from pathlib import Path; import walter.dashboard as D;"
        f"D.FACE_DIR = Path({str(tmp_path / 'face')!r}); sys.argv = ['walter-dashboard', *sys.argv[1:]];"
        f"runpy.run_path({str(REPO / 'bin' / 'walter-dashboard')!r}, run_name='__main__')", *a],
        cwd=REPO, env=env, capture_output=True, text=True)
    assert run("add", "shopping:", "milk").returncode == 0
    assert run("add", "call the vet", "--due", "tomorrow 10am").returncode == 0
    out = run("list").stdout
    assert "1. [ ] shopping milk" in out and "call the vet" in out
    assert run("done", "2").returncode == 0
    assert "[x]" in run("list").stdout
    assert run("remove", "9").returncode == 1
    assert run("add", "x", "--due", "whenever").returncode == 1
    ev = tmp_path / "ev.json"
    ev.write_text(json.dumps([{"title": "Thing", "start": "2026-11-04T10:00:00-06:00"}]))
    assert "events: 1" in run("set-events", str(ev)).stdout
    ev.write_text(json.dumps([{"title": "Soccer practice", "start": "2026-11-04T16:00:00-06:00"}]))
    assert "1 outdoor" in run("set-events", str(ev)).stdout
    assert (tmp_path / "face" / "dashboard-events.json").exists()
    assert not (REPO / "face" / "dashboard-list.json").exists() or "call the vet" not in (REPO / "face" / "dashboard-list.json").read_text()


def test_month_window():
    """Sunday on/before the 1st to the day after the Saturday on/after the last day."""
    assert D.month_window(dt.date(2026, 11, 6)) == (dt.date(2026, 11, 1), dt.date(2026, 12, 6))      # 5 rows
    assert D.month_window(dt.date(2026, 2, 14)) == (dt.date(2026, 2, 1), dt.date(2026, 3, 1))        # 4 rows
    assert D.month_window(dt.date(2026, 8, 31)) == (dt.date(2026, 7, 26), dt.date(2026, 9, 6))       # 6 rows
    assert D.month_window(dt.date(2026, 10, 1)) == (dt.date(2026, 9, 27), dt.date(2026, 11, 1))
    for m in range(1, 13):
        s, e = D.month_window(dt.date(2027, m, 15))
        assert s.weekday() == 6 and e.weekday() == 6 and (e - s).days in (28, 35, 42)


def test_month_of_events_kept(iso):
    """set-events keeps a whole month grid from several calendars (the portrait month calendar)."""
    raw = []
    for d in range(35):
        day = (dt.date(2026, 11, 1) + dt.timedelta(days=d)).isoformat()
        raw.append({"title": f"Day {d} all-day", "start": day, "end": day, "calendar": "A"})
        for h in (8, 12, 15, 18):
            raw.append({"title": f"Day {d} at {h}", "start": f"{day}T{h:02d}:00:00-06:00", "calendar": "B"})
    ev = D.set_events(raw)
    assert len(ev) == 35 * 5
    assert D.event_days(ev) == 35 and D.event_days([]) == 0
    out = json.loads((iso / "face" / "dashboard-events.json").read_text())["events"]
    assert out[-1]["title"] == "Day 34 at 18" and out[0]["title"] == "Day 0 all-day"
    assert len(D.validate_events([{"title": "x", "start": "2026-11-06"}] * 1000)) == D.MAX_EVENTS


def test_publish_keeps_calendar_colours_and_verse_flag(tmp_path):
    """cal_colors / verse come from walter-dashboard's face_extra and survive list edits."""
    import json
    from walter import dashboard as D
    st = D.ListStore(path=tmp_path / "list.json", face_out=tmp_path / "face.json")
    st.publish(extra={"cal_colors": {"Family": "#123456"}, "verse": True})
    st.add("milk")
    out = json.loads((tmp_path / "face.json").read_text())
    assert out["cal_colors"] == {"Family": "#123456"} and out["verse"] is True
