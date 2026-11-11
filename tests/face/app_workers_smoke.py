"""Headless test of the companion app's "Workers at once" stepper (Walter > Settings > Workers).
Run with the browser venv's python:
~/.walter/browser-venv/bin/python tests/face/app_workers_smoke.py [shot.png]

Serves app/ over a throwaway local HTTP server and fakes /api/app/* in the page, then checks:
  - opening Settings shows the stored value, running/queued and the CPU hint;
  - three quick taps on + send ONE POST (debounced) with the final value;
  - minus stops at 1 (button disabled);
  - a Walter that predates the route (404) shows "Needs one restart to enable", buttons disabled.
Prints PASS/FAIL."""
import json
import sys

from playwright.sync_api import sync_playwright

from app_textsize_smoke import serve


def run(have_route: bool):
    posts = []
    state = {"max_tasks": 2, "min": 1, "max": 8, "running": 1, "queued": 2, "threads": 4, "load": 1.234}
    srv = serve()
    base = f"http://localhost:{srv.server_address[1]}"
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        page = b.new_page(viewport={"width": 390, "height": 844}, has_touch=True)
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.add_init_script("navigator.serviceWorker && (navigator.serviceWorker.register = () => new Promise(() => {}));"
                             "window.WebSocket = function () { return { close() {}, send() {}, readyState: 3 }; };")

        def api(route):
            req = route.request
            if "/api/app/workers" in req.url:
                if not have_route:
                    return route.fulfill(status=404, body="not found")
                if req.method == "POST":
                    n = json.loads(req.post_data)["max_tasks"]
                    posts.append(n)
                    state["max_tasks"] = n
                return route.fulfill(json=state)
            route.fulfill(json={})
        page.route("**/api/app/**", api)
        page.goto(base + "/index.html")
        page.evaluate("document.querySelector('[data-view=walter]').click()")
        page.click("[data-go=settings]")
        page.wait_for_timeout(500)
        out = {"val": page.text_content("#wkVal"), "now": page.text_content("#wkNow"),
               "line": page.text_content("#wkLine"), "up_disabled": page.locator("#wkUp").is_disabled(),
               "errors": errors}
        if have_route:
            page.locator("#wkUp").scroll_into_view_if_needed()
            for _ in range(3):
                page.click("#wkUp")
            out["posts_during"] = list(posts)
            out["val_after_taps"] = page.text_content("#wkVal")
            page.wait_for_timeout(1000)
            out["posts_after"] = list(posts)
            for _ in range(8):
                if page.locator("#wkDown").is_disabled():
                    break
                page.click("#wkDown")
            out["min_val"], out["down_disabled"] = page.text_content("#wkVal"), page.locator("#wkDown").is_disabled()
            page.wait_for_timeout(1000)
            out["posts_end"] = list(posts)
            if len(sys.argv) > 1:
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
    # other panels get {} from the fake API and may throw; only our code matters here
    check(f"no stepper errors {a['errors']}", not [e for e in a["errors"] if "wk" in e.lower()])
    check(f"shows the stored value on open ({a['val']})", a["val"] == "2" and not a["up_disabled"])
    check(f"running/queued ({a['now']})", a["now"] == "1 running, 2 queued.")
    check(f"CPU hint ({a['line']})", a["line"] == "More workers use more CPU: this computer has 4 threads, load now 1.2.")
    check(f"taps update the value, nothing sent yet ({a['val_after_taps']}, {a['posts_during']})",
          a["val_after_taps"] == "5" and a["posts_during"] == [])
    check(f"one debounced POST ({a['posts_after']})", a["posts_after"] == [5])
    check(f"minus stops at 1 ({a['min_val']})", a["min_val"] == "1" and a["down_disabled"])
    check(f"then one POST of 1 ({a['posts_end']})", a["posts_end"] == [5, 1])
    o = run(False)
    check(f"old Walter: needs one restart ({o['val']})",
          o["val"] == "Needs one restart to enable" and o["up_disabled"])
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
