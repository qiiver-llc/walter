"""One-shot private phone page for an add-on secret (walterctl addon secret ID KEY; the app's "Set secret").

  python -m walter.addon_secret_drop ID KEY [--no-qr]

Prints the page's address as its FIRST line on stdout (the caller reads it), shows a QR on the wall,
serves http://<lan-ip>:PORT/<random>/ until one secret arrives or the time runs out, saves it to
~/.walter/addons-secrets/<id>/<key> (mode 600, folder 700) and tells Walter with a system note. The
value is never printed, logged, spoken or shown. Same pattern as the API-key drop pages.
"""
from __future__ import annotations

import html
import http.server
import os
import secrets
import socket
import subprocess
import sys
import threading
import urllib.parse
from pathlib import Path

from . import addons as A

REPO = Path(__file__).resolve().parent.parent
SHOW = str(REPO / "bin" / "walter-show")
WALTERCTL = str(REPO / "bin" / "walterctl")
PORTS = (8768, 8769, 8770, 0)

PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Walter · %(addon)s</title><style>body{font-family:system-ui;background:#05060a;color:#e8eefc;padding:22px;max-width:520px;margin:auto}
button{display:block;text-align:center;background:#58c4ff;color:#05060a;padding:14px;border-radius:12px;font-weight:600;border:0;width:100%%;font-size:17px}
input{width:100%%;box-sizing:border-box;margin:12px 0;border-radius:12px;background:#0d111b;color:#fff;border:1px solid #2a3550;padding:12px;font-size:15px}
p{color:#9fb0d0;line-height:1.45}</style>
<h2>%(label)s for %(addon)s</h2>
<p>%(help)s</p>
<p>Paste it here and send. It goes straight into a private file on Walter, not into the conversation, and is never shown.</p>
<form method=post><input type=password name=k autocomplete=off autocapitalize=off spellcheck=false><button>Send to Walter</button></form>"""


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def msg(text: str) -> str:
    return "<p style='font-family:system-ui;font-size:19px;padding:20px'>%s</p>" % html.escape(text)


def make_server(store: A.Addons, aid: str, key: str, ports=PORTS, on_saved=None):
    spec = store.secret_spec(aid, key)
    token = secrets.token_urlsafe(12)
    done = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, code, body):
            b = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path.rstrip("/") != "/" + token or done.is_set():
                return self.reply(404, "not found")
            self.reply(200, PAGE % {"addon": html.escape(spec["addon"]), "label": html.escape(spec["label"]),
                                    "help": html.escape(spec.get("help") or "Copy it from the service it belongs to.")})

        def do_POST(self):
            if self.path.rstrip("/") != "/" + token or done.is_set():
                return self.reply(404, "not found")
            n = min(int(self.headers.get("Content-Length", 0) or 0), A.SECRET_CAP * 3 + 16)
            val = (urllib.parse.parse_qs(self.rfile.read(n).decode(errors="replace")).get("k") or [""])[0]
            try:
                store.save_secret(aid, key, val)
            except A.AddonError as e:
                return self.reply(400, msg(f"{e}. Go back and try again."))
            val = None
            done.set()
            self.reply(200, msg(f"Done. Walter has the {spec['label']} for {spec['addon']}. You can close this page."))
            if on_saved:
                on_saved()
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    last = None
    for port in ports:
        try:
            srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), H)
            break
        except OSError as e:
            last = e
    else:
        raise last or OSError("no port")
    url = "http://%s:%d/%s/" % (lan_ip(), srv.server_address[1], token)
    return srv, url, spec, done


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    qr = "--no-qr" not in argv
    minutes = 15
    if len(args) < 2:
        print("usage: python -m walter.addon_secret_drop ID KEY [--no-qr]", file=sys.stderr)
        return 2
    aid, key = args[0], args[1]
    store = A.Addons()

    def saved():
        subprocess.run([SHOW, "--clear"], capture_output=True)
        subprocess.run([WALTERCTL, "ask", f"[system note, not the owner speaking] The {spec['label']} for the "
                        f"{spec['addon']} add-on arrived through the private phone page and is saved. "
                        "Tell them briefly."], capture_output=True)

    try:
        srv, url, spec, _ = make_server(store, aid, key, on_saved=saved)
    except (A.AddonError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(url, flush=True)
    try:
        sys.stdout.close()
    except OSError:
        pass
    if qr:
        subprocess.run([SHOW, "--qr", url, f"{spec['label']} for {spec['addon']}", "Scan with your phone",
                        str(minutes * 60)], capture_output=True)
    t = threading.Timer(minutes * 60, srv.shutdown)
    t.daemon = True
    t.start()
    srv.serve_forever()
    if qr:
        subprocess.run([SHOW, "--clear"], capture_output=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
