"""Headless test of the companion app's "Wall text size" slider (Walter > Settings > Wall).
Run with the browser venv's python:
~/.walter/browser-venv/bin/python tests/face/app_textsize_smoke.py [shot.png]

Serves app/ over a throwaway local HTTP server and fakes /api/app/* in the page, then checks:
  - opening Settings shows the stored value (GET /api/app/display -> 125%);
  - dragging updates the label live but sends nothing until the finger lifts, then ONE POST;
  - Reset sends the default (80%);
  - a Walter that predates the route (404) shows "Needs one restart to enable", slider disabled.
Prints PASS/FAIL."""
import functools
import http.server
import json
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

APP = Path(__file__).resolve().parents[2] / "app"


def serve():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    h = functools.partial(Quiet, directory=str(APP))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), h)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def run(have_route: bool):
    posts, state = [], {"text_scale": 1.25}
    srv = serve()
    base = f"http://localhost:{srv.server_address[1]}"
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        page = b.new_page(viewport={"width": 390, "height": 844}, has_touch=True)
        page.add_init_script("navigator.serviceWorker && (navigator.serviceWorker.register = () => new Promise(() => {}));"
                             "window.WebSocket = function () { return { close() {}, send() {}, readyState: 3 }; };")

        def api(route):
            req = route.request
            if "/api/app/display" in req.url:
                if not have_route:
                    return route.fulfill(status=404, body="not found")
                if req.method == "POST":
                    v = json.loads(req.post_data)["text_scale"]
                    posts.append(v)
                    state["text_scale"] = round(v, 2)
                return route.fulfill(json={**state, "default": 0.8, "min": 0.5, "max": 1.5})
            route.fulfill(json={})
        page.route("**/api/app/**", api)
        page.goto(base + "/index.html")
        page.evaluate("document.querySelector('[data-view=walter]').click()")
        page.click("[data-go=settings]")
        page.wait_for_timeout(500)
        r = page.locator("#tsRange")
        out = {"label": page.text_content("#tsVal"), "disabled": r.is_disabled(),
               "line": page.text_content("#tsLine")}
        if have_route:
            # drag: input events only (label follows), nothing sent yet
            page.evaluate("(() => { const r = document.getElementById('tsRange');"
                          " for (const v of [120, 110, 100]) { r.value = v; r.dispatchEvent(new Event('input')); } })()")
            page.wait_for_timeout(600)
            out["drag_label"], out["posts_during_drag"] = page.text_content("#tsVal"), list(posts)
            page.evaluate("document.getElementById('tsRange').dispatchEvent(new Event('change'))")   # finger lifts
            page.wait_for_timeout(700)
            out["posts_after_lift"] = list(posts)
            page.click("#tsReset")
            page.wait_for_timeout(700)
            out["posts_after_reset"], out["reset_label"] = list(posts), page.text_content("#tsVal")
            out["reset_text"] = page.text_content("#tsReset")
            if len(sys.argv) > 1:
                page.locator("#tsRange").scroll_into_view_if_needed()
                page.screenshot(path=sys.argv[1])
        b.close()
    srv.shutdown()
    return out


def main():
    ok = True

    def check(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(("ok   " if cond else "FAIL ") + name)
    a = run(True)
    check(f"shows the stored value on open ({a['label']})", a["label"] == "125%" and not a["disabled"])
    check(f"label follows the drag ({a['drag_label']})", a["drag_label"] == "100%")
    check("nothing sent while dragging", a["posts_during_drag"] == [])
    check(f"one POST when the finger lifts ({a['posts_after_lift']})", a["posts_after_lift"] == [1.0])
    check(f"Reset sends the default ({a['posts_after_reset']})",
          a["posts_after_reset"] == [1.0, 0.8] and a["reset_label"] == "80%" and a["reset_text"] == "Reset to 80%")
    o = run(False)
    check(f"old Walter: needs one restart ({o['label']})", o["label"] == "Needs one restart to enable" and o["disabled"])
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
