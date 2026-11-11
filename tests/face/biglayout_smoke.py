"""Headless smoke test of the big-screen portrait layout (html.big: I sit small at the top,
an always-on info board fills the rest). Run with the browser venv's python:
~/.walter/browser-venv/bin/python tests/face/biglayout_smoke.py [shots_dir]

Serves face/ over a throwaway local HTTP server with a fake brain websocket and canned,
generic data (no personal info). Runs:
  1080x1920 index.html (layout auto -> large)
  1920x1080 rotate.html turned 90 (what the wall monitor shows; checks run inside the frame)
  800x1280 (auto -> compact) and 1920x1080 landscape: must NOT use the big layout
  1080x1920 with layout "compact": the old big-face portrait
States: idle, speaking caption, card, focus ring, canvas, browser, quiet-hours (night)
board while awake, and asleep (the normal dashboard). Prints PASS/FAIL."""
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

    def open_page(br, vw, vh, url, display, st, extra=None):
        now = time.time()
        data = sample_data(now)
        data["usage.json"] = {"ok": True, "updated": now, "session": {"pct": 34}, "week": {"pct": 72}}
        data["checklist.json"] = {"items": [{"text": "Tidy the caption code", "status": "doing"}], "changed_at": 0}
        data["dashboard-events.json"]["updated"] = now
        if extra:
            data.update(extra)
        pg = br.new_page(viewport={"width": vw, "height": vh})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.add_init_script(FAKE_WS)
        pg.add_init_script("try { localStorage.clear(); } catch (e) {}")
        js = lambda body: (lambda r: r.fulfill(status=200, content_type="application/json", body=body))
        # text_scale 1 pinned: the min-font-size checks below are for the original size
        pg.route("**/display.json*", lambda r: js(json.dumps({"text_scale": 1, **display}))(r))
        pg.route("**/focus.json*", lambda r: js(json.dumps(st["focus"]))(r))
        pg.route("**/card.json*", lambda r: js(json.dumps(st["card"]))(r))
        pg.route("**/canvas.json*", lambda r: js(json.dumps(st["canvas"]))(r))
        pg.route("**/canvas-doc.json*", lambda r: js(json.dumps({"id": st["canvas"]["id"], "kind": "md", "content": CANVAS_MD}))(r))
        pg.route("**/browser.json*", lambda r: js(json.dumps(st["browser"]))(r))
        for f, d in data.items():
            pg.route(f"**/{f}*", (lambda body: lambda r: js(body)(r))(json.dumps(d)))
        pg.route("**/background.json*", js("{}"))
        pg.route("**/api/**", js("[]"))
        pg.route("**/api/tasks*", js(json.dumps([
            {"id": 7, "title": "Compare three robot vacuums", "status": "running", "started": now - 420},
            {"id": 6, "title": "Fix the caption timing", "status": "done", "started": now - 900, "finished": now - 60}])))
        pg.route("**/api/reminders*", js(json.dumps([{"text": "QUEUED [Write the weekly summary] ..."}])))
        pg.route("**/api/setup/qr.svg*", lambda r: r.fulfill(status=200, content_type="image/svg+xml", body=QR_SVG))
        pg.route("**/stream.mjpg*", lambda r: r.fulfill(status=200, content_type="image/png", body=st["png"]))
        pg.route("**/events*", lambda r: r.fulfill(status=204, body=""))
        pg.goto(root + url)
        pg.wait_for_timeout(3500)
        return pg, errors

    with sync_playwright() as p:
        br = p.chromium.launch(executable_path="/usr/bin/chromium")
        tmp = br.new_page(viewport={"width": 1024, "height": 810})
        tmp.set_content("<body style='font:22px sans-serif;margin:40px;background:#f6f6f6'><h1>Example Domain</h1>"
                        "<p>This domain is for use in illustrative examples in documents.</p></body>")
        page_png = tmp.screenshot()
        tmp.close()

        # ---- not big: landscape, small portrait, compact setting
        for (vw, vh, disp, tag) in ((1920, 1080, {}, "landscape"), (800, 1280, {}, "800x1280"),
                                    (1080, 1920, {"layout": "compact"}, "compact")):
            st = {"focus": {"box": None}, "card": {"id": 0, "clear": True}, "canvas": {"id": 0, "hide": True},
                  "browser": {"id": 0, "open": False}, "png": page_png}
            pg, errors = open_page(br, vw, vh, "/index.html", disp, st)
            big = pg.evaluate("document.documentElement.classList.contains('big')")
            board = pg.evaluate("document.body.classList.contains('board-on')")
            pos = pg.evaluate("window.WALTER_STATE.pos")
            check(not big and not board, f"{tag}: no big layout / board")
            check(abs(pos["R"] - min(vw, vh) * 0.26) < 2, f"{tag}: my size unchanged ({pos['R']:.0f}px)")
            check(not errors, f"{tag}: no page errors {errors[:2]}")
            SHOTS and pg.screenshot(path=str(SHOTS / f"{tag}-idle.png"))
            pg.close()

        # ---- big: direct 1080x1920, and rotated 90 inside 1920x1080
        for (vw, vh, url, disp, tag) in ((1080, 1920, "/index.html", {}, "big"),
                                         (1920, 1080, "/rotate.html?r=90", {"rotate": 90, "layout": "auto"}, "rot90")):
            st = {"focus": {"box": None}, "card": {"id": 0, "clear": True}, "canvas": {"id": 0, "hide": True},
                  "browser": {"id": 0, "open": False}, "png": page_png}
            alert = {"dashboard-weather.json": {**sample_data(time.time())["dashboard-weather.json"],
                                                "alerts": [{"event": "Wind Advisory", "severity": "Moderate",
                                                            "ends": time.strftime("%Y-%m-%dT21:00:00")}]}}
            pg, errors = open_page(br, vw, vh, url, disp, st, alert if tag == "rot90" else None)
            fr = pg.frames[1] if tag == "rot90" else pg.main_frame
            fw, fh = 1080, 1920
            print(f"--- {tag}")
            push = lambda m: fr.evaluate("m => window.__push(m)", m)
            shot = lambda n: SHOTS and pg.screenshot(path=str(SHOTS / f"{tag}-{n}.png"))
            me = lambda: fr.evaluate("""(() => { const p = window.WALTER_STATE.pos;
                return { l: p.cx - p.R * 1.1, r: p.cx + p.R * 1.1, t: p.cy - p.R * 1.1, b: p.cy + p.R * 1.15,
                         cx: p.cx, cy: p.cy, R: p.R }; })()""")
            box = lambda sel: fr.evaluate("""s => { const e = document.querySelector(s); if (!e) return null;
                const b = e.getBoundingClientRect(), cs = getComputedStyle(e);
                return { l: b.left, r: b.right, t: b.top, b: b.bottom, w: b.width, h: b.height,
                         op: +cs.opacity, vis: cs.visibility, fs: parseFloat(cs.fontSize) }; }""", sel)
            inside = lambda b: b["l"] >= -1 and b["t"] >= -1 and b["r"] <= fw + 1 and b["b"] <= fh + 1
            cards = lambda: fr.evaluate("""Array.from(document.querySelectorAll('#dash .dayv > *')).filter(e =>
                getComputedStyle(e).display !== 'none' && e.getBoundingClientRect().height > 0).map(e => {
                const b = e.getBoundingClientRect(); return { c: e.className, l: b.left, r: b.right, t: b.top, b: b.bottom,
                over: e.scrollHeight > e.clientHeight + 2 }; })""")

            check(fr.evaluate("innerWidth") == fw and fr.evaluate("innerHeight") == fh, "face sees 1080x1920")
            check(fr.evaluate("document.documentElement.classList.contains('big')"), "big layout on (auto)")
            check(fr.evaluate("document.body.classList.contains('board-on')"), "info board on while awake")
            m = me()
            check(0.40 * 0.26 * fw <= m["R"] <= 0.5 * 0.26 * fw + 1, f"I'm 40-50% of the old size (R {m['R']:.0f})")
            check(abs(m["cx"] - fw / 2) < 5 and m["cy"] < fh * 0.2, f"I'm docked at the top {m}")
            cs = cards()
            for i, a in enumerate(cs):
                check(inside(a), f"board: {a['c']} on screen")
                for b_ in cs[i + 1:]:
                    check(not overlap(a, b_, 2), f"board: {a['c']} vs {b_['c']} no overlap")
                check(not overlap(a, m), f"board: {a['c']} clear of me")
            names = " ".join(c["c"] for c in cs)
            for k in ("clk", "wx", "cal", "lst", "sys"):
                check(k in names, f"board shows {k}")
            check(box("#dash .it")["fs"] >= 30, f"list text big enough ({box('#dash .it')['fs']:.0f}px)")
            check(box("#dash .ev .what")["fs"] >= 30, "calendar text big enough")
            # the status card is the icon strip now (me_icons_smoke.py tests it in detail)
            sys_html = fr.evaluate("document.querySelector('#dash .sys').innerHTML")
            for k in ("orb ", "b mic", "b wk", "b use"):
                check(k in sys_html, f"status strip has {k.strip()!r}")
            check(box("#workers")["vis"] == "hidden" and box("#clock")["vis"] == "hidden", "old workers/clock hidden")
            if tag == "rot90":
                check("Wind Advisory" in fr.evaluate("document.querySelector('#dash .al').innerText"), "alert strip shown")
            shot("1-idle")

            push({"t": "config", "captions": True})
            push({"t": "state", "state": "speaking"})
            push({"t": "caption", "who": "walter",
                  "text": "Ooh, good question. It looks like rain after lunch, so maybe do the fence gate this morning."})
            fr.wait_for_timeout(900)
            cap, m = box("#caption"), me()
            check(cap["op"] > 0.9 and inside(cap), f"caption shown {cap}")
            check(not overlap(cap, m) and cap["t"] < fh * 0.35, "caption just under me, clear of me")
            shot("2-speaking")
            push({"t": "state", "state": "idle"})
            push({"t": "mute", "on": True})
            fr.wait_for_timeout(600)
            check("mic off" in fr.evaluate("document.querySelector('#dash .sys').innerHTML"), "mute shown (struck mic)")
            push({"t": "mute", "on": False})

            st["card"] = {"id": 1, "title": "Pair your phone", "body": "Code 482 913", "img": "/api/setup/qr.svg"}
            fr.wait_for_timeout(2600)
            cd = box("#card")
            check(cd["op"] > 0.9 and inside(cd), f"card on screen {cd}")
            check(fr.evaluate("+getComputedStyle(document.getElementById('card')).zIndex") > 1, "card above the board")
            shot("3-card")
            st["card"] = {"id": 2, "clear": True}
            fr.wait_for_timeout(2600)

            t = time.time()
            st["focus"] = {"box": {"task": "Write the quarterly summary", "start": t - 300, "end": t + 1200,
                                   "status": "running", "planned_min": 25}, "strip": "auto"}
            fr.wait_for_timeout(6000)
            rg, m = box("#focusring"), me()
            check(rg["op"] > 0.9 and inside(rg), f"focus ring on screen {rg}")
            check(not overlap(m, rg), f"focus: I'm clear of the ring {m}")
            check(m["cy"] < fh * 0.2, "focus: I stay at the top")
            check(box("#dash .lst")["op"] < 0.3, "focus: board steps back")
            shot("4-focus")
            st["focus"] = {"box": None}
            fr.wait_for_timeout(4000)

            st["canvas"] = {"id": 11, "title": "Weekend plan"}
            fr.wait_for_timeout(3500)
            cv, m = box("#canvas"), me()
            check(cv["op"] > 0.9 and inside(cv) and not overlap(cv, m), f"canvas over the board, clear of me {cv}")
            push({"t": "state", "state": "speaking"})
            push({"t": "caption", "who": "walter", "text": "It's on screen. Saturday is the busy one."})
            fr.wait_for_timeout(900)
            cap = box("#caption")
            check(not overlap(cap, cv) and not overlap(cap, me()), f"canvas: caption clear {cap}")
            shot("5-canvas")
            push({"t": "state", "state": "idle"})
            st["canvas"] = {"id": 12, "hide": True}
            fr.wait_for_timeout(3500)

            st["browser"] = {"id": 21, "open": True, "title": "Example Domain", "url": "https://example.com/",
                             "w": 1024, "h": 810, "port": srv.server_port, "token": "x", "alive": time.time() + 600}
            fr.wait_for_timeout(3500)
            bw, m = box("#browser"), me()
            check(bw["op"] > 0.9 and inside(bw) and not overlap(bw, m), f"browser over the board, clear of me {bw}")
            shot("6-browser")
            st["browser"] = {"id": 22, "open": False}
            fr.wait_for_timeout(3000)
            check(not errors, f"no page errors {errors[:3]}")
            pg.close()

            # quiet hours, still awake: dimmed board; then asleep: the normal dashboard
            for at, state, name in (("22:30", "idle", "7-night-awake"), ("14:00", "sleeping", "8-asleep")):
                pg, errors = open_page(br, vw, vh, url + ("&" if "?" in url else "?") + "at=" + at, disp, st)
                fr = pg.frames[1] if tag == "rot90" else pg.main_frame
                if state == "sleeping":
                    fr.evaluate("m => window.__push(m)", {"t": "state", "state": "sleeping"})
                    fr.wait_for_timeout(4000)
                    check(fr.evaluate("document.body.classList.contains('dash-on') && !document.body.classList.contains('board-on')"),
                          "asleep: normal dashboard, no board")
                else:
                    check(fr.evaluate("document.body.classList.contains('board-night')"), "quiet hours: board dimmed")
                SHOTS and pg.screenshot(path=str(SHOTS / f"{tag}-{name}.png"))
                check(not errors, f"{name}: no page errors {errors[:2]}")
                pg.close()
        br.close()
    srv.shutdown()
    print("PASS" if not fails else f"FAIL ({len(fails)})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
