"""Headless smoke test of the weather chips next to outdoor calendar events (run with the
browser venv's python: ~/.walter/browser-venv/bin/python tests/face/wxevents_smoke.py [shots_dir]).

Builds the events with walter.dashboard.set_events (keyword rule + "outdoor" override) and
a weather file with an hourly block, serves face/ over a throwaway local HTTP server (the
live kiosk is untouched), and checks the dashboard at 1920x1080 (landscape), 800x1280
(compact portrait) and the 1080x1920 big-screen board: chips only on outdoor events, the
right hour's temperature and rain, nothing where no forecast covers the time, and every
chip inside its row and on screen. Sample data is generic. Prints PASS/FAIL."""
import datetime as dt
import functools
import http.server
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from walter import dashboard as D  # noqa: E402

FACE = ROOT / "face"
SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None
FAKE_WS = """
class FakeWS { constructor() { window.__ws = this; setTimeout(() => this.onopen && this.onopen(), 10); }
  send(x) {} close() {} }
window.WebSocket = FakeWS;
"""


def sample(now):
    today = dt.date.fromtimestamp(now)
    tmr = today + dt.timedelta(days=1)
    d, m = today.isoformat(), tmr.isoformat()
    raw = [
        {"title": "Team stand-up", "start": d + "T15:30:00", "end": d + "T16:00:00", "calendar": "Work"},
        {"title": "Soccer practice", "start": d + "T16:00:00", "end": d + "T17:30:00", "calendar": "Kids"},
        {"title": "Dinner on the patio", "start": d + "T19:00:00", "end": d + "T20:30:00", "calendar": "Home", "outdoor": True},
        {"title": "Football playoffs", "start": d + "T19:30:00", "end": d + "T21:00:00", "calendar": "Kids", "outdoor": False},
        {"title": "County fair", "start": m, "end": m, "calendar": "Home", "all_day": True},
        {"title": "Dentist", "start": m + "T09:00:00", "end": m + "T10:00:00", "calendar": "Home"},
        {"title": "Track meet", "start": m + "T18:00:00", "end": m + "T20:00:00", "calendar": "Kids", "location": "High school"},
    ]
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "ev.json"
        D.set_events(raw, out=out)
        events = json.loads(out.read_text())
    events["updated"] = now
    # hourly forecast: today 00:00 to tomorrow 11:00 only (so tomorrow 6 PM has no forecast)
    hours = [dt.datetime.combine(today, dt.time()) + dt.timedelta(hours=i) for i in range(36)]
    temp = [40 + (h.hour if h.hour <= 15 else 30 - h.hour) for h in hours]   # 16:00 -> 54
    rain = [40 if h.hour == 16 and h.date() == today else 5 for h in hours]
    code = [61 if h.hour == 16 and h.date() == today else 1 for h in hours]
    weather = {"updated": now, "ok": True, "temp": 50, "feels": 47, "code": 2, "text": "Partly cloudy", "is_day": 1,
               "wind": 9, "gust": 12, "wind_dir": "S", "alerts": [],
               "today": {"date": d, "high": 55, "low": 36, "rain": 40, "code": 61, "text": "Light rain"},
               "tomorrow": {"date": m, "high": 61, "low": 40, "rain": 30, "code": 2, "text": "Partly cloudy"},
               "hourly": {"time": [h.strftime("%Y-%m-%dT%H:%M") for h in hours], "temp": temp, "rain": rain,
                          "code": code, "is_day": [1 if 7 <= h.hour < 18 else 0 for h in hours]}}
    lst = {"title": "The list", "enabled": True, "items": [
        {"id": 1, "text": "Call the plumber", "section": "tasks", "created": now - 900},
        {"id": 2, "text": "Milk", "section": "shopping", "created": now - 600}]}
    return {"dashboard-events.json": events, "dashboard-weather.json": weather, "dashboard-list.json": lst,
            "dashboard-away.json": {"items": []}}


def main():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(FACE)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    root = f"http://127.0.0.1:{srv.server_port}"
    fails = []

    def check(cond, what):
        print(("ok   " if cond else "FAIL ") + what)
        if not cond:
            fails.append(what)

    data = sample(time.time())
    ev = {e["title"]: e for e in data["dashboard-events.json"]["events"]}
    check(ev["Soccer practice"]["outdoor"] and ev["Track meet"]["outdoor"] and ev["County fair"]["outdoor"], "keyword rule tags outdoor events")
    check(not ev["Team stand-up"]["outdoor"] and not ev["Dentist"]["outdoor"], "indoor events untagged")
    check(ev["Dinner on the patio"]["outdoor"] and not ev["Football playoffs"]["outdoor"], "outdoor overrides win")

    with sync_playwright() as p:
        br = p.chromium.launch(executable_path="/usr/bin/chromium")
        for (vw, vh, disp, url, tag) in ((1920, 1080, {}, "/index.html?dash=1&at=14:00", "landscape"),
                                         (800, 1280, {}, "/index.html?dash=1&at=14:00", "portrait"),
                                         (1080, 1920, {"layout": "compact"}, "/index.html?dash=1&at=14:00", "compact"),
                                         (1080, 1920, {}, "/index.html?at=14:00", "big-board")):
            print(f"--- {tag} {vw}x{vh}")
            pg = br.new_page(viewport={"width": vw, "height": vh})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.add_init_script(FAKE_WS)
            pg.add_init_script("try { localStorage.clear(); } catch (e) {}")
            js = lambda body: (lambda r: r.fulfill(status=200, content_type="application/json", body=body))
            pg.route("**/display.json*", js(json.dumps(disp)))
            pg.route("**/card.json*", js('{"id": 0, "clear": true}'))
            for f in ("focus.json", "canvas.json", "browser.json", "background.json", "checklist.json", "usage.json"):
                pg.route(f"**/{f}*", js("{}"))
            for f, d in data.items():
                pg.route(f"**/{f}*", js(json.dumps(d)))
            pg.route("**/api/**", js("[]"))
            pg.route("**/events*", lambda r: r.fulfill(status=204, body=""))
            pg.goto(root + url)
            pg.wait_for_timeout(3500)
            rows = pg.evaluate("""Array.from(document.querySelectorAll('#dash .cal .ev')).map(r => {
                const c = r.querySelector('.wxe'), b = r.getBoundingClientRect(), cb = c && c.getBoundingClientRect(),
                      w = r.querySelector('.what').getBoundingClientRect();
                return { name: r.querySelector('.nm').textContent, chip: c ? c.textContent : null,
                         icon: !!(c && c.querySelector('svg')),
                         box: cb && { l: cb.left, r: cb.right, t: cb.top, b: cb.bottom, w: cb.width, h: cb.height },
                         row: { l: b.left, r: b.right, t: b.top, b: b.bottom }, what: { l: w.left, r: w.right },
                         nm_b: r.querySelector('.nm').getBoundingClientRect().bottom,
                         nm_cut: (n => n.scrollWidth > n.clientWidth + 1 && n.textContent.length < 24)(r.querySelector('.nm')) }; })""")
            got = {r["name"]: r for r in rows}
            if tag == "big-board":
                check(pg.evaluate("document.body.classList.contains('board-on')"), "big board is on")
            chips = {n: r["chip"] for n, r in got.items()}
            print("     chips:", chips)
            check(got.get("Soccer practice", {}).get("chip") == "54° 40%", "soccer: 4 PM hour, 54° and 40% rain")
            check(got.get("Dinner on the patio", {}).get("chip") == "51°", "override outdoor: 7 PM hour, no rain shown under 20%")
            check("Football playoffs" in got and got["Football playoffs"]["chip"] is None, "override indoor: no chip")
            check("Team stand-up" in got and got["Team stand-up"]["chip"] is None, "indoor event: no chip")
            if "County fair" in got:
                check(got["County fair"]["chip"] == "61° 30%", "all-day outdoor: tomorrow's high and rain")
            if "Track meet" in got:
                check(got["Track meet"]["chip"] is None, "no forecast for that hour: no chip")
            for n, r in got.items():
                if r["box"]:
                    b = r["box"]
                    check(r["icon"] and b["w"] > 10 and b["h"] > 8, f"{n}: chip has an icon and a size")
                    check(b["l"] >= r["what"]["l"] - 1 and b["r"] <= r["row"]["r"] + 1 and b["t"] >= r["nm_b"] - 1
                          and b["b"] <= r["row"]["b"] + 1, f"{n}: chip inside its row, under the title")
                    check(not r["nm_cut"], f"{n}: title not squeezed by the chip")
                    check(b["l"] >= 0 and b["r"] <= vw and b["b"] <= vh, f"{n}: chip on screen")
            check(not errors, f"no page errors {errors[:3]}")
            if SHOTS:
                SHOTS.mkdir(parents=True, exist_ok=True)
                pg.screenshot(path=str(SHOTS / f"{tag}-{vw}x{vh}.png"))
            pg.close()
        br.close()
    srv.shutdown()
    print("PASS" if not fails else f"FAIL ({len(fails)})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
