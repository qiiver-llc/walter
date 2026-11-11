#!/usr/bin/env python3
"""One-shot phone page for the owner to hand Walter a Google OAuth client privately.

Usage: oauth-drop.py [TARGET_FILE] [PORT]
  TARGET_FILE  where a Desktop-app client goes (default ~/.walter/secrets/google-oauth-client.json)
  PORT         default 8769

Accepts both client types and keeps them apart:
  Desktop app      -> TARGET_FILE (the MCP's own client; wall-browser sign-in via authorize.py)
  Web application  -> ~/.walter/secrets/google-oauth-web-client.json (phone sign-in via
                      phone-connect.py / `walterctl google connect`; redirect URI:
                      <[app] public_url>/app/oauth/google.html)

Serves http://<lan-ip>:PORT/<random>/ for 30 minutes and shows a QR code on the face.
The owner either picks the client_secret_....json he downloaded from Google Cloud, or pastes the
Client ID and Client secret. The page checks the shape, makes one free check against
Google's token endpoint (a dummy code: "invalid_grant" means the client is real,
"invalid_client" means it is not), saves it mode 600 in a mode-700 folder, then exits.
The secret is never printed, logged, spoken or shown. Modelled on skills/api-key-drop.py.
"""
import html, http.server, json, os, re, secrets, socket, subprocess, sys, threading
import urllib.error, urllib.parse, urllib.request

ID_RE = re.compile(r"^[0-9]{6,20}-[a-z0-9]{10,64}\.apps\.googleusercontent\.com$")
SECRET_RE = re.compile(r"^[A-Za-z0-9_\-]{20,80}$")
TOKEN_URI = os.environ.get("GOOGLE_TOKEN_URI", "https://oauth2.googleapis.com/token")  # tests can fake it
SHOW = os.path.expanduser("~/walter/bin/walter-show")
WEB_TARGET = os.environ.get("GOOGLE_MCP_WEB_CLIENT_FILE", "~/.walter/secrets/google-oauth-web-client.json")
WALTERCTL = os.path.expanduser("~/walter/bin/walterctl")


def parse(form: dict) -> tuple[str, str, str, str]:
    """(client_id, client_secret, kind "web"|"installed", problem). Pasted JSON or the two fields
    (then the page's "kind" choice says which type it is; default web)."""
    raw = (form.get("json") or [""])[0].strip()
    cid = (form.get("id") or [""])[0].strip()
    sec = (form.get("secret") or [""])[0].strip()
    kind = "installed" if (form.get("kind") or [""])[0] == "installed" else "web"
    if raw:
        try:
            d = json.loads(raw)
        except ValueError:
            return "", "", "", "That file isn't valid JSON. Pick the client_secret file you downloaded."
        if not isinstance(d, dict):
            return "", "", "", "That file isn't a Google client file."
        kind = "web" if "web" in d and "installed" not in d else "installed"
        d = d.get(kind) or {}
        cid, sec = str(d.get("client_id", "")).strip(), str(d.get("client_secret", "")).strip()
    if not ID_RE.match(cid):
        return "", "", "", "The Client ID should end in .apps.googleusercontent.com."
    if not SECRET_RE.match(sec):
        return "", "", "", "That Client secret doesn't look right (it usually starts with GOCSPX-)."
    return cid, sec, kind, ""


def validate(cid: str, sec: str, timeout: float = 10.0) -> tuple[bool, str]:
    """One free request with a dummy code. Never echoes the secret."""
    body = urllib.parse.urlencode({"grant_type": "authorization_code", "code": "walter-check",
                                   "redirect_uri": "http://localhost", "client_id": cid,
                                   "client_secret": sec}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URI, data=body), timeout=timeout) as r:
            r.read(1)
        return True, "ok"
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read(2000) or b"{}").get("error", "")
        except ValueError:
            err = ""
        if err == "invalid_client" or e.code == 401:
            return False, "Google didn't recognise that client ID and secret."
        if err in ("invalid_grant", "invalid_request", "redirect_uri_mismatch"):
            return True, "ok"
        return False, f"Google answered with an error ({e.code}). Try again in a minute."
    except Exception:
        return False, "Couldn't reach Google to check it."


def public_redirect() -> str:
    """The Web client's redirect URI: [app] public_url + /app/oauth/google.html ("" if unset)."""
    try:
        import tomllib
        with open(os.path.expanduser("~/.walter/config.toml"), "rb") as f:
            pub = str(tomllib.load(f).get("app", {}).get("public_url") or "").rstrip("/")
    except Exception:
        pub = ""
    return pub + "/app/oauth/google.html" if pub.startswith("https://") else ""


def save(path: str, cid: str, sec: str, kind: str = "installed"):
    path = os.path.expanduser(path)
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    redirects = ["http://localhost"] if kind == "installed" else [u for u in [public_redirect()] if u]
    data = {kind: {"client_id": cid, "client_secret": sec,
                   "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                   "token_uri": "https://oauth2.googleapis.com/token",
                   "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                   "redirect_uris": redirects}}
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.connect(("1.1.1.1", 80)); return s.getsockname()[0]
    finally: s.close()


PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Walter - Google client</title><style>body{font-family:system-ui;background:#05060a;color:#e8eefc;padding:22px;max-width:520px;margin:auto}
button,label.b{display:block;text-align:center;background:#58c4ff;color:#05060a;padding:14px;border-radius:12px;font-weight:600;border:0;width:100%;font-size:17px;box-sizing:border-box}
input[type=text],input[type=password]{width:100%;box-sizing:border-box;margin:8px 0;border-radius:12px;background:#0d111b;color:#fff;border:1px solid #2a3550;padding:12px;font-size:15px}
p{color:#9fb0d0;line-height:1.45}#f{display:none}#ok{color:#7fe0a0}</style>
<h2>Give Walter your Google OAuth client</h2>
<p>This goes straight into a private file on Walter, never into the conversation, and is never shown.</p>
<form method=post id=form><input type=hidden name=json id=json>
<p><b>Option 1:</b> pick the <i>client_secret_....json</i> file you downloaded.</p>
<label class=b for=f>Choose the downloaded file</label><input type=file id=f accept=".json,application/json">
<p id=ok></p>
<p><b>Option 2:</b> paste the two values instead.</p>
<p><label><input type=radio name=kind value=web checked> Web application</label> &nbsp;
<label><input type=radio name=kind value=installed> Desktop app</label></p>
<input type=text name=id placeholder="Client ID (...apps.googleusercontent.com)" autocomplete=off autocapitalize=off spellcheck=false>
<input type=password name=secret placeholder="Client secret (GOCSPX-...)" autocomplete=off autocapitalize=off spellcheck=false>
<p></p><button>Send to Walter</button></form>
<script>document.getElementById('f').onchange=e=>{const r=new FileReader();r.onload=()=>{document.getElementById('json').value=r.result;
document.getElementById('ok').textContent='File loaded. Tap Send.'};r.readAsText(e.target.files[0])}</script>"""


def msg(text, code=200):
    return code, "<p style='font-family:system-ui;font-size:19px;padding:20px'>%s</p>" % html.escape(text)


def main(argv):
    target = argv[0] if argv else "~/.walter/secrets/google-oauth-client.json"
    port = int(argv[1]) if len(argv) > 1 else 8769
    token = secrets.token_urlsafe(9)

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a): pass

        def reply(self, code, body):
            b = body.encode(); self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)

        def do_GET(self):
            if self.path.rstrip("/") != "/" + token: return self.reply(404, "not found")
            self.reply(200, PAGE)

        def do_POST(self):
            if self.path.rstrip("/") != "/" + token: return self.reply(404, "not found")
            n = min(int(self.headers.get("Content-Length", 0) or 0), 16384)
            form = urllib.parse.parse_qs(self.rfile.read(n).decode(errors="replace"))
            cid, sec, kind, problem = parse(form)
            form = None
            if problem:
                return self.reply(*msg(problem + " Go back and try again.", 400))
            ok, why = validate(cid, sec)
            if not ok:
                return self.reply(*msg(why + " Go back and try again.", 400))
            save(WEB_TARGET if kind == "web" else target, cid, sec, kind)
            sec = None
            subprocess.run([SHOW, "--clear"], capture_output=True)
            if kind == "web":
                self.reply(*msg("Done. Walter has the Web client. Next he'll put a QR on the wall: scan it, "
                                "pick an account and tap Allow. You can close this page."))
                note = ("Google Web-application OAuth client received via the private phone page, checked with "
                        "Google and saved (~/.walter/secrets/google-oauth-web-client.json). Next: run "
                        "`walterctl google connect` (QR on the wall; the owner approves each account on his phone, "
                        "where he's already signed in). Tell the owner briefly.")
            else:
                self.reply(*msg("Done. Walter has the Google client. Next, he'll open a Google sign-in "
                                "page on the wall for each account. You can close this page."))
                note = ("Google OAuth client (Desktop app) received via the private phone page, checked with "
                        "Google and saved. Next: authorise each account with ~/walter/tools/google-mcp/authorize.py "
                        "EMAIL (opens Google's consent page in the wall browser; the owner signs in himself), then "
                        "walterctl new-session so the google-accounts tools load. Tell the owner briefly.")
            subprocess.run([WALTERCTL, "ask", "[system note, not your person speaking] " + note], capture_output=True)
            threading.Thread(target=self.server.shutdown).start()

    url = "http://%s:%d/%s/" % (ip(), port, token)
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), H)
    subprocess.run([SHOW, "--qr", url, "Google client for Walter", "Scan with your phone", "1800"])
    threading.Timer(1800, srv.shutdown).start()
    srv.serve_forever()
    subprocess.run([SHOW, "--clear"], capture_output=True)


if __name__ == "__main__":
    main(sys.argv[1:])
