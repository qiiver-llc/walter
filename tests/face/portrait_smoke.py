"""Headless portrait-layout smoke test of the face (run with the browser venv's python:
~/.walter/browser-venv/bin/python tests/face/portrait_smoke.py [shots_dir]).

Serves face/ over a throwaway local HTTP server with a fake brain websocket and canned
data files, then for 1080x1920 and 800x1280 (portrait) and 1920x1080 (landscape
regression) walks through: idle, speaking caption, info card, focus ring, canvas open,
browser open, setup QR, restart pill, dashboard (day + night). Checks that nothing
important overlaps (me vs. caption / panels / ring / setup QR; pills and cards inside the
screen) and that landscape keeps its old geometry. Screenshots go to shots_dir (named
<W>x<H>-<state>.png). Prints PASS/FAIL. Sample data is generic (no personal info)."""
import functools
import http.server
import json
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

FACE = Path(__file__).resolve().parents[2] / "face"
SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None

FAKE_WS = """
window.__msgs = [];
class FakeWS { constructor() { window.__ws = this; setTimeout(() => this.onopen && this.onopen(), 10); }
  send(x) { window.__msgs.push(x); } close() {} }
window.WebSocket = FakeWS;
window.__push = (m) => window.__ws && window.__ws.onmessage && window.__ws.onmessage({ data: JSON.stringify(m) });
"""

QR_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 21 21"><rect width="21" height="21" fill="#fff"/>'
          '<rect x="1" y="1" width="7" height="7"/><rect x="13" y="1" width="7" height="7"/>'
          '<rect x="1" y="13" width="7" height="7"/><rect x="10" y="10" width="3" height="3"/></svg>')

CANVAS_MD = ("# Weekend plan\n\n## Saturday\n- Farmers market, 9 AM\n- Fix the fence gate\n- Grill dinner\n\n"
             "## Sunday\n| Time | What |\n|---|---|\n| 10 AM | Church |\n| 2 PM | Football |\n| 6 PM | Pizza night |\n\n"
             "```python\nprint('hello from the canvas')\n```\n")


def sample_data(now):
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    tmr = time.strftime("%Y-%m-%d", time.localtime(now + 86400))
    return {
        "dashboard-list.json": {"title": "The list", "enabled": True, "items": [
            {"id": 1, "text": "Call the plumber about the kitchen sink", "section": "tasks", "created": now - 900},
            {"id": 2, "text": "Renew the car registration", "section": "tasks", "created": now - 800, "due": day + "T17:00"},
            {"id": 3, "text": "Book the dentist", "section": "tasks", "created": now - 700},
            {"id": 4, "text": "Milk", "section": "shopping", "created": now - 600},
            {"id": 5, "text": "Coffee beans", "section": "shopping", "created": now - 500},
            {"id": 6, "text": "Try a smaller speech model", "section": "ideas", "created": now - 400}]},
        "dashboard-events.json": {"events": [
            {"title": "Team stand-up", "start": day + "T15:30:00", "end": day + "T16:00:00", "calendar": "Work"},
            {"title": "Soccer practice", "start": day + "T17:30:00", "end": day + "T19:00:00", "calendar": "Kids"},
            {"title": "Dinner with friends", "start": day + "T19:30:00", "end": day + "T21:00:00", "calendar": "Home"},
            {"title": "Trash pickup", "start": tmr, "end": tmr, "calendar": "Home", "all_day": True},
            {"title": "Dentist", "start": tmr + "T09:00:00", "end": tmr + "T10:00:00", "calendar": "Home"}]},
        "dashboard-weather.json": {"updated": now, "ok": True, "temp": 47, "feels": 41, "code": 2, "text": "Partly cloudy",
                                   "is_day": 1, "wind": 12, "gust": 22, "wind_dir": "N",
                                   "today": {"date": day, "high": 54, "low": 37, "rain": 18, "code": 3, "text": "Cloudy"},
                                   "tomorrow": {"date": tmr, "high": 40, "low": 34, "rain": 0, "code": 45, "text": "Fog"},
                                   "alerts": []},
        "dashboard-sports.json": {"teams": []},   # sports row off unless a test sets it (sports_smoke)
        "dashboard-away.json": {"unseen": 2, "items": [
            {"t": now - 1800, "title": "Reminder", "body": "The tea is ready"},
            {"t": now - 600, "title": "Worker done", "body": "Compared three robot vacuums; notes are on the canvas"}]},
    }


def main():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(FACE)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}/index.html"
    fails = []

    def check(cond, what):
        print(("ok   " if cond else "FAIL ") + what)
        if not cond:
            fails.append(what)

    def overlap(a, b, slack=0):
        return not (a["r"] <= b["l"] + slack or b["r"] <= a["l"] + slack or a["b"] <= b["t"] + slack or b["b"] <= a["t"] + slack)

    with sync_playwright() as p:
        br = p.chromium.launch(executable_path="/usr/bin/chromium")
        tmp = br.new_page(viewport={"width": 1024, "height": 810})
        tmp.set_content("<body style='font:22px sans-serif;margin:40px;background:#f6f6f6'><h1>Example Domain</h1>"
                        "<p>This domain is for use in illustrative examples in documents.</p>"
                        "<p><a href='#'>More information...</a></p></body>")
        page_png = tmp.screenshot()
        tmp.close()
        for (vw, vh) in ((1080, 1920), (800, 1280), (1920, 1080)):
            portrait = vh > vw
            tag = f"{vw}x{vh}"
            print(f"--- {tag}")
            now = time.time()
            data = sample_data(now)
            st = {"focus": {"box": None, "strip": "auto"}, "card": {"id": 0, "clear": True},
                  "canvas": {"id": 0, "hide": True}, "browser": {"id": 0, "open": False}}
            pg = br.new_page(viewport={"width": vw, "height": vh})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.add_init_script(FAKE_WS)
            pg.add_init_script("try { localStorage.clear(); } catch (e) {}")
            js = lambda body: (lambda r: r.fulfill(status=200, content_type="application/json", body=body))
            # the compact portrait layout (1080x1920 would pick the big-screen one on "auto":
            # that has its own test, biglayout_smoke.py)
            pg.route("**/display.json*", js('{"layout": "compact"}'))
            pg.route("**/focus.json*", lambda r: js(json.dumps(st["focus"]))(r))
            pg.route("**/card.json*", lambda r: js(json.dumps(st["card"]))(r))
            pg.route("**/canvas.json*", lambda r: js(json.dumps(st["canvas"]))(r))
            pg.route("**/canvas-doc.json*", lambda r: js(json.dumps({"id": st["canvas"]["id"], "kind": "md",
                                                                       "content": CANVAS_MD}))(r))
            pg.route("**/browser.json*", lambda r: js(json.dumps(st["browser"]))(r))
            for f, d in data.items():
                pg.route(f"**/{f}*", (lambda body: lambda r: js(body)(r))(json.dumps(d)))
            for f in ("background.json", "checklist.json", "usage.json"):
                pg.route(f"**/{f}*", js("{}"))
            pg.route("**/api/**", js("[]"))
            pg.route("**/api/setup/qr.svg*", lambda r: r.fulfill(status=200, content_type="image/svg+xml", body=QR_SVG))
            pg.route("**/stream.mjpg*", lambda r: r.fulfill(status=200, content_type="image/png", body=page_png))
            pg.route("**/events*", lambda r: r.fulfill(status=204, body=""))
            pg.goto(base)
            pg.wait_for_timeout(2500)

            push = lambda m: pg.evaluate("m => window.__push(m)", m)
            shot = lambda n: SHOTS and pg.screenshot(path=str(SHOTS / f"{tag}-{n}.png"))
            me = lambda: pg.evaluate("""(() => { const p = window.WALTER_STATE.pos;
                return { l: p.cx - p.R * 1.05, r: p.cx + p.R * 1.05, t: p.cy - p.R * 1.05, b: p.cy + p.R * 1.12,
                         cx: p.cx, cy: p.cy, R: p.R }; })()""")
            box = lambda sel: pg.evaluate("""s => { const e = document.querySelector(s); if (!e) return null;
                const b = e.getBoundingClientRect(), cs = getComputedStyle(e);
                return { l: b.left, r: b.right, t: b.top, b: b.bottom, w: b.width, h: b.height,
                         op: +cs.opacity, vis: cs.visibility, fs: parseFloat(cs.fontSize) }; }""", sel)
            inside = lambda b: b["l"] >= -1 and b["t"] >= -1 and b["r"] <= vw + 1 and b["b"] <= vh + 1

            check(pg.evaluate("document.documentElement.classList.contains('portrait')") == portrait, "portrait class matches")

            # idle
            m = me()
            if portrait:
                check(abs(m["cx"] - vw / 2) < 5 and vh * 0.38 < m["cy"] < vh * 0.5, f"idle: centred, a bit above the middle {m}")
                check(m["R"] > vw * 0.2 and m["l"] > 0 and m["r"] < vw, "idle: big, fits the width")
            else:
                check(abs(m["cx"] - vw / 2) < 5 and abs(m["cy"] - vh / 2) < 5 and abs(m["R"] - vh * 0.26) < 2,
                      f"landscape idle: same as before {m}")
            clk = box("#clock")
            check(inside(clk) and not overlap(clk, m), "clock: on screen, clear of me")
            ts = pg.evaluate("window.WALTER_TEXT_SCALE ? window.WALTER_TEXT_SCALE() : 1")  # textscale.js
            check(abs(clk["fs"] - (min(vw, vh) * 0.045 if portrait else vh * 0.045) * ts) < 2, f"clock size {clk['fs']:.0f}px")
            shot("1-idle")

            # speaking with a caption
            push({"t": "config", "captions": True})
            push({"t": "state", "state": "speaking"})
            push({"t": "caption", "who": "walter",
                  "text": "Ooh, good question. It looks like rain after lunch, so maybe do the fence gate this morning."})
            pg.wait_for_timeout(900)
            cap, m = box("#caption"), me()
            check(cap["op"] > 0.9 and inside(cap), f"caption: shown on screen {cap}")
            check(not overlap(cap, m), "caption: clear of me")
            shot("2-speaking")
            push({"t": "state", "state": "idle"})

            # mic muted pill + listening pill fit
            push({"t": "mute", "on": True})
            pg.wait_for_timeout(400)
            check(inside(box("#micoff")), "mic-off pill inside the screen")
            push({"t": "mute", "on": False})

            # info card (QR-style)
            st["card"] = {"id": 1, "title": "Pair your phone", "body": "Code 482 913", "img": "/api/setup/qr.svg"}
            pg.wait_for_timeout(2600)
            cd = box("#card")
            check(cd["op"] > 0.9 and inside(cd), f"card: on screen {cd}")
            check(box("#card .img")["w"] >= min(vw, vh) * 0.3, "card: QR big enough to scan")
            shot("3-card")
            st["card"] = {"id": 2, "title": "Note", "body": "Remember to take the casserole out of the oven at 6:15 PM and let it rest for ten minutes."}
            pg.wait_for_timeout(2600)
            check(inside(box("#card")), "long card: inside the screen")
            shot("3b-card-long")
            st["card"] = {"id": 3, "clear": True}
            pg.wait_for_timeout(2600)

            # focus box: big ring + me in the corner
            t = time.time()
            st["focus"]["box"] = {"task": "Write the quarterly summary", "start": t - 300, "end": t + 1200,
                                  "status": "running", "planned_min": 25}
            pg.wait_for_timeout(9000)
            rg, m, strip = box("#focusring"), me(), box("#focusstrip")
            check(rg["op"] > 0.9 and rg["l"] >= -2 and rg["r"] <= vw + 2 and rg["t"] >= 0 and rg["b"] <= vh,
                  f"focus ring: fully on screen {rg}")
            check(not overlap(m, {**rg, "l": rg["l"] + rg["w"] * 0.15, "r": rg["r"] - rg["w"] * 0.15,
                                  "t": rg["t"] + rg["h"] * 0.15, "b": rg["b"] - rg["h"] * 0.15}), "focus: I'm clear of the ring")
            check(m["cx"] > vw * 0.7 and m["cy"] > vh * 0.7, "focus: me in the bottom-right corner")
            check(strip["op"] < 0.1 or not overlap(strip, {**rg, "l": rg["l"] + rg["w"] * 0.2, "r": rg["r"] - rg["w"] * 0.2,
                                                         "t": rg["t"] + rg["h"] * 0.1}), "focus: strip clear of the ring")
            check(pg.evaluate("(() => { const t = document.querySelector('#focusring .title');"
                              " return t.scrollHeight <= t.clientHeight + 1; })()"), "focus: title fits")
            shot("4-focus")
            st["focus"]["box"] = None
            pg.wait_for_timeout(6000)

            # canvas
            st["canvas"] = {"id": 11, "title": "Weekend plan"}
            pg.wait_for_timeout(3500)
            cv, m = box("#canvas"), me()
            check(cv["op"] > 0.9 and inside(cv), f"canvas: open on screen {cv}")
            check(not overlap(cv, m), f"canvas: I'm clear of it {m}")
            if portrait:
                check(cv["t"] > vh * 0.3 and cv["w"] > vw * 0.9, "canvas: lower part, full width")
            else:
                check(cv["l"] > vw * 0.45 and abs(cv["w"] - vw * 0.5) < 3, "landscape canvas: right half as before")
            push({"t": "state", "state": "speaking"})
            push({"t": "caption", "who": "walter", "text": "It's on screen. Saturday is the busy one."})
            pg.wait_for_timeout(900)
            cap = box("#caption")
            check(not overlap(cap, cv) and not overlap(cap, me()), f"canvas: caption clear of panel and me {cap}")
            shot("5-canvas")
            push({"t": "state", "state": "idle"})
            st["canvas"] = {"id": 12, "hide": True}
            pg.wait_for_timeout(3500)

            # live browser panel
            st["browser"] = {"id": 21, "open": True, "title": "Example Domain", "url": "https://example.com/",
                             "w": 1024, "h": 810, "port": srv.server_port, "token": "x", "alive": time.time() + 600}
            pg.wait_for_timeout(3500)
            bw, m = box("#browser"), me()
            check(bw["op"] > 0.9 and inside(bw), f"browser: open on screen {bw}")
            check(not overlap(bw, m), f"browser: I'm clear of it {m}")
            push({"t": "state", "state": "speaking"})
            push({"t": "caption", "who": "walter", "text": "Here's the page."})
            pg.wait_for_timeout(900)
            check(not overlap(box("#caption"), bw), "browser: caption clear of the panel")
            shot("6-browser")
            push({"t": "state", "state": "idle"})
            st["browser"] = {"id": 22, "open": False}
            pg.wait_for_timeout(3500)

            # restart pill
            pg.evaluate("window.walterLink(true); window.walterLink(false)")
            pg.wait_for_timeout(1200)
            check(inside(box("#restart")), "restart pill inside the screen")
            shot("7-restart")
            pg.evaluate("window.walterLink(true)")

            # first-time setup QR
            push({"t": "setup", "needed": True})
            pg.wait_for_timeout(3000)
            su, m = box("#setup"), me()
            check(inside(su), f"setup: on screen {su}")
            check(not overlap(su, m), f"setup: I'm clear of the QR {m}")
            check(box("#setup .qr")["w"] >= min(vw, vh) * 0.25, "setup: QR big enough")
            shot("8-setup")
            push({"t": "setup", "needed": False})
            pg.wait_for_timeout(1500)
            check(not errors, f"no page errors {errors[:3]}")
            pg.close()

            # dashboard: day and night
            for when, name in (("14:00", "9-dashboard"), ("22:30", "9b-dashboard-night")):
                pg = br.new_page(viewport={"width": vw, "height": vh})
                pg.add_init_script(FAKE_WS)
                pg.route("**/display.json*", lambda r: r.fulfill(status=200, content_type="application/json",
                                                                 body='{"layout": "compact"}'))
                for f, d in data.items():
                    pg.route(f"**/{f}*", (lambda body: lambda r: r.fulfill(status=200, content_type="application/json", body=body))(json.dumps(d)))
                pg.route("**/card.json*", lambda r: r.fulfill(status=200, content_type="application/json",
                                                              body='{"id": 0, "clear": true}'))
                for f in ("focus.json", "canvas.json", "browser.json", "background.json", "checklist.json", "usage.json"):
                    pg.route(f"**/{f}*", lambda r: r.fulfill(status=200, content_type="application/json", body="{}"))
                pg.route("**/api/**", lambda r: r.fulfill(status=200, content_type="application/json", body="[]"))
                pg.goto(base + f"?dash=1&at={when}")
                pg.wait_for_timeout(3000)
                if name == "9-dashboard":
                    cards = pg.evaluate("""Array.from(document.querySelectorAll('#dash .dayv > *')).filter(e =>
                        getComputedStyle(e).display !== 'none').map(e => { const b = e.getBoundingClientRect();
                        return { c: e.className, l: b.left, r: b.right, t: b.top, b: b.bottom }; })""")
                    p_ = pg.evaluate("window.WALTER_STATE.pos")
                    mine = {"l": p_["cx"] - p_["R"], "r": p_["cx"] + p_["R"], "t": p_["cy"] - p_["R"], "b": p_["cy"] + p_["R"]}
                    for i, a in enumerate(cards):
                        check(inside(a), f"dashboard: {a['c']} on screen")
                        for b_ in cards[i + 1:]:
                            check(not overlap(a, b_, 2), f"dashboard: {a['c']} vs {b_['c']} no overlap")
                        check(not overlap(a, mine), f"dashboard: {a['c']} clear of me")
                SHOTS and pg.screenshot(path=str(SHOTS / f"{tag}-{name}.png"))
                pg.close()
        br.close()
    srv.shutdown()
    print("PASS" if not fails else f"FAIL ({len(fails)})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
