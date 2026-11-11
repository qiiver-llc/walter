"""Headless smoke test of the portrait "me" strip (icons + badges instead of text for my own
status: state dot, mic, workers count, usage ring, camera/cloud only while on; words only
for attention: Restarting, Offline). Run with the browser venv's python:
~/.walter/browser-venv/bin/python tests/face/me_icons_smoke.py [shots_dir]

Big-screen board at 1080x1920 and rotated 90 inside 1920x1080 (rotate.html): idle,
speaking, muted, workers running (+ queued, camera, cloud), restarting; asleep day and
night; text scale 0.6 vs 1 (the strip follows it). Landscape asleep must keep the old text
status line. Generic sample data only. Prints PASS/FAIL."""
import functools
import http.server
import json
import re
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from portrait_smoke import FAKE_WS, QR_SVG, sample_data  # noqa: E402

FACE = Path(__file__).resolve().parents[2] / "face"
SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None
ATTENTION = {"Restarting", "Offline"}


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

    def open_page(br, vw, vh, url, display, tasks):
        now = time.time()
        data = sample_data(now)
        data["usage.json"] = {"ok": True, "updated": now, "session": {"pct": 34}, "week": {"pct": 72}}
        data["checklist.json"] = {"items": [], "changed_at": 0}
        data["dashboard-events.json"]["updated"] = now
        pg = br.new_page(viewport={"width": vw, "height": vh})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.add_init_script(FAKE_WS)
        pg.add_init_script("try { localStorage.clear(); } catch (e) {}")
        js = lambda body: (lambda r: r.fulfill(status=200, content_type="application/json", body=body))
        pg.route("**/display.json*", lambda r: js(json.dumps(display))(r))
        for f in ("focus.json", "canvas.json", "browser.json", "background.json"):
            pg.route(f"**/{f}*", js("{}"))
        pg.route("**/card.json*", js('{"id": 0, "clear": true}'))
        for f, d in data.items():
            pg.route(f"**/{f}*", (lambda body: lambda r: js(body)(r))(json.dumps(d)))
        pg.route("**/api/**", js("[]"))
        pg.route("**/api/net/status*", js('{"online": true}'))
        pg.route("**/api/tasks*", lambda r: js(json.dumps(tasks["list"]))(r))
        pg.route("**/api/reminders*", lambda r: js(json.dumps(tasks["queue"]))(r))
        pg.route("**/api/setup/qr.svg*", lambda r: r.fulfill(status=200, content_type="image/svg+xml", body=QR_SVG))
        pg.route("**/events*", lambda r: r.fulfill(status=204, body=""))
        pg.goto(root + url)
        pg.wait_for_timeout(3500)
        return pg, errors

    def strip(fr, sel):
        return fr.evaluate("""s => { const e = document.querySelector(s); if (!e) return null;
            const b = e.getBoundingClientRect(), cs = getComputedStyle(e);
            return { text: e.innerText.trim(), html: e.innerHTML, disp: cs.display, vis: cs.visibility,
                     fs: parseFloat(cs.fontSize), h: b.height, w: b.width, t: b.top, b: b.bottom, l: b.left, r: b.right,
                     icons: e.querySelectorAll('svg, .orb').length }; }""", sel)

    def words(text):
        return set(w for w in re.findall(r"[A-Za-z]+", text or ""))

    with sync_playwright() as p:
        br = p.chromium.launch(executable_path="/usr/bin/chromium")
        now = time.time()
        busy = [{"id": 7, "title": "Compare three robot vacuums", "status": "running", "started": now - 420},
                {"id": 8, "title": "Tidy the notes", "status": "running", "started": now - 60}]

        for (vw, vh, url, disp, tag) in ((1080, 1920, "/index.html", {"text_scale": 0.8}, "big"),
                                         (1920, 1080, "/rotate.html?r=90", {"rotate": 90, "text_scale": 0.8}, "rot90")):
            print(f"--- {tag}")
            tasks = {"list": [], "queue": []}
            pg, errors = open_page(br, vw, vh, url, disp, tasks)
            fr = pg.frames[1] if tag == "rot90" else pg.main_frame
            push = lambda m: fr.evaluate("m => window.__push(m)", m)
            shot = lambda n: SHOTS and pg.screenshot(path=str(SHOTS / f"{tag}-{n}.png"))
            sel = "#dash .sys .mei"

            check(fr.evaluate("document.body.classList.contains('board-on')"), "board on")
            m = strip(fr, sel)
            check(m and m["vis"] == "visible" and m["h"] > 10, f"idle: strip shown {m and {k: m[k] for k in ('h', 'w', 'fs')}}")
            check(not words(m["text"]), f"idle: no words, only icons/numbers ({m['text']!r})")
            check("orb idle" in m["html"] and "mic" in m["html"] and "use" in m["html"], "idle: state dot, mic, usage ring")
            check("cam" not in m["html"] and "cloud" not in m["html"] and " wk" not in m["html"], "idle: no camera/cloud/workers")
            shot("1-idle")

            push({"t": "state", "state": "speaking"})
            push({"t": "caption", "who": "walter", "text": "Sure, it's on the list."})
            fr.wait_for_timeout(1500)
            check("orb speaking" in strip(fr, sel)["html"], "speaking: pulsing dot")
            shot("2-speaking")
            push({"t": "state", "state": "listening"})
            fr.wait_for_timeout(1300)
            check("orb listening" in strip(fr, sel)["html"], "listening: ring")
            push({"t": "state", "state": "thinking"})
            fr.wait_for_timeout(1300)
            check("orb thinking" in strip(fr, sel)["html"], "thinking: turning arc")
            shot("3-thinking")
            push({"t": "state", "state": "idle"})

            push({"t": "mute", "on": True})
            fr.wait_for_timeout(1500)
            m = strip(fr, sel)
            check("mic off" in m["html"] and not words(m["text"]), "muted: struck-through mic, no words")
            check(fr.evaluate("getComputedStyle(document.getElementById('micoff')).visibility") == "hidden",
                  "muted: separate Mic off pill steps aside on the board")
            shot("4-muted")
            push({"t": "mute", "on": False})

            push({"t": "hwmute", "on": True})   # the mic's own button: amber struck-through mic
            fr.wait_for_timeout(1500)
            m = strip(fr, sel)
            check("mic off hw" in m["html"] and not words(m["text"]), "hw muted: amber struck-through mic, no words")
            check(fr.evaluate("document.getElementById('micoff').classList.contains('hw')"), "hw muted: pill is amber")
            shot("4b-hw-muted")
            push({"t": "mute", "on": True})     # the software mute wins (red) while both are on
            fr.wait_for_timeout(1300)
            check("mic off hw" not in strip(fr, sel)["html"], "both muted: red software-mute icon")
            push({"t": "mute", "on": False})
            push({"t": "hwmute", "on": False})
            fr.wait_for_timeout(1300)
            check("mic off" not in strip(fr, sel)["html"], "hw unmuted: plain mic again")

            tasks["list"], tasks["queue"] = busy, [{"text": "QUEUED [Write the weekly summary] ..."}]
            push({"t": "jobs", "n": 2})
            push({"t": "camera", "on": True})
            push({"t": "cloud", "tts": True, "stt": False})
            fr.wait_for_timeout(6500)
            m = strip(fr, sel)
            check(" wk" in m["html"] and "2" in m["text"] and "+1" in m["text"], f"workers: count 2 + 1 queued ({m['text']!r})")
            check("Compare" not in m["text"], "workers: no titles in the strip")
            check("cam" in m["html"] and "cloud" in m["html"], "camera + cloud icons while on")
            check(not words(m["text"]), "busy: still no words")
            shot("5-workers")

            fr.evaluate("window.walterLink(true); window.walterLink(false)")  # (the fake socket opens before walterLink exists)
            fr.wait_for_timeout(1800)
            m = strip(fr, sel)
            check("Restarting" in m["text"] and words(m["text"]) <= ATTENTION, f"restarting: says so ({m['text']!r})")
            check(fr.evaluate("getComputedStyle(document.getElementById('restart')).visibility") == "hidden",
                  "restarting: corner pill steps aside on the board")
            shot("6-restarting")
            fr.evaluate("window.walterLink(true)")
            fr.evaluate("document.getElementById('netIcon').style.display = 'block'")
            fr.wait_for_timeout(1300)
            check("Offline" in strip(fr, sel)["text"], "offline: says so")
            shot("6b-offline")
            check(not errors, f"no page errors {errors[:3]}")
            pg.close()

            # text scale: the strip (and its icons) follow it
            sizes = {}
            for ts in (0.6, 1.0):
                pg, errors = open_page(br, vw, vh, url, {**disp, "text_scale": ts}, {"list": busy, "queue": []})
                fr = pg.frames[1] if tag == "rot90" else pg.main_frame
                sizes[ts] = fr.evaluate("""(() => { const e = document.querySelector('#dash .sys .mei svg');
                    return e ? e.getBoundingClientRect().width : 0; })()""")
                pg.close()
            check(sizes[1.0] > 0 and abs(sizes[0.6] / sizes[1.0] - 0.6) < 0.05, f"text scale: icons scale ({sizes})")

            # asleep: day dashboard + quiet hours
            for at, name in (("14:00", "7-asleep-day"), ("22:30", "8-asleep-night"), ("02:00", "9-asleep-dark")):
                pg, errors = open_page(br, vw, vh, url + ("&" if "?" in url else "?") + "at=" + at, disp,
                                       {"list": busy[:1], "queue": []})
                fr = pg.frames[1] if tag == "rot90" else pg.main_frame
                fr.evaluate("m => window.__push(m)", {"t": "mute", "on": True})
                fr.evaluate("m => window.__push(m)", {"t": "state", "state": "sleeping"})
                fr.wait_for_timeout(4500)
                sel2 = "#dash .foot .mei" if at == "14:00" else "#dash .nightv .mei"
                m = strip(fr, sel2)
                if at == "02:00":
                    check(m["h"] == 0 or not m["text"], f"{name}: nothing but attention in the dark ({m['text']!r})")
                else:
                    check(m["disp"] != "none" and m["h"] > 5 and "moon" in m["html"] and "mic off" in m["html"],
                          f"{name}: moon + struck mic")
                    check(not words(m["text"]), f"{name}: no words ({m['text']!r})")
                if at == "14:00":
                    check(strip(fr, "#dash .foot .stat")["disp"] == "none", f"{name}: old text status hidden")
                SHOTS and pg.screenshot(path=str(SHOTS / f"{tag}-{name}.png"))
                check(not errors, f"{name}: no page errors {errors[:2]}")
                pg.close()

        # landscape asleep: unchanged text status, no strip
        print("--- landscape")
        pg, errors = open_page(br, 1920, 1080, "/index.html?at=14:00", {"text_scale": 0.8}, {"list": busy, "queue": []})
        pg.evaluate("m => window.__push(m)", {"t": "state", "state": "sleeping"})
        pg.wait_for_timeout(4500)
        st, me = strip(pg, "#dash .foot .stat"), strip(pg, "#dash .foot .mei")
        check(st["disp"] != "none" and "Mic" in st["text"], f"landscape: text status as before ({st['text']!r})")
        check(me["disp"] == "none", "landscape: no icon strip")
        SHOTS and pg.screenshot(path=str(SHOTS / "landscape-asleep.png"))
        check(not errors, f"landscape: no page errors {errors[:2]}")
        pg.close()
        br.close()
    srv.shutdown()
    print("PASS" if not fails else f"FAIL ({len(fails)})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
