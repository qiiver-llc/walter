"""Phone typing page for the wall browser (`walter-browse typing-page`).

The person can't type on the wall, so this serves a one-shot page on the LAN at an
unguessable address (QR code on the face) for 10 minutes. On the phone: a text box
("Type into the page"), a masked password box, a code box (2FA) and Enter / Tab /
Next field / Done buttons. Each send goes over the live browser's private unix socket
and is typed into the focused box with real keyboard events.

Privacy: the text is never logged, printed, stored, spoken or shown. The HTTP server's
request log is off, the reply only says how many characters went in and which box
(by label), and passwords are only typed into a real password box (masked on the wall).
Passwords typed here are the person's own action, so they don't need Walter's --yes;
this page is the only way to do that.

Only the first device that sends something can keep using the page (a cookie binds it).
"Done" (or the 10 minutes running out) stops the server and clears the QR card.
"""
import http.server, json, os, secrets, signal, socket, subprocess, sys, threading, time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browse_live as live  # noqa: E402

WALTER = Path(os.environ.get("WALTER_DIR", Path.home() / ".walter"))
STATE = WALTER / "typepad.json"
SHOW = os.environ.get("WALTER_SHOW_BIN", os.path.expanduser("~/walter/bin/walter-show"))
WALTERCTL = os.environ.get("WALTER_CTL_BIN", os.path.expanduser("~/walter/bin/walterctl"))
PORT = int(os.environ.get("WALTER_TYPEPAD_PORT", "8772"))
MINUTES = 10
MAX_BODY = 4096


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def browser(cmd: dict, timeout: float = 60) -> dict:
    """Talk to the live browser; errors come back as a plain message (never the text)."""
    try:
        return live.send(cmd, timeout=timeout)
    except Exception:
        return {"ok": False, "error": "the wall browser isn't open; ask Walter to open the page first"}


def where(res: dict) -> dict:
    """Only what's safe to show on the phone: page title, host/path, focused box label."""
    return {"ok": bool(res.get("ok")), "msg": res.get("msg") or res.get("error") or "",
            "title": (res.get("title") or "")[:100], "url": live.display_url(res.get("url", "")),
            "field": res.get("field", "")}


def handle_action(body: dict, send=browser) -> dict:
    """Validate one phone action and pass it to the browser. Pure enough to test."""
    act = body.get("action")
    if act == "type":
        kind = body.get("kind") if body.get("kind") in ("text", "password", "code") else "text"
        text = body.get("text")
        if not isinstance(text, str) or not text:
            return {"ok": False, "msg": "Nothing to type."}
        if len(text) > 500:
            return {"ok": False, "msg": "That's too long."}
        if kind == "code":
            text = text.strip()
        res = send({"cmd": "pad", "args": {"action": "type", "kind": kind, "text": text,
                                           "enter": bool(body.get("enter"))}})
        return where(res)
    if act == "key":
        if body.get("key") not in live.PAD_KEYS:
            return {"ok": False, "msg": "Unknown key."}
        return where(send({"cmd": "pad", "args": {"action": "key", "key": body["key"]}}))
    if act == "peek":
        return where(send({"cmd": "pad", "args": {"action": "peek"}}, timeout=10))
    return {"ok": False, "msg": "Unknown action."}


PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta name=referrer content=no-referrer>
<title>Type for Walter</title><style>
body{font-family:system-ui;background:#05060a;color:#e8eefc;padding:16px 18px 40px;max-width:520px;margin:auto}
h2{margin:6px 0 2px}#where{color:#9fb0d0;font-size:14px;min-height:38px;line-height:1.35}
#field{color:#58c4ff}label{display:block;margin:18px 0 4px;color:#9fb0d0;font-size:14px}
input{width:100%;box-sizing:border-box;border-radius:12px;background:#0d111b;color:#fff;border:1px solid #2a3550;
padding:13px;font-size:17px}.row{display:flex;gap:8px;margin-top:8px}
button{flex:1;background:#58c4ff;color:#05060a;padding:13px 6px;border-radius:12px;font-weight:600;border:0;font-size:16px}
button.k{background:#1b2436;color:#e8eefc}button.done{background:#8affc1;margin-top:26px;width:100%}
.chk{display:flex;align-items:center;gap:6px;margin-top:6px;color:#9fb0d0;font-size:14px}
.chk input{width:auto}#msg{margin-top:14px;min-height:22px;font-size:15px}#msg.bad{color:#ff9b8a}
#left{color:#6c7a96;font-size:13px;margin-top:10px}</style></head><body>
<h2>Type for Walter</h2>
<div id=where>Wall browser: <span id=title>...</span><br>Selected: <span id=field>...</span></div>
<label for=t>Text (email address, search...)</label>
<input id=t autocomplete=off autocapitalize=off autocorrect=off spellcheck=false>
<div class=row><button data-kind=text data-in=t>Type into the page</button></div>
<label for=p>Password (hidden, goes only into a password box)</label>
<input id=p type=password autocomplete=off autocapitalize=off spellcheck=false>
<div class=row><button data-kind=password data-in=p>Type password</button></div>
<label for=c>Code (2-step verification)</label>
<input id=c inputmode=numeric autocomplete=one-time-code autocapitalize=off spellcheck=false>
<div class=row><button data-kind=code data-in=c>Type code</button></div>
<div class=chk><input type=checkbox id=enter checked><label for=enter style="margin:0">Press Enter after typing</label></div>
<div class=row><button class=k data-key="Enter">Enter</button><button class=k data-key="Tab">Tab</button>
<button class=k data-key="Next field">Next field</button></div>
<div id=msg></div>
<button class=done id=done>Done</button>
<div id=left></div>
<script>
const base = location.pathname.replace(/\\/$/, '') + '/api';
const $ = id => document.getElementById(id);
let busy = false, ends = __ENDS__ * 1000;
async function call(body) {
  const r = await fetch(base, {method: 'POST', headers: {'Content-Type': 'application/json'},
                               body: JSON.stringify(body), credentials: 'same-origin'});
  return r.json();
}
function show(r) {
  if (r.title !== undefined && r.title !== '') $('title').textContent = r.title + (r.url ? ' (' + r.url + ')' : '');
  if (r.field) $('field').textContent = r.field;
  if (r.msg !== undefined && !r.quiet) { $('msg').textContent = r.msg; $('msg').className = r.ok ? '' : 'bad'; }
  if (r.closed) end(r.msg || 'Closed.');
}
async function act(body) {
  if (busy) return; busy = true; $('msg').textContent = '...'; $('msg').className = '';
  try { show(await call(body)); } catch (e) { show({ok: false, msg: 'Lost touch with Walter.'}); }
  busy = false;
}
document.querySelectorAll('button[data-kind]').forEach(b => b.onclick = () => {
  const box = $(b.dataset.in), text = box.value;
  if (!text) { box.focus(); return; }
  box.value = '';                          // never left lying around on the phone either
  act({action: 'type', kind: b.dataset.kind, text, enter: $('enter').checked});
});
document.querySelectorAll('button[data-key]').forEach(b => b.onclick = () => act({action: 'key', key: b.dataset.key}));
function end(m) {
  document.body.innerHTML = '<h2>Type for Walter</h2><p style="color:#9fb0d0;font-size:18px"></p>';
  document.querySelector('p').textContent = m; clearInterval(timer);
}
$('done').onclick = async () => { try { await call({action: 'done'}); } catch (e) {} end('All done. You can close this page.'); };
async function peek() {
  if (document.hidden || busy) return;
  try { const r = await call({action: 'peek'}); r.quiet = true; show(r); } catch (e) {}
}
const timer = setInterval(() => {
  const s = Math.max(0, Math.round((ends - Date.now()) / 1000));
  $('left').textContent = 'This page closes in ' + Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  if (!s) end('This page has expired. Ask Walter for a new one.');
  else if (s % 4 === 0) peek();
}, 1000);
peek();
</script></body></html>"""


def notify(text: str):
    subprocess.run([WALTERCTL, "ask", "[system note, not your person speaking] " + text],
                   capture_output=True, timeout=30)


def serve(minutes: int = MINUTES, port: int = PORT, show_qr: bool = True, on_ready=None):
    secret = secrets.token_urlsafe(16)
    cookie_name = "wpad"
    owner = {"cookie": None}
    ends = time.time() + minutes * 60
    done = threading.Event()
    seen = {"opened": False}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):          # no request log at all
            pass

        def reply(self, code, body, ctype="text/html; charset=utf-8", extra=()):
            b = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                             "connect-src 'self'; form-action 'none'; frame-ancestors 'none'")
            for k, v in extra:
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def ok_path(self, suffix=""):
            return secrets.compare_digest(self.path.split("?")[0].rstrip("/"), f"/{secret}{suffix}")

        def do_GET(self):
            if not self.ok_path() or time.time() > ends:
                return self.reply(404, "not found")
            if not seen["opened"]:
                seen["opened"] = True
                if show_qr:                     # out of the way of the wall browser
                    subprocess.run([SHOW, "--clear"], capture_output=True)
            self.reply(200, PAGE.replace("__ENDS__", str(int(ends))))

        def do_POST(self):
            if not self.ok_path("/api") or time.time() > ends:
                return self.reply(404, "not found")
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self.reply(415, "json only")
            mine = ""
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == cookie_name:
                    mine = v
            extra = ()
            if owner["cookie"] is None:          # the first device to act owns the page
                owner["cookie"] = secrets.token_urlsafe(16)
                mine = owner["cookie"]
                extra = (("Set-Cookie", f"{cookie_name}={mine}; Path=/{secret}; HttpOnly; SameSite=Strict"),)
            if not secrets.compare_digest(mine, owner["cookie"]):
                return self.reply(403, json.dumps({"ok": False, "msg": "This page is in use on another device."}),
                                  "application/json")
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self.reply(413, json.dumps({"ok": False, "msg": "Too long."}), "application/json")
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8"))
                assert isinstance(body, dict)
            except Exception:
                return self.reply(400, json.dumps({"ok": False, "msg": "Bad request."}), "application/json")
            if body.get("action") == "done":
                done.set()
                self.reply(200, json.dumps({"ok": True, "closed": True, "msg": "All done."}), "application/json", extra)
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            res = handle_action(body)
            body = None
            self.reply(200, json.dumps(res), "application/json", extra)

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), H)
    url = f"http://{lan_ip()}:{srv.server_address[1]}/{secret}/"
    if show_qr:
        subprocess.run([SHOW, "--qr", url, "Type for Walter",
                        "Scan with your phone to type into the wall browser", str(minutes * 60)],
                       capture_output=True)
    if on_ready:
        on_ready(url, srv)
    timer = threading.Timer(max(1, ends - time.time()), srv.shutdown)
    timer.daemon = True
    timer.start()
    try:
        srv.serve_forever()
    finally:
        timer.cancel()
        srv.server_close()
    return done.is_set()


def main():
    minutes = int(sys.argv[1]) if len(sys.argv) > 1 else MINUTES
    minutes = max(1, min(minutes, 30))
    os.umask(0o077)

    def ready(url, srv):
        WALTER.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps({"pid": os.getpid(), "port": srv.server_address[1],
                                     "until": int(time.time() + minutes * 60)}))

    def stop(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        finished = serve(minutes, on_ready=ready)
    except KeyboardInterrupt:              # walter-browse typing-page --stop
        finished = None
    finally:
        try:
            STATE.unlink()
        except FileNotFoundError:
            pass
        subprocess.run([SHOW, "--clear"], capture_output=True)
    if finished is None:
        return
    st = browser({"cmd": "status"}, timeout=10)
    page = where(st)
    on = f" The wall browser is on: {page['title'] or page['url']}." if st.get("ok") else ""
    if finished:
        notify("The owner pressed Done on the phone typing page; it's closed." + on +
               " Carry on with what he wanted (don't read his mail unless he asked).")
    else:
        notify("The phone typing page expired after %d minutes without Done; it's closed.%s "
               "Mention it only if relevant." % (minutes, on))


if __name__ == "__main__":
    main()
