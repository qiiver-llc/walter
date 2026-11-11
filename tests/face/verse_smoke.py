"""Headless smoke test of the verse of the day under the clock (dashboard.js .vs). Run with the
browser venv's python:
~/.walter/browser-venv/bin/python tests/face/verse_smoke.py [shots_dir]

Generic canned data, fake brain websocket, face/ served locally. The verse comes from a notes
item "Verse: <text> - <ref>" added today (routine #24). Checks, per screen:
  1080x1920 and 1920x1080 via rotate.html (turned 90): awake board: under the date in the clock's
    column, clear of me and the weather, at most two lines, reference last; hidden with the canvas
    or browser open and back after; quiet hours awake: still there (board dims); dark hours: gone.
    Asleep: day (column under the clock), quiet hours (night clock, dim), dark hours (hidden).
  1920x1080 landscape: asleep day under the clock with the weather card clear below it; quiet
    hours dim under the night clock; dark hours hidden; awake: no board, nothing shown.
  Everywhere: no "Verse:" row in the Notes list (other notes stay); an old verse (yesterday)
  shows nothing; a long verse is cut to two lines with an ellipsis and keeps its reference.
Prints PASS/FAIL."""
import functools
import http.server
import json
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from portrait_smoke import FAKE_WS, sample_data  # noqa: E402

FACE = Path(__file__).resolve().parents[2] / "face"
SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None
SHORT = "Verse: Be still, and know that I am God. - Psalm 46:10"
LONG = ("Verse: Trust in the LORD with all thine heart; and lean not unto thine own understanding. In all thy ways "
        "acknowledge him, and he shall direct thy paths. And be not wise in thine own eyes: fear the LORD, and depart "
        "from evil. - Proverbs 3:5-7")


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

    def open_page(br, vw, vh, url, display, st, verse=SHORT, age=60):
        now = time.time()
        data = sample_data(now)
        items = data["dashboard-list.json"]["items"]
        items.append({"id": 20, "text": "Spare key is in the blue drawer", "section": "notes", "created": now - 300})
        if verse:
            items.append({"id": 21, "text": verse, "section": "notes", "created": now - age})
        pg = br.new_page(viewport={"width": vw, "height": vh})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.add_init_script(FAKE_WS)
        pg.add_init_script("try { localStorage.clear(); } catch (e) {}")
        js = lambda body: (lambda r: r.fulfill(status=200, content_type="application/json", body=body))
        pg.route("**/display.json*", lambda r: js(json.dumps(display))(r))
        pg.route("**/focus.json*", lambda r: js(json.dumps({"box": None}))(r))
        pg.route("**/card.json*", lambda r: js(json.dumps({"id": 0, "clear": True}))(r))
        pg.route("**/canvas.json*", lambda r: js(json.dumps(st["canvas"]))(r))
        pg.route("**/canvas-doc.json*", lambda r: js(json.dumps({"id": st["canvas"]["id"], "kind": "md", "content": "# Plan\n\nText"}))(r))
        pg.route("**/browser.json*", lambda r: js(json.dumps(st["browser"]))(r))
        for f, d in data.items():
            pg.route(f"**/{f}*", (lambda body: lambda r: js(body)(r))(json.dumps(d)))
        pg.route("**/background.json*", js("{}"))
        pg.route("**/api/**", js("[]"))
        pg.route("**/stream.mjpg*", lambda r: r.fulfill(status=200, content_type="image/png", body=st["png"]))
        pg.route("**/events*", lambda r: r.fulfill(status=204, body=""))
        pg.goto(root + url)
        pg.wait_for_timeout(3500)
        return pg, errors

    def helpers(frame):
        box = lambda sel: frame.evaluate("""s => { const e = document.querySelector(s); if (!e) return null;
            const b = e.getBoundingClientRect(), cs = getComputedStyle(e);
            return { l: b.left, r: b.right, t: b.top, b: b.bottom, w: b.width, h: b.height, op: +cs.opacity,
                     disp: cs.display, fs: parseFloat(cs.fontSize), lh: parseFloat(cs.lineHeight), color: cs.color }; }""", sel)
        me = lambda: frame.evaluate("""(() => { const p = window.WALTER_STATE.pos;
            return { l: p.cx - p.R * 1.1, r: p.cx + p.R * 1.1, t: p.cy - p.R * 1.25, b: p.cy + p.R * 1.15 }; })()""")
        on = lambda: frame.evaluate("document.body.classList.contains('vs-on')")
        push = lambda m: frame.evaluate("m => window.__push(m)", m)
        text = lambda sel: frame.evaluate("s => { const e = document.querySelector(s); return e ? e.innerText : ''; }", sel)
        return box, me, on, push, text

    def verse_ok(box, text, name, long=False):
        v = box("#dash .vs")
        lines = round(v["h"] / v["lh"]) if v and v["lh"] else 99
        t = text("#dash .vs")
        check(v["disp"] != "none" and v["h"] > 0 and 1 <= lines <= 2, f"{name}: verse shown, {lines} line(s)")
        if long:
            check("…" in t and t.rstrip().endswith("Proverbs 3:5-7"), f"{name}: long verse cut with an ellipsis, reference kept: {t!r}")
        else:
            check(t.startswith("Be still") and t.rstrip().endswith("Psalm 46:10") and "Verse:" not in t, f"{name}: text then reference: {t!r}")
        check(v["fs"] >= 12, f"{name}: readable size {v['fs']:.1f}px")
        return v

    def list_ok(text, name):
        lst = text("#dash .lst")
        check("Verse:" not in lst and "Psalm" not in lst and "Spare key" in lst, f"{name}: Notes list without the verse, other notes stay")

    with sync_playwright() as p:
        br = p.chromium.launch(executable_path="/usr/bin/chromium")
        tmp = br.new_page(viewport={"width": 1024, "height": 810})
        tmp.set_content("<body style='font:22px sans-serif;margin:40px;background:#f6f6f6'><h1>Example Domain</h1></body>")
        page_png = tmp.screenshot()
        tmp.close()
        fresh = lambda: {"canvas": {"id": 0, "hide": True}, "browser": {"id": 0, "open": False}, "png": page_png}
        shot = lambda pg, n: SHOTS and pg.screenshot(path=str(SHOTS / f"{n}.png"))

        # ---- landscape
        print("--- landscape")
        pg, errors = open_page(br, 1920, 1080, "/index.html?at=14:00", {}, fresh())
        box, me, on, push, text = helpers(pg.main_frame)
        check(not on(), "landscape awake: no board, no verse")
        push({"t": "state", "state": "sleeping"})
        pg.wait_for_timeout(4500)
        v = verse_ok(box, text, "landscape asleep day")
        d, wx, cal = box("#dash .clk .d"), box("#dash .wx"), box("#dash .cal")
        check(v["t"] >= d["b"] - 1 and abs(v["l"] - d["l"]) < 2, "landscape: right under the date")
        check(not overlap(v, wx) and v["b"] <= wx["t"], f"landscape: weather card clear below {wx}")
        check(not overlap(v, cal) and not overlap(wx, box("#dash .foot")), "landscape: clear of the calendar; weather clear of the footer")
        list_ok(text, "landscape")
        shot(pg, "landscape-asleep-day")
        check(not errors, f"landscape: no page errors {errors[:2]}")
        pg.close()
        for at, name, want in (("22:30", "landscape-asleep-night", True), ("02:00", "landscape-asleep-dark", False)):
            pg, errors = open_page(br, 1920, 1080, "/index.html?at=" + at, {}, fresh())
            box, me, on, push, text = helpers(pg.main_frame)
            push({"t": "state", "state": "sleeping"})
            pg.wait_for_timeout(4500)
            check(on() == want, f"{name}: verse {'on' if want else 'off'}")
            if want:
                v = verse_ok(box, text, name)
                check(v["t"] >= box("#dash .nightv .d")["b"] - 1, f"{name}: under the night clock")
                check(float(v["color"].split(",")[-1].strip(" )")) < 0.5, f"{name}: dim {v['color']}")
            shot(pg, name)
            check(not errors, f"{name}: no page errors {errors[:2]}")
            pg.close()
        # no verse today / a long one
        pg, errors = open_page(br, 1920, 1080, "/index.html?at=14:00", {}, fresh(), age=86400 * 1.5)
        box, me, on, push, text = helpers(pg.main_frame)
        push({"t": "state", "state": "sleeping"})
        pg.wait_for_timeout(4500)
        check(not on() and box("#dash .vs")["disp"] == "none", "yesterday's verse: nothing shown")
        list_ok(text, "yesterday's verse")
        pg.close()
        pg, errors = open_page(br, 1920, 1080, "/index.html?at=14:00", {}, fresh(), verse=LONG)
        box, me, on, push, text = helpers(pg.main_frame)
        push({"t": "state", "state": "sleeping"})
        pg.wait_for_timeout(4500)
        verse_ok(box, text, "landscape long", long=True)
        shot(pg, "landscape-asleep-long")
        pg.close()

        # ---- portrait: the big board and the rotated screen
        for (vw, vh, url, disp, tag) in ((1080, 1920, "/index.html", {}, "big"),
                                         (1920, 1080, "/rotate.html?r=90", {"rotate": 90}, "rot90")):
            print(f"--- {tag}")
            for verse, sfx in ((SHORT, ""), (LONG, "-long")):
                st = fresh()
                pg, errors = open_page(br, vw, vh, url + ("&" if "?" in url else "?") + "at=14:00", disp, st, verse=verse)
                fr = pg.frames[1] if tag == "rot90" else pg.main_frame
                box, me, on, push, text = helpers(fr)
                check(fr.evaluate("document.body.classList.contains('board-on')"), f"{tag}{sfx}: board on")
                v = verse_ok(box, text, f"{tag}{sfx} board", long=bool(sfx))
                d = box("#dash .clk .d")
                check(v["t"] >= d["b"] - 1 and abs(v["l"] - d["l"]) < 2, f"{tag}{sfx}: under the date")
                check(not overlap(v, me()) and not overlap(v, box("#dash .wx")), f"{tag}{sfx}: clear of me and the weather {v}")
                check(v["b"] <= box("#dash .cal")["t"], f"{tag}{sfx}: above the calendar")
                list_ok(text, f"{tag}{sfx}")
                shot(pg, f"{tag}-board{sfx}")
                if not sfx:
                    st["canvas"] = {"id": 11, "title": "Weekend plan"}
                    fr.wait_for_timeout(3500)
                    check(not on() and box("#dash .vs")["disp"] == "none", f"{tag}: canvas open, verse hidden")
                    shot(pg, f"{tag}-board-canvas")
                    st["canvas"] = {"id": 12, "hide": True}
                    fr.wait_for_timeout(3500)
                    check(on(), f"{tag}: canvas closed, verse back")
                    st["browser"] = {"id": 21, "open": True, "title": "Example Domain", "url": "https://example.com/",
                                     "w": 1024, "h": 810, "port": srv.server_port, "token": "x", "alive": time.time() + 600}
                    fr.wait_for_timeout(3500)
                    check(not on(), f"{tag}: browser open, verse hidden")
                check(not errors, f"{tag}{sfx}: no page errors {errors[:2]}")
                pg.close()
            for at, state, name, want in (("22:30", "idle", "night-awake", True), ("02:00", "idle", "dark-awake", False),
                                          ("14:00", "sleeping", "asleep-day", True), ("22:30", "sleeping", "asleep-night", True),
                                          ("02:00", "sleeping", "asleep-dark", False)):
                pg, errors = open_page(br, vw, vh, url + ("&" if "?" in url else "?") + "at=" + at, disp, fresh())
                fr = pg.frames[1] if tag == "rot90" else pg.main_frame
                box, me, on, push, text = helpers(fr)
                if state == "sleeping":
                    push({"t": "state", "state": "sleeping"})
                    fr.wait_for_timeout(4500)
                check(on() == want, f"{tag} {name}: verse {'on' if want else 'off'}")
                if want:
                    v = verse_ok(box, text, f"{tag} {name}")
                    check(not overlap(v, me()), f"{tag} {name}: clear of me")
                    if name == "asleep-day":
                        check(v["t"] >= box("#dash .clk .d")["b"] - 1 and v["b"] <= box("#dash .wx")["t"], f"{tag} {name}: between the date and the weather")
                        list_ok(text, f"{tag} {name}")
                    if name == "asleep-night":
                        check(v["t"] >= box("#dash .nightv .d")["b"] - 1, f"{tag} {name}: under the night clock")
                shot(pg, f"{tag}-{name}")
                check(not errors, f"{tag} {name}: no page errors {errors[:2]}")
                pg.close()
        br.close()
    srv.shutdown()
    print("PASS" if not fails else f"FAIL ({len(fails)})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
