"""Headless test of the global text scale (face/textscale.js; walterctl display text-scale).
Run with the browser venv's python:
~/.walter/browser-venv/bin/python tests/face/textscale_smoke.py [shots_dir]

Serves face/ over a throwaway local HTTP server with a fake brain websocket and canned data
(generic sample data from portrait_smoke.py), then for landscape 1920x1080, portrait
1080x1920 (big board and compact), software-rotated 90 inside 1920x1080 and the sleeping
dashboard checks that:
  - text scale 1 gives exactly the font sizes the face had without textscale.js;
  - 0.8 and 0.6 shrink every text element by that factor (absolute sizes) ...
  - ... while my size (the blob/eyes), the QR codes and the panels/buttons stay the same;
  - no extra text gets clipped (overflowing boxes) when it shrinks;
  - a change in display.json applies live (no reload).
Screenshots go to shots_dir (<tag>-ts<scale>.png). Prints PASS/FAIL."""
import functools
import http.server
import json
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from portrait_smoke import FAKE_WS, QR_SVG, sample_data  # noqa: E402

FACE = Path(__file__).resolve().parents[2] / "face"
SHOTS = Path(sys.argv[1]) if len(sys.argv) > 1 else None

# every element with its own visible text: a stable key -> computed font size, plus clipping
FONTS = """(() => {
  const out = {}, seen = {};
  for (const e of document.querySelectorAll('body *')) {
    if (/^(SCRIPT|STYLE|CANVAS|IFRAME|IMG|SVG)$/i.test(e.tagName)) continue;
    if (!Array.from(e.childNodes).some(n => n.nodeType === 3 && n.textContent.trim())) continue;
    let k = e.tagName + '#' + e.id + '.' + (e.getAttribute('class') || '');
    for (let p = e.parentElement; p && p !== document.body; p = p.parentElement)
      k = p.tagName + '#' + p.id + '.' + (p.getAttribute('class') || '') + '>' + k;
    seen[k] = (seen[k] || 0) + 1; k += '@' + seen[k];
    const cs = getComputedStyle(e), r = e.getBoundingClientRect();
    out[k] = { fs: parseFloat(cs.fontSize), shown: r.width > 0 && r.height > 0 && cs.visibility !== 'hidden',
               clip: r.width > 0 && cs.overflow !== 'visible' && (e.scrollWidth > e.clientWidth + 1 ||
                     (cs.webkitLineClamp === 'none' && e.scrollHeight > e.clientHeight + 1)) };
  }
  return out; })()"""
GEOM = """(() => { const p = window.WALTER_STATE.pos, wh = s => { const e = document.querySelector(s);
  if (!e) return null; const b = e.getBoundingClientRect(); return [Math.round(b.width), Math.round(b.height)]; };
  return { R: Math.round(p.R), cardImg: wh('#card .img'), card: wh('#card'), dashQr: wh('#dash .qr'),
           canvas: wh('#canvas'), ts: window.WALTER_TEXT_SCALE ? window.WALTER_TEXT_SCALE() : null }; })()"""


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

    def open_page(br, vw, vh, url, display, card, without_ts=False):
        pg = br.new_page(viewport={"width": vw, "height": vh})
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.add_init_script(FAKE_WS)
        pg.add_init_script("try { localStorage.clear(); } catch (e) {}")
        js = lambda body: (lambda r: r.fulfill(status=200, content_type="application/json", body=body))
        if without_ts:  # the face as it was before textscale.js
            pg.route("**/textscale.js*", lambda r: r.fulfill(status=200, content_type="text/javascript", body=""))
        pg.route("**/display.json*", lambda r: js(json.dumps(display))(r))
        pg.route("**/card.json*", lambda r: js(json.dumps(card))(r))
        for f, d in sample_data(time.time()).items():
            pg.route(f"**/{f}*", (lambda body: lambda r: js(body)(r))(json.dumps(d)))
        for f in ("focus.json", "canvas.json", "browser.json", "background.json", "checklist.json", "usage.json"):
            pg.route(f"**/{f}*", js("{}"))
        pg.route("**/api/**", js("[]"))
        pg.route("**/api/setup/qr.svg*", lambda r: r.fulfill(status=200, content_type="image/svg+xml", body=QR_SVG))
        pg.route("**/events*", lambda r: r.fulfill(status=204, body=""))
        pg.goto(root + url)
        pg.wait_for_timeout(3500)
        return pg, errors

    card = {"id": 1, "title": "Pair your phone", "body": "Code 482 913", "img": "/api/setup/qr.svg"}
    cases = (  # tag, viewport, url, display.json, frame index (rotated face lives in an iframe)
        ("landscape", 1920, 1080, "/index.html", {}, 0),
        ("portrait-big", 1080, 1920, "/index.html", {"layout": "large"}, 0),
        ("portrait-compact", 1080, 1920, "/index.html", {"layout": "compact"}, 0),
        ("rot90", 1920, 1080, "/rotate.html?r=90", {"rotate": 90}, 1),
        ("dash-landscape", 1920, 1080, "/index.html?dash=1&at=14:00", {}, 0),
        ("dash-portrait", 1080, 1920, "/index.html?dash=1&at=14:00", {"layout": "compact"}, 0),
    )
    with sync_playwright() as p:
        br = p.chromium.launch(executable_path="/usr/bin/chromium")
        for tag, vw, vh, url, disp, fi in cases:
            print(f"--- {tag}")
            res = {}
            for ts in (None, 1.0, 0.8, 0.6):
                d = dict(disp) if ts is None else {**disp, "text_scale": ts}
                pg, errors = open_page(br, vw, vh, url, d, {"id": 0, "clear": True} if "dash=1" in url else card,
                                       without_ts=ts is None)
                fr = pg.frames[fi]
                if not url.startswith("/index.html?dash"):
                    fr.evaluate("m => window.__push(m)", {"t": "config", "captions": True})
                    fr.evaluate("m => window.__push(m)", {"t": "caption", "who": "walter",
                                                          "text": "Ooh, rain after lunch, so maybe do the fence gate this morning."})
                    pg.wait_for_timeout(800)
                res[ts] = {"fonts": fr.evaluate(FONTS), "geom": fr.evaluate(GEOM)}
                check(not errors, f"ts={ts}: no page errors {errors[:2]}")
                if SHOTS and ts is not None:
                    pg.screenshot(path=str(SHOTS / f"{tag}-ts{ts}.png"))
                if ts == 1.0 and tag in ("landscape", "rot90"):
                    # live change, no reload
                    disp_live = {**disp, "text_scale": 0.6}
                    pg.unroute("**/display.json*")
                    pg.route("**/display.json*", lambda r: r.fulfill(status=200, content_type="application/json",
                                                                     body=json.dumps(disp_live)))
                    before = fr.evaluate("parseFloat(getComputedStyle(document.querySelector('#clock')).fontSize)")
                    pg.wait_for_timeout(2600)
                    after = fr.evaluate("parseFloat(getComputedStyle(document.querySelector('#clock')).fontSize)")
                    check(abs(after - before * 0.6) < 0.6, f"live change 1 -> 0.6: clock {before:.1f} -> {after:.1f}px")
                pg.close()

            base, one = res[None], res[1.0]
            common = [k for k in base["fonts"] if k in one["fonts"]]
            diff = [k for k in common if abs(base["fonts"][k]["fs"] - one["fonts"][k]["fs"]) > 0.01]
            check(len(common) > 10 and not diff, f"scale 1 = original sizes ({len(common)} text elements; diffs {diff[:3]})")
            check(one["geom"]["ts"] == 1.0, "scale 1 read from display.json")
            for ts in (0.8, 0.6):
                r = res[ts]
                check(r["geom"]["ts"] == ts, f"ts={ts}: read from display.json")
                ks = [k for k in common if k in r["fonts"] and base["fonts"][k]["fs"] > 4]
                bad = [(k.split(">")[-1], base["fonts"][k]["fs"], r["fonts"][k]["fs"]) for k in ks if r["fonts"][k]["shown"]
                       if abs(r["fonts"][k]["fs"] - base["fonts"][k]["fs"] * ts) > 0.6]
                check(len(ks) > 10 and not bad, f"ts={ts}: all {len(ks)} text elements scaled {bad[:3]}")
                g0, g = base["geom"], r["geom"]
                check(abs(g["R"] - g0["R"]) <= 2, f"ts={ts}: my size unchanged ({g0['R']} -> {g['R']}px)")
                check(g["cardImg"] == g0["cardImg"] and g["dashQr"] == g0["dashQr"],
                      f"ts={ts}: QR codes unchanged ({g['cardImg']}, {g['dashQr']})")
                check(g["canvas"] == g0["canvas"], f"ts={ts}: panels unchanged")
                newclip = [k.split(">")[-1] for k in ks if r["fonts"][k]["clip"] and r["fonts"][k]["shown"]
                           and not base["fonts"][k]["clip"]]
                check(not newclip, f"ts={ts}: no newly clipped text {newclip[:3]}")
        br.close()
    srv.shutdown()
    print("PASS" if not fails else f"FAIL ({len(fails)})")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
