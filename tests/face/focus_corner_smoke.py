"""Headless smoke test of the face's focus corner (run with the browser venv's python:
~/.walter/browser-venv/bin/python tests/face/focus_corner_smoke.py [shots_dir]).

Serves face/ over a throwaway local HTTP server, fakes the brain's websocket, feeds a
focus box through a routed focus.json and checks: I shrink into the bottom-right corner,
big moves are dropped there, talking brings me back, I settle again, and the box ending
brings me home. The big centre ring: shown mid-screen with the title and time left, amber in the
last five minutes, dims while we talk or a card is up, hides (with the strip, no small ring) behind the canvas/browser panels,
fades out when the box ends. Also pokes every emotion and action tag for JS errors. Prints PASS/FAIL."""
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

focus = {"box": None, "strip": "auto"}


def main():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    handler = functools.partial(Quiet, directory=str(FACE))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/index.html"
    errors, fails = [], []

    def check(cond, what):
        print(("ok   " if cond else "FAIL ") + what)
        if not cond:
            fails.append(what)

    with sync_playwright() as p:
        b = p.chromium.launch(executable_path="/usr/bin/chromium")
        pg = b.new_page(viewport={"width": 1920, "height": 1080})
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.add_init_script(FAKE_WS)
        empty = lambda body: (lambda r: r.fulfill(status=200, content_type="application/json", body=body))
        pg.route("**/focus.json*", lambda r: r.fulfill(status=200, content_type="application/json",
                                                         body=json.dumps(focus)))
        card = ['{"id": 0, "clear": true}']
        pg.route("**/card.json*", lambda r: r.fulfill(status=200, content_type="application/json", body=card[0]))
        for f in ("canvas.json", "browser.json", "background.json", "checklist.json",
                  "dashboard-list.json", "dashboard-events.json", "usage.json"):
            pg.route(f"**/{f}*", empty("{}"))
        pg.route("**/api/**", empty("[]"))
        # plain landscape at the original text size, whatever the live face/display.json says
        pg.route("**/display.json*", empty('{"rotate": 0, "text_scale": 1}'))
        pg.goto(url)
        pg.wait_for_timeout(2500)
        S = lambda: pg.evaluate("({...window.WALTER_STATE.pos, q: window.WALTER_STATE.focusQuiet,"
                                " fm: window.WALTER_STATE.focusMix, acts: window.WALTER_STATE.actions.length})")
        push = lambda m: pg.evaluate("m => window.__push(m)", m)
        shot = lambda n: SHOTS and pg.screenshot(path=str(SHOTS / f"{n}.png"))
        RING = lambda: pg.evaluate("""(() => { const r = document.getElementById('focusring'), b = r.getBoundingClientRect(),
            t = r.querySelector('.title'), cs = getComputedStyle(r);
            return { cls: r.className, op: +cs.opacity, cx: b.left + b.width / 2, cy: b.top + b.height / 2, w: b.width,
                     title: t.textContent, fits: t.scrollHeight <= t.clientHeight + 1, fs: parseFloat(getComputedStyle(t).fontSize),
                     left: r.querySelector('.left').textContent,
                     off: +r.querySelector('.arc').getAttribute('stroke-dashoffset') }; })()""")

        normal = S()
        check(abs(normal["cx"] - 960) < 30 and abs(normal["cy"] - 540) < 30, f"normal: centred {normal}")
        shot("1-normal")

        t = time.time()
        focus["box"] = {"task": "Test task", "start": t - 60, "end": t + 600, "status": "running", "planned_min": 11}
        pg.wait_for_timeout(9000)
        small = S()
        check(small["q"] and small["fm"] > 0.97, f"box: quiet in the corner {small}")
        check(small["R"] < normal["R"] * 0.4, "box: really small")
        check(small["cx"] > 1920 * 0.85 and small["cy"] > 1080 * 0.75, "box: bottom-right corner")
        # The owner (5 Nov, 55e9ab4): sit tight to the bottom, rings may spill off-screen; my body stays on it
        check(small["cy"] + small["R"] < 1080, "box: body on screen at the bottom")
        shot("2-corner")
        rg = RING()
        check("on" in rg["cls"] and rg["op"] > 0.95, f"ring: shown {rg['cls']} {rg['op']}")
        check(abs(rg["cx"] - 960) < 20 and abs(rg["cy"] - 1080 * 0.47) < 20 and rg["w"] > 1080 * 0.6, f"ring: big in the middle {rg}")
        check(rg["title"] == "Test task" and "min left" in rg["left"], f"ring: title + time left {rg['title']!r} {rg['left']!r}")
        check(260 < rg["off"] < 280, f"ring: ~9% filled (dashoffset {rg['off']:.1f})")
        check(not pg.evaluate("window.WALTER_STATE.focusVis > 0.05"), "ring: no small ring round me")
        check(small["cx"] - small["R"] * 1.3 > rg["cx"] + rg["w"] / 2, "ring: I'm clear of it in the corner")
        focus["box"]["task"] = ("Get the drafts done and then also write the long summary for the network "
                                "application, every section of it, before five")
        pg.wait_for_timeout(3500)
        rg = RING()
        check(rg["fits"] and rg["fs"] < 1080 * 0.05, f"ring: long title shrinks to fit ({rg['fs']:.0f}px)")
        shot("2b-long-title")
        focus["box"]["task"] = "Test task"
        focus["box"]["end"] = time.time() + 200
        pg.wait_for_timeout(3500)
        check("warn" in RING()["cls"], "ring: amber in the last five minutes")
        shot("2c-amber")
        focus["box"]["end"] = time.time() + 600

        for a in ("bounce", "jump", "spin", "scoot:left", "grow", "peek", "laugh"):
            push({"t": "action", "name": a})
        push({"t": "emotion", "name": "excited"})
        check(S()["acts"] == 0, "box: big moves dropped in the corner")
        pg.wait_for_timeout(500)

        push({"t": "state", "state": "speaking"})
        pg.wait_for_timeout(3000)
        talk = S()
        check(not talk["q"] and normal["R"] * 0.45 < talk["R"] < normal["R"] * 0.65, f"talking: a little bigger {talk}")
        check(talk["cx"] > 1920 * 0.8, "talking: still in the corner, off the ring")
        check("dim" in RING()["cls"], "talking: ring dims")
        push({"t": "action", "name": "bounce"})
        check(S()["acts"] >= 1, "talking: moves allowed again")
        push({"t": "caption", "who": "walter", "text": "Sure, I will keep it short so you can get back to it."})
        shot("3-talking")
        push({"t": "state", "state": "idle"})
        pg.wait_for_timeout(3000)
        check(not S()["q"], "after talking: still a bit bigger for a few seconds")
        pg.wait_for_timeout(8000)
        check(S()["q"] and S()["R"] < normal["R"] * 0.45, "after talking: settled back small")
        pg.wait_for_timeout(1600)
        check("dim" not in RING()["cls"] and RING()["op"] > 0.95, "after talking: ring bright again")

        # an info card mid-box: the ring dims under it
        card[0] = '{"id": 7, "title": "Code", "body": "123 456"}'
        pg.wait_for_timeout(2600)
        check("dim" in RING()["cls"], "card up: ring dims")
        shot("3b-card")
        card[0] = '{"id": 8, "clear": true}'
        pg.wait_for_timeout(2600)
        check("dim" not in RING()["cls"], "card gone: ring bright")

        # every emotion and body tag, while small and while normal: no JS errors
        emos = pg.evaluate("Object.keys({neutral:1,happy:1,excited:1,sad:1,curious:1,thinking:1,surprised:1,"
                           "mischievous:1,wink:1,embarrassed:1,love:1,angry:1,sleepy:1})")
        for e in emos:
            push({"t": "emotion", "name": e, "hold": 1})
        for a in ("bounce", "hop", "pop", "perk", "wiggle", "shake", "nod", "jump", "spin", "shiver", "melt",
                  "puff", "grow", "shrink", "scoot:left", "scoot:right", "peek", "laugh", "look:left", "blink"):
            push({"t": "action", "name": a})
        for st in ("listening", "thinking", "working", "speaking", "idle"):
            push({"t": "state", "state": st})
            pg.wait_for_timeout(150)

        # canvas open mid-box: normal canvas layout (step left), not the corner
        pg.evaluate("window.walter && window.walter.S ? (window.walter.S.canvas = true) : (window.WALTER_STATE.canvas = true)")
        pg.wait_for_timeout(1500)
        check(not S()["q"], "canvas open: not in the corner")
        pg.wait_for_timeout(1500)
        check(RING()["op"] < 0.05, "canvas open: big ring hidden")
        check(not pg.evaluate("window.WALTER_STATE.focusVis > 0.05"), "canvas open: no small ring round me either")
        check(not pg.evaluate("document.getElementById('focusstrip').classList.contains('on')"), "canvas open: Now/Next strip hidden")
        pg.evaluate("(window.walter && window.walter.S || window.WALTER_STATE).canvas = false")
        pg.wait_for_timeout(9000)
        check(S()["q"], "canvas closed: back in the corner")
        check(RING()["op"] > 0.95, "canvas closed: big ring back")
        check(pg.evaluate("document.getElementById('focusstrip').classList.contains('on')"), "canvas closed: strip back")
        pg.evaluate("window.WALTER_STATE.browser = true; document.body.classList.add('browser-on')")
        pg.wait_for_timeout(3000)
        check(RING()["op"] < 0.05 and not S()["q"], "browser open: ring hidden, normal browser layout")
        check(not pg.evaluate("document.getElementById('focusstrip').classList.contains('on')") and not pg.evaluate("window.WALTER_STATE.focusVis > 0.05"), "browser open: strip + small ring hidden")
        pg.evaluate("window.WALTER_STATE.browser = false; document.body.classList.remove('browser-on')")
        pg.wait_for_timeout(9000)
        check(RING()["op"] > 0.95 and S()["q"], "browser closed: ring and corner back")
        check(pg.evaluate("document.getElementById('focusstrip').classList.contains('on')"), "browser closed: strip back")
        focus["box"]["status"] = "checkin"; focus["box"]["end"] = time.time() - 5
        pg.wait_for_timeout(3500)
        rg = RING()
        check("up" in rg["cls"] and rg["left"] == "Time's up" and rg["off"] < 1, f"time's up: full, pulsing {rg['cls']}")
        shot("3c-timesup")

        focus["box"] = None
        pg.wait_for_timeout(6500)
        end = S()
        check(not end["q"] and abs(end["cx"] - 960) < 40 and end["R"] > normal["R"] * 0.95, f"box over: home again {end}")
        shot("4-after")
        check(RING()["op"] < 0.05, "box over: big ring faded out")
        check(not errors, f"no page errors {errors[:3]}")
        b.close()
    srv.shutdown()
    print("PASS" if not fails else f"FAIL ({len(fails)})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
