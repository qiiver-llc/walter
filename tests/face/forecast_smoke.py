"""Headless smoke test of the portrait 7-day forecast strip (dashboard.js .fc). Run with the
browser venv's python:
~/.walter/browser-venv/bin/python tests/face/forecast_smoke.py [shots_dir]

Generic canned data (no personal info), fake brain websocket, face/ served locally. Checks:
  1080x1920 awake (big board): 7 cells in a full-width row ABOVE the "me" row (.sys, at the very
    bottom) and under the calendar/list, rain only when >= 20 %, clear of me, the board cards, the
    caption, the restart pill, a pairing card (QR) and the focus ring; hidden with the canvas and
    the browser open; still there (dimmed) in quiet hours
  1080x1920 asleep: day (with and without the list QR) shows it above the footer (QR + hint + me
    row at the very bottom), I sit beside the footer; quiet hours: under the night clock, above
    the me row; dark hours hide it
  1920x1080 rotate.html turned 90: same strip inside the turned frame
  1920x1080 landscape: never shown, my sleeping corner unchanged
Prints PASS/FAIL."""
import datetime
import functools
import http.server
import json
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from portrait_smoke import CANVAS_MD, FAKE_WS, QR_SVG, sample_data  # noqa: E402

FACE = Path(__file__).resolve().parents[2] / "face"
SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None
RAIN = [10, 20, 65, 0, 19, 40, 5]
CODES = [2, 61, 63, 0, 3, 71, 45]


def weather(now):
    w = sample_data(now)["dashboard-weather.json"]
    d0 = datetime.date.today()
    w["days"] = [{"date": (d0 + datetime.timedelta(days=i)).isoformat(), "high": 50 + i * 3, "low": 30 + i,
                  "code": CODES[i], "rain": RAIN[i]} for i in range(7)]
    return w


def above(a, b, slack=1):
    return a["b"] <= b["t"] + slack


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

    def overlap(a, b, slack=0):
        return not (a["r"] <= b["l"] + slack or b["r"] <= a["l"] + slack or a["b"] <= b["t"] + slack or b["b"] <= a["t"] + slack)

    def open_page(br, vw, vh, url, display, st, qr=False):
        now = time.time()
        data = sample_data(now)
        if qr:
            data["dashboard-list.json"]["qr"] = "/api/setup/qr.svg"
        data["dashboard-weather.json"] = weather(now)
        data["dashboard-events.json"]["updated"] = now
        data["usage.json"] = {"ok": True, "updated": now, "session": {"pct": 34}, "week": {"pct": 72}}
        pg = br.new_page(viewport={"width": vw, "height": vh})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.add_init_script(FAKE_WS)
        pg.add_init_script("try { localStorage.clear(); } catch (e) {}")
        js = lambda body: (lambda r: r.fulfill(status=200, content_type="application/json", body=body))
        pg.route("**/display.json*", lambda r: js(json.dumps(display))(r))
        pg.route("**/focus.json*", lambda r: js(json.dumps(st["focus"]))(r))
        pg.route("**/card.json*", lambda r: js(json.dumps(st["card"]))(r))
        pg.route("**/canvas.json*", lambda r: js(json.dumps(st["canvas"]))(r))
        pg.route("**/canvas-doc.json*", lambda r: js(json.dumps({"id": st["canvas"]["id"], "kind": "md", "content": CANVAS_MD}))(r))
        pg.route("**/browser.json*", lambda r: js(json.dumps(st["browser"]))(r))
        for f, d in data.items():
            pg.route(f"**/{f}*", (lambda body: lambda r: js(body)(r))(json.dumps(d)))
        pg.route("**/background.json*", js("{}"))
        pg.route("**/api/**", js("[]"))
        pg.route("**/api/setup/qr.svg*", lambda r: r.fulfill(status=200, content_type="image/svg+xml", body=QR_SVG))
        pg.route("**/stream.mjpg*", lambda r: r.fulfill(status=200, content_type="image/png", body=st["png"]))
        pg.route("**/events*", lambda r: r.fulfill(status=204, body=""))
        pg.goto(root + url)
        pg.wait_for_timeout(3500)
        return pg, errors

    def helpers(pg, frame):
        box = lambda sel: frame.evaluate("""s => { const e = document.querySelector(s); if (!e) return null;
            const b = e.getBoundingClientRect(), cs = getComputedStyle(e);
            return { l: b.left, r: b.right, t: b.top, b: b.bottom, w: b.width, h: b.height,
                     op: +cs.opacity, vis: cs.visibility, disp: cs.display, fs: parseFloat(cs.fontSize) }; }""", sel)
        me = lambda: frame.evaluate("""(() => { const p = window.WALTER_STATE.pos;
            return { l: p.cx - p.R * 1.1, r: p.cx + p.R * 1.1, t: p.cy - p.R * 1.25, b: p.cy + p.R * 1.15,
                     cx: p.cx, cy: p.cy, R: p.R }; })()""")
        fc_on = lambda: frame.evaluate("document.body.classList.contains('fc-on')")
        push = lambda m: frame.evaluate("m => window.__push(m)", m)
        return box, me, fc_on, push

    with sync_playwright() as p:
        br = p.chromium.launch(executable_path="/usr/bin/chromium")
        tmp = br.new_page(viewport={"width": 1024, "height": 810})
        tmp.set_content("<body style='font:22px sans-serif;margin:40px;background:#f6f6f6'><h1>Example Domain</h1></body>")
        page_png = tmp.screenshot()
        tmp.close()
        fresh = lambda: {"focus": {"box": None}, "card": {"id": 0, "clear": True}, "canvas": {"id": 0, "hide": True},
                         "browser": {"id": 0, "open": False}, "png": page_png}

        # ---- landscape: never
        st = fresh()
        pg, errors = open_page(br, 1920, 1080, "/index.html?at=14:00", {}, st)
        box, me, fc_on, push = helpers(pg, pg.main_frame)
        check(not fc_on() and box("#dash .fc")["disp"] == "none", "landscape awake: no strip")
        push({"t": "state", "state": "sleeping"})
        pg.wait_for_timeout(4500)
        m = me()
        check(not fc_on() and box("#dash .fc")["disp"] == "none", "landscape asleep: no strip")
        check(abs(m["cy"] - (1080 - m["R"] * 1.35 - 1080 * 0.04)) < 3, f"landscape: my sleeping corner unchanged {m}")
        SHOTS and pg.screenshot(path=str(SHOTS / "landscape-asleep.png"))
        check(not errors, f"landscape: no page errors {errors[:2]}")
        pg.close()

        for (vw, vh, url, disp, tag) in ((1080, 1920, "/index.html", {}, "big"),
                                         (1920, 1080, "/rotate.html?r=90", {"rotate": 90}, "rot90")):
            fw, fh = 1080, 1920
            print(f"--- {tag}")
            st = fresh()
            pg, errors = open_page(br, vw, vh, url, disp, st)
            fr = pg.frames[1] if tag == "rot90" else pg.main_frame
            box, me, fc_on, push = helpers(pg, fr)
            shot = lambda n: SHOTS and pg.screenshot(path=str(SHOTS / f"{tag}-{n}.png"))
            inside = lambda b: b["l"] >= -1 and b["t"] >= -1 and b["r"] <= fw + 1 and b["b"] <= fh + 1

            check(fr.evaluate("document.body.classList.contains('board-on')"), "awake: info board on")
            check(fc_on(), "awake: strip on")
            fc, sysb = box("#dash .fc"), box("#dash .sys")
            check(inside(fc) and fc["w"] > fw * 0.85, f"strip full width {fc}")
            check(above(fc, sysb) and sysb["b"] > fh - 80, f"strip above the me row, me row at the bottom {sysb}")
            check(above(box("#dash .cal"), fc) and above(box("#dash .lst"), fc), "calendar + list above the strip")
            cells = fr.evaluate("Array.from(document.querySelectorAll('#dash .fc .d')).map(e => e.innerText.replace(/\\s+/g, ' ').trim())")
            check(len(cells) == 7, f"7 day cells {cells}")
            check(cells and cells[0].startswith("Today"), "first cell is today")
            rains = fr.evaluate("Array.from(document.querySelectorAll('#dash .fc .rn')).map(e => e.classList.contains('no') ? null : e.innerText.trim())")
            check(rains == [None, "20%", "65%", None, None, "40%", None], f"rain only when >= 20% {rains}")
            check(fr.evaluate("document.querySelectorAll('#dash .fc svg.ic').length") == 7, "an icon per day")
            ts = fr.evaluate("window.WALTER_TEXT_SCALE ? window.WALTER_TEXT_SCALE() : 1")  # the global text-size setting
            check(box("#dash .fc .hl b")["fs"] >= 30 * ts - 0.5 and box("#dash .fc .dn")["fs"] >= 24 * ts - 0.5,
                  f"big enough to read across the room (text scale {ts}; 80% strip, 6 Nov)")
            check(not overlap(fc, me()), "strip clear of me")
            cards = fr.evaluate("""Array.from(document.querySelectorAll('#dash .dayv > *')).filter(e =>
                getComputedStyle(e).display !== 'none' && e.getBoundingClientRect().height > 0).map(e => {
                const b = e.getBoundingClientRect(); return { c: e.className, l: b.left, r: b.right, t: b.top, b: b.bottom }; })""")
            for c in cards:
                if "fc" not in c["c"].split():
                    check(not overlap(fc, c, 2), f"strip clear of {c['c']}")
            fr.evaluate("""(() => { const r = document.getElementById('restart'); r.textContent = 'Restarting...'; r.className = 'down'; })()""")
            push({"t": "config", "captions": True})
            push({"t": "state", "state": "speaking"})
            push({"t": "caption", "who": "walter", "text": "Rain on Saturday and Sunday, then a dry Monday."})
            fr.wait_for_timeout(1000)
            cap, rs = box("#caption"), box("#restart")
            check(cap["op"] > 0.9 and not overlap(cap, fc), f"caption clear of the strip {cap}")
            check(inside(rs) and not overlap(rs, fc), f"restart pill clear of the strip {rs}")
            shot("1-speaking")
            push({"t": "state", "state": "idle"})
            fr.evaluate("document.getElementById('restart').className = ''")
            fr.wait_for_timeout(800)
            shot("0-idle")

            st["card"] = {"id": 1, "title": "Pair your phone", "body": "Code 482 913", "img": "/api/setup/qr.svg"}
            fr.wait_for_timeout(2600)
            cd = box("#card")
            check(cd["op"] > 0.9 and inside(cd) and not overlap(cd, box("#dash .fc")), f"pairing card (QR) clear of the strip {cd}")
            check(fc_on(), "card: strip stays")
            shot("0b-card")
            st["card"] = {"id": 2, "clear": True}
            fr.wait_for_timeout(2600)

            t = time.time()
            st["focus"] = {"box": {"task": "Write the quarterly summary", "start": t - 300, "end": t + 1200,
                                   "status": "running", "planned_min": 25}, "strip": "auto"}
            fr.wait_for_timeout(6000)
            rg = box("#focusring")
            check(rg["op"] > 0.9 and not overlap(rg, box("#dash .fc")), f"focus ring clear of the strip {rg}")
            shot("2-focus")
            st["focus"] = {"box": None}
            fr.wait_for_timeout(4000)

            st["canvas"] = {"id": 11, "title": "Weekend plan"}
            fr.wait_for_timeout(3500)
            check(not fc_on() and box("#dash .fc")["disp"] == "none", "canvas open: strip hidden")
            shot("3-canvas")
            st["canvas"] = {"id": 12, "hide": True}
            fr.wait_for_timeout(3500)
            check(fc_on(), "canvas closed: strip back")
            st["browser"] = {"id": 21, "open": True, "title": "Example Domain", "url": "https://example.com/",
                             "w": 1024, "h": 810, "port": srv.server_port, "token": "x", "alive": time.time() + 600}
            fr.wait_for_timeout(3500)
            check(not fc_on(), "browser open: strip hidden")
            st["browser"] = {"id": 22, "open": False}
            fr.wait_for_timeout(3000)
            check(not errors, f"no page errors {errors[:3]}")
            pg.close()

            for at, state, name, want, qr in (("22:30", "idle", "4-night-awake", True, False),
                                              ("14:00", "sleeping", "5-asleep-day", True, False),
                                              ("14:00", "sleeping", "5b-asleep-day-qr", True, True),
                                              ("22:30", "sleeping", "6-asleep-night", True, False),
                                              ("02:00", "sleeping", "7-asleep-dark", False, False)):
                pg, errors = open_page(br, vw, vh, url + ("&" if "?" in url else "?") + "at=" + at, disp, fresh(), qr)
                fr = pg.frames[1] if tag == "rot90" else pg.main_frame
                box, me, fc_on, push = helpers(pg, fr)
                if state == "sleeping":
                    push({"t": "state", "state": "sleeping"})
                    fr.wait_for_timeout(4500)
                check(fc_on() == want, f"{name}: strip {'on' if want else 'off'}")
                if want:
                    fc, m = box("#dash .fc"), me()
                    check(not overlap(fc, m), f"{name}: I'm clear of the strip {m}")
                    if name.startswith("5"):
                        ft, mei = box("#dash .foot"), box("#dash .foot .mei")
                        check(not overlap(fc, ft) and not overlap(m, ft), f"{name}: footer clear of the strip and me {ft}")
                        check(above(fc, ft) and ft["b"] > fh - 80, f"{name}: strip above the footer, footer at the bottom")
                        check(above(fc, mei) and mei["b"] > fh - 140, f"{name}: me row under the strip, near the bottom {mei}")
                        check(above(box("#dash .lst"), fc), f"{name}: list above the strip")
                        qrb = box("#dash .foot .qr")
                        if qr:
                            check(qrb["w"] > 50 and above(fc, qrb) and inside(qrb), f"{name}: QR shown under the strip {qrb}")
                        else:
                            check(qrb["w"] == 0, f"{name}: no QR without a link")
                    if name.startswith("6"):
                        nm = box("#dash .nightv .mei")
                        check(above(box("#dash .nightv .t"), fc) and above(fc, nm), f"{name}: night clock, strip, me row in that order")
                        check(not overlap(fc, m) and not overlap(nm, m), f"{name}: strip and me row clear of me")
                    if state == "sleeping" and name != "4-night-awake":
                        check(box("#dash .fc")["op"] < 0.7 if "night" in name else True, f"{name}: dimmed in quiet hours")
                SHOTS and pg.screenshot(path=str(SHOTS / f"{tag}-{name}.png"))
                check(not errors, f"{name}: no page errors {errors[:2]}")
                pg.close()
        br.close()
    srv.shutdown()
    print("PASS" if not fails else f"FAIL ({len(fails)})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
