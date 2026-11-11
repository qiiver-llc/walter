#!/usr/bin/env python3
"""Connect a Google account to Walter's Google MCP from the owner's PHONE (QR on the wall, no wall browser).

Usage: ~/.walter/google-mcp/venv/bin/python ~/walter/tools/google-mcp/phone-connect.py [--minutes N] [--no-show]
       (or simply: walterctl google connect)

How it works
  1. Shows a QR on Walter's face. It opens a small page served by this script on the home
     network (http://<lan-ip>:8772/<random>/). The owner taps "Connect a Google account".
  2. His phone goes to Google's consent page, where he is already signed in: he picks the
     account and taps Allow. No passwords are typed anywhere near Walter.
  3. Google redirects to the public app address (config [app] public_url +
     /app/oauth/google.html, a static relay page; no server change). That page forwards the
     code to this script on the home network (the address travels inside the OAuth state).
  4. This script swaps the code for tokens (Web client secret + PKCE verifier; the code alone
     is useless to anyone else), takes the account's email from Google's own answer, writes
     ~/.walter/google-mcp/credentials/<email>.json in exactly workspace-mcp's format, then
     proves it with one read-only Gmail search through the MCP server.
  The owner can connect several accounts in one go ("Connect another account"); the script stops
  when he taps "I'm done" or after --minutes (default 20). Walter gets a system note.

Needs the Web-application OAuth client in ~/.walter/secrets/google-oauth-web-client.json
(dropped with tools/google-mcp/oauth-drop.py; setup steps: journal/2026-11-05-google-phone-connect.md).
The Desktop client (google-oauth-client.json) stays as the MCP's own client and backup.
Nothing secret is printed, logged or shown; no email address is needed on the command line.
"""
import asyncio, base64, datetime, hashlib, html, http.server, json, os, re, secrets, socket
import subprocess, sys, threading, time, tomllib, urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
W = os.path.expanduser("~/.walter")
WEB_CLIENT = os.environ.get("GOOGLE_MCP_WEB_CLIENT_FILE", os.path.join(W, "secrets/google-oauth-web-client.json"))
CREDS = os.environ.get("WORKSPACE_MCP_CREDENTIALS_DIR", os.path.join(W, "google-mcp/credentials"))
CONFIG = os.environ.get("WALTER_CONFIG", os.path.join(W, "config.toml"))
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = os.environ.get("GOOGLE_TOKEN_URI", "https://oauth2.googleapis.com/token")  # tests can fake it
SHOW = os.path.expanduser("~/walter/bin/walter-show")
WALTERCTL = os.path.expanduser("~/walter/bin/walterctl")
RELAY_PATH = "/app/oauth/google.html"
PORT = int(os.environ.get("GOOGLE_CONNECT_PORT", "8772"))
STATE_TTL = 15 * 60
EMAIL_RE = re.compile(r"^[^@\s/\\]{1,64}@[A-Za-z0-9.-]{1,253}$")


# ------------------------------------------------------------------ pure helpers (tested)
def load_web_client(path: str | None = None) -> dict:
    """{"client_id", "client_secret"} from a Google 'web' client file, or {} if missing/wrong."""
    try:
        with open(os.path.expanduser(path or WEB_CLIENT)) as f:
            d = json.load(f).get("web") or {}
    except (OSError, ValueError, AttributeError):
        return {}
    if not d.get("client_id") or not d.get("client_secret"):
        return {}
    return {"client_id": d["client_id"], "client_secret": d["client_secret"]}


def public_url(config_path: str | None = None) -> str:
    try:
        with open(config_path or CONFIG, "rb") as f:
            return str(tomllib.load(f).get("app", {}).get("public_url") or "").rstrip("/")
    except (OSError, tomllib.TOMLDecodeError):
        return ""


def redirect_uri(pub: str) -> str:
    return pub.rstrip("/") + RELAY_PATH


def scopes_from_consent_url(url: str) -> list[str]:
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    return sorted(set((q.get("scope") or [""])[0].split()))


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def make_state(nonce: str, back: str) -> str:
    return nonce + "." + base64.urlsafe_b64encode(back.encode()).rstrip(b"=").decode()


def nonce_of(state: str) -> str:
    return (state or "").split(".", 1)[0]


def auth_url(client_id: str, redirect: str, scopes: list[str], state: str, challenge: str) -> str:
    return AUTH_URI + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(scopes), "access_type": "offline", "prompt": "consent select_account",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})


def id_token_claims(id_token: str) -> dict:
    """Claims of an ID token received straight from Google's token endpoint over TLS (OIDC
    allows skipping the signature check in that case). We still check aud/iss."""
    try:
        part = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    except Exception:
        return {}


def email_from_tokens(tok: dict, client_id: str) -> str:
    c = id_token_claims(tok.get("id_token") or "")
    if c.get("aud") != client_id or c.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        return ""
    if not c.get("email_verified", False):
        return ""
    email = str(c.get("email") or "").strip().lower()
    return email if EMAIL_RE.match(email) else ""


def missing_scopes(granted: list[str], required: list[str]) -> list[str]:
    g = set(granted)
    # Google answers with full URLs for openid/email/profile sometimes and short names other times
    alias = {"email": "https://www.googleapis.com/auth/userinfo.email",
             "profile": "https://www.googleapis.com/auth/userinfo.profile"}
    g |= {alias[s] for s in list(g) if s in alias} | {k for k, v in alias.items() if v in g}
    return [s for s in required if s not in g]


def credential_record(tok: dict, client: dict, now: datetime.datetime | None = None) -> dict:
    """Exactly what workspace-mcp's LocalDirectoryCredentialStore.store_credential writes."""
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)  # naive UTC, like google-auth
    exp = now + datetime.timedelta(seconds=int(tok.get("expires_in") or 3599))
    return {"token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": client["client_id"], "client_secret": client["client_secret"],
            "scopes": sorted(set((tok.get("scope") or "").split())), "expiry": exp.isoformat()}


def credential_path(email: str, creds_dir: str | None = None) -> str:
    """workspace-mcp's file name: quote(email, safe='@._-') + '.json', contained in creds_dir."""
    creds_dir = creds_dir or CREDS
    name = urllib.parse.quote(email, safe="@._-") + ".json"
    p = os.path.join(creds_dir, name)
    if os.path.dirname(os.path.realpath(p)) != os.path.realpath(creds_dir):
        raise ValueError("bad credential path")
    return p


def write_credential(email: str, rec: dict, creds_dir: str | None = None) -> str:
    creds_dir = creds_dir or CREDS
    os.makedirs(creds_dir, mode=0o700, exist_ok=True)
    os.chmod(creds_dir, 0o700)
    path = credential_path(email, creds_dir)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(rec, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def exchange(code: str, verifier: str, client: dict, redirect: str, timeout: float = 15.0) -> tuple[dict, str]:
    """(token response, problem). Never logs the secret or the tokens."""
    body = urllib.parse.urlencode({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
                                   "client_id": client["client_id"], "client_secret": client["client_secret"],
                                   "code_verifier": verifier}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URI, data=body), timeout=timeout) as r:
            return json.loads(r.read(65536)), ""
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read(2000) or b"{}").get("error", "")
        except ValueError:
            err = ""
        if err == "redirect_uri_mismatch":
            return {}, "Google says the redirect address doesn't match the Web client. Check step 3 of the setup."
        if err == "invalid_grant":
            return {}, "Google says that sign-in code expired or was already used. Tap Connect again."
        if err == "invalid_client":
            return {}, "Google didn't accept Walter's Web client. It may need dropping again."
        return {}, "Google answered with an error (%s). Tap Connect again in a minute." % (err or e.code)
    except Exception:
        return {}, "Couldn't reach Google to finish signing in. Tap Connect again."


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 80)); return s.getsockname()[0]
    finally:
        s.close()


# ------------------------------------------------------------------ MCP side (scopes + proof)
def _authorize_mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location("walter_gmcp_authorize", os.path.join(HERE, "authorize.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


class MCP:
    """One workspace-mcp stdio session, run on its own event loop thread."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.session = None
        self.error = ""
        self._stop = None
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        self._stop = asyncio.Event()
        os.makedirs(os.path.join(W, "google-mcp/logs"), exist_ok=True)
        errlog = open(os.path.join(W, "google-mcp/logs/phone-connect-stderr.log"), "a")
        try:
            params = StdioServerParameters(command=os.path.join(HERE, "run.sh"), env=dict(os.environ))
            async with stdio_client(params, errlog=errlog) as (r, w):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    self.session = s
                    self.ready.set()
                    await self._stop.wait()
        except Exception as e:
            self.error = type(e).__name__
        finally:
            self.ready.set()

    def call(self, coro_fn, timeout=60):
        if not self.ready.wait(90) or self.session is None:
            raise RuntimeError("MCP server didn't start (%s)" % (self.error or "timeout"))
        return asyncio.run_coroutine_threadsafe(coro_fn(self.session), self.loop).result(timeout)

    def close(self):
        if self._stop is not None:
            self.loop.call_soon_threadsafe(self._stop.set)


def required_scopes(mcp, probe) -> list[str]:
    """The exact scopes workspace-mcp asks for with run.sh's --permissions: read them off the
    consent link it builds for an account it has never seen (placeholder address)."""
    _, url = mcp.call(lambda s: probe(s, "walter-scope-probe@example.com"))
    return scopes_from_consent_url(url or "")


# ------------------------------------------------------------------ phone pages
STYLE = """<meta name=viewport content="width=device-width,initial-scale=1"><meta name=referrer content=no-referrer>
<style>body{font-family:system-ui;background:#05060a;color:#e8eefc;padding:22px;max-width:520px;margin:auto}
a.b{display:block;text-align:center;background:#58c4ff;color:#05060a;padding:15px;border-radius:12px;font-weight:600;
text-decoration:none;font-size:18px;margin:14px 0}a.g{background:#1b2333;color:#e8eefc}
p,li{color:#9fb0d0;line-height:1.45;font-size:17px}.ok{color:#7fe0a0}.bad{color:#ff9a8a}</style>"""


def page(title: str, body: str) -> str:
    return "<!doctype html><title>Walter - Google</title>%s<h2>%s</h2>%s" % (STYLE, html.escape(title), body)


def home_page(tok: str, connected: list[str]) -> str:
    done = "".join("<li class=ok>%s</li>" % html.escape(e) for e in connected)
    return page("Connect Google to Walter", (
        "<p>Tap below, pick the Google account and tap <b>Allow</b>. You're already signed in on this phone, "
        "so there's nothing to type.</p>"
        "<p>If Google warns that the app isn't verified: tap <b>Advanced</b>, then <b>Go to ...</b>. "
        "It's Walter's own private app.</p>"
        "<p>Please leave every box ticked (Gmail drafts, Calendar, Drive).</p>"
        "<a class=b href='/%s/go'>Connect a Google account</a>" % tok
        + ("<p>Connected so far:</p><ul>%s</ul><a class='b g' href='/%s/done'>I'm done</a>" % (done, tok) if connected else "")))


def result_page(tok: str, ok: bool, text: str) -> str:
    cls = "ok" if ok else "bad"
    return page("All set" if ok else "Not quite", "<p class=%s>%s</p>"
                "<a class=b href='/%s/go'>%s</a><a class='b g' href='/%s/done'>I'm done</a>"
                % (cls, html.escape(text), tok, "Connect another account" if ok else "Try again", tok))


# ------------------------------------------------------------------ main
def note(text: str):
    subprocess.run([WALTERCTL, "ask", "[system note, not your person speaking] " + text], capture_output=True)


def show_qr(url: str, body: str, secs: int):
    subprocess.run([SHOW, "--qr", url, "Connect Google to Walter", body, str(secs)], capture_output=True)


def main(argv):
    minutes = int(argv[argv.index("--minutes") + 1]) if "--minutes" in argv else 20
    show = "--no-show" not in argv
    client = load_web_client()
    if not client:
        print("NO WEB CLIENT: the owner needs to create a Web-application OAuth client and drop it with "
              "python3 ~/walter/tools/google-mcp/oauth-drop.py (see the Google section of the setup docs).")
        return 2
    pub = public_url()
    if not pub.startswith("https://"):
        print("NO PUBLIC HTTPS ADDRESS: set [app] public_url in ~/.walter/config.toml."); return 2
    redirect = redirect_uri(pub)
    probe = _authorize_mod().probe
    mcp = MCP()
    try:
        scopes = required_scopes(mcp, probe)
    except Exception as e:
        print("FAILED: couldn't ask the Google MCP server which permissions it needs (%s)." % e)
        mcp.close(); return 1
    if not scopes:
        print("FAILED: the MCP server didn't give a consent link to read the scopes from (see ~/.walter/google-mcp/logs/).")
        mcp.close(); return 1

    tok = secrets.token_urlsafe(12)
    ip = lan_ip()
    base = "http://%s:%d/%s" % (ip, PORT, tok)
    pending: dict[str, tuple[str, float]] = {}   # nonce -> (pkce verifier, created)
    connected: list[str] = []
    lock = threading.Lock()

    def finish_code(q: dict) -> tuple[bool, str]:
        if q.get("error"):
            return False, ("You tapped Cancel, so nothing was connected." if q["error"] == "access_denied"
                           else "Google stopped the sign-in (%s)." % q["error"][:60])
        with lock:
            entry = pending.pop(nonce_of(q.get("state", "")), None)
        if not entry or time.time() - entry[1] > STATE_TTL:
            return False, "That sign-in link is old or was already used. Tap Try again."
        resp, problem = exchange(q.get("code", ""), entry[0], client, redirect)
        if problem:
            return False, problem
        email = email_from_tokens(resp, client["client_id"])
        if not email:
            return False, "Google didn't say which account this is. Tap Try again."
        miss = missing_scopes((resp.get("scope") or "").split(), scopes)
        if miss:
            return False, ("Some boxes were unticked on Google's page, so Walter can't use %s yet. Tap Try "
                           "again and leave every box ticked." % email)
        if not resp.get("refresh_token"):
            return False, "Google didn't give a long-lived permission. Tap Try again."
        write_credential(email, credential_record(resp, client))
        resp = None
        try:
            ok, _ = mcp.call(lambda s: probe(s, email))
        except Exception:
            ok = False
        if not ok:
            return False, ("Saved %s, but Walter's test Gmail search didn't work yet. Tell Walter; he'll check "
                           "the logs." % email)
        with lock:
            if email not in connected:
                connected.append(email)
        print("CONNECTED: %s (read-only Gmail check passed)" % email, flush=True)
        if show:
            show_qr(base + "/", "Connected: %s\nScan again for another account" % ", ".join(connected),
                    int(max(60, deadline - time.time())))
        return True, "Walter can now use %s: Gmail drafts, Calendar and Drive. (Sending email stays off.)" % email

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass   # the query holds the sign-in code: never log requests

        def reply(self, code: int, body: str, location: str = ""):
            b = body.encode()
            self.send_response(code)
            if location:
                self.send_header("Location", location)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)

        def do_GET(self):
            parts = urllib.parse.urlsplit(self.path)
            path = parts.path.rstrip("/")
            if path == "/" + tok:
                with lock:
                    c = list(connected)
                return self.reply(200, home_page(tok, c))
            if path == "/%s/go" % tok:
                nonce = secrets.token_urlsafe(16)
                verifier, challenge = pkce_pair()
                with lock:
                    for k in [k for k, v in pending.items() if time.time() - v[1] > STATE_TTL]:
                        pending.pop(k, None)
                    pending[nonce] = (verifier, time.time())
                url = auth_url(client["client_id"], redirect, scopes, make_state(nonce, base + "/cb"), challenge)
                return self.reply(302, "", url)
            if path == "/%s/cb" % tok:
                q = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
                try:
                    ok, text = finish_code(q)
                except Exception as e:
                    ok, text = False, "Something went wrong on Walter's side (%s). Tap Try again." % type(e).__name__
                q = None
                return self.reply(200, result_page(tok, ok, text))
            if path == "/%s/done" % tok:
                self.reply(200, page("Thanks", "<p>All done. You can close this page.</p>"))
                threading.Thread(target=srv.shutdown).start()
                return
            self.reply(404, "not found")

    srv = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), H)
    deadline = time.time() + minutes * 60
    if show:
        show_qr(base + "/", "Scan with your phone (on the home Wi-Fi),\nthen pick the account and tap Allow", minutes * 60)
    print("QR is on the wall (page %s:%d, %d min). Waiting for the owner..." % (ip, PORT, minutes), flush=True)
    timer = threading.Timer(minutes * 60, srv.shutdown); timer.daemon = True; timer.start()
    try:
        srv.serve_forever()
    finally:
        timer.cancel(); srv.server_close(); mcp.close()
        if show:
            subprocess.run([SHOW, "--clear"], capture_output=True)
    if connected:
        msg = "Google accounts connected from the owner's phone and checked with a read-only Gmail search: %s." % ", ".join(connected)
        print("DONE: " + msg)
        note(msg + " Tools are mcp__google-accounts__* (pass user_google_email). If they aren't loaded, "
             "walterctl new-session. Tell the owner briefly.")
        return 0
    print("NOTHING CONNECTED (timed out or the owner finished without connecting).")
    note("Google phone connect closed with no account connected (timed out or cancelled). Mention it only if the owner asks.")
    return 1


if __name__ == "__main__":
    if sys.argv[1:2] in (["-h"], ["--help"]):
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1:]))
