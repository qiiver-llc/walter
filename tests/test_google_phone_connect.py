"""tools/google-mcp/phone-connect.py + the static relay page + oauth-drop's Web client support.
No real Google, no real credentials: a fake token endpoint and a stub MCP server."""
import asyncio, base64, hashlib, http.server, importlib.util, json, os, re, threading, time
import urllib.error, urllib.parse, urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools" / "google-mcp"
CID = "123456789-abcdefghijklmnop.apps.googleusercontent.com"
SEC = "GOCSPX-not-a-real-secret-0000"
SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email",
          "https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/calendar"]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, TOOLS / file)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m


@pytest.fixture
def pc():
    return load("phone_connect_t", "phone-connect.py")


def b64(d: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()


def id_token(**claims) -> str:
    c = {"iss": "https://accounts.google.com", "aud": CID, "email": "user@example.com", "email_verified": True}
    c.update(claims)
    return b64({"alg": "RS256"}) + "." + b64(c) + ".sig"


# ------------------------------------------------------------------ helpers
def test_state_roundtrip_and_relay_regex(pc):
    back = "http://192.168.1.20:8772/AbCdEfGh12345678/cb"
    st = pc.make_state("nonce123", back)
    assert pc.nonce_of(st) == "nonce123"
    enc = st.split(".", 1)[1]
    assert base64.urlsafe_b64decode(enc + "=" * (-len(enc) % 4)).decode() == back
    # the relay page only forwards to private-network http addresses
    js = (ROOT / "app" / "oauth" / "google.js").read_text()
    rx = re.compile(re.search(r"var ok = /(.+)/\.test\(back\)", js).group(1).replace("\\/", "/"))
    assert rx.match(back)
    assert rx.match("http://10.0.0.5:8772/AbCdEfGh12345678/cb")
    assert rx.match("http://100.101.1.2:8772/AbCdEfGh12345678/cb")   # tailnet
    for bad in ("https://evil.example.com/AbCdEfGh12345678/cb", "http://8.8.8.8:80/AbCdEfGh12345678/cb",
                "http://192.168.1.20:8772/AbCdEfGh12345678/cb/../x", "http://192.168.1.20.evil.com:80/abcdefgh/cb",
                "javascript:alert(1)//192.168.1.1:80/abcdefgh/cb", "http://172.32.0.1:80/AbCdEfGh12345678/cb"):
        assert not rx.match(bad), bad


def test_relay_page_is_static_and_csp_safe():
    html = (ROOT / "app" / "oauth" / "google.html").read_text()
    assert "<script src=\"google.js\"></script>" in html and "<script>" not in html   # CSP: script-src 'self'


def test_auth_url_has_pkce_offline_and_scopes(pc):
    v, ch = pc.pkce_pair()
    assert 43 <= len(v) <= 128
    assert ch == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    u = pc.auth_url(CID, "https://walter.example.com/app/oauth/google.html", SCOPES, "s.x", ch)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)
    assert q["access_type"] == ["offline"] and "consent" in q["prompt"][0]
    assert q["code_challenge_method"] == ["S256"] and q["scope"][0].split() == SCOPES
    assert pc.scopes_from_consent_url(u) == sorted(SCOPES)


def test_email_from_tokens_checks_audience_issuer_verified(pc):
    assert pc.email_from_tokens({"id_token": id_token()}, CID) == "user@example.com"
    assert pc.email_from_tokens({"id_token": id_token(aud="someone-else")}, CID) == ""
    assert pc.email_from_tokens({"id_token": id_token(iss="https://evil.example.com")}, CID) == ""
    assert pc.email_from_tokens({"id_token": id_token(email_verified=False)}, CID) == ""
    assert pc.email_from_tokens({"id_token": id_token(email="../../x@example.com")}, CID) == ""
    assert pc.email_from_tokens({}, CID) == ""


def test_missing_scopes(pc):
    assert pc.missing_scopes(SCOPES, SCOPES) == []
    assert pc.missing_scopes(["openid", "email", SCOPES[2], SCOPES[3]], SCOPES) == []
    assert pc.missing_scopes(SCOPES[:2], SCOPES) == SCOPES[2:]


def test_credential_record_matches_workspace_mcp_format(pc, tmp_path):
    import datetime
    now = datetime.datetime(2026, 11, 5, 12, 0, 0)
    tok = {"access_token": "at", "refresh_token": "rt", "expires_in": 3599, "scope": " ".join(SCOPES)}
    rec = pc.credential_record(tok, {"client_id": CID, "client_secret": SEC}, now)
    assert set(rec) == {"token", "refresh_token", "token_uri", "client_id", "client_secret", "scopes", "expiry"}
    assert rec["expiry"] == "2026-11-05T12:59:59" and rec["scopes"] == sorted(SCOPES)
    p = pc.write_credential("user+x@example.com", rec, str(tmp_path / "creds"))
    assert os.path.basename(p) == "user%2Bx@example.com.json"          # quote(email, safe="@._-")
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"
    assert oct(os.stat(tmp_path / "creds").st_mode & 0o777) == "0o700"
    assert json.loads(Path(p).read_text()) == rec


def test_workspace_mcp_reads_our_file(pc, tmp_path):
    """If the MCP's venv is here, prove its own credential store loads what we wrote."""
    import glob, subprocess, sys
    py = os.path.expanduser("~/.walter/google-mcp/venv/bin/python")
    if not os.path.exists(py):
        pytest.skip("workspace-mcp not installed")
    tok = {"access_token": "at", "refresh_token": "rt", "expires_in": 3599, "scope": " ".join(SCOPES)}
    pc.write_credential("user@example.com", pc.credential_record(tok, {"client_id": CID, "client_secret": SEC}),
                        str(tmp_path))
    code = ("import os;os.environ['WORKSPACE_MCP_CREDENTIALS_DIR']=%r\n"
            "from auth.credential_store import LocalDirectoryCredentialStore as S\n"
            "c=S().get_credential('user@example.com');print(c.refresh_token,c.client_id,sorted(c.scopes)==%r,c.expiry)"
            % (str(tmp_path), sorted(SCOPES)))
    out = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-500:]
    assert out.stdout.split()[:3] == ["rt", CID, "True"]


def test_load_web_client(pc, tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"installed": {"client_id": CID, "client_secret": SEC}}))
    assert pc.load_web_client(str(f)) == {}
    f.write_text(json.dumps({"web": {"client_id": CID, "client_secret": SEC}}))
    assert pc.load_web_client(str(f)) == {"client_id": CID, "client_secret": SEC}
    assert pc.load_web_client(str(tmp_path / "missing.json")) == {}


# ------------------------------------------------------------------ end to end (fake Google)
class FakeGoogle:
    def __init__(self, scope=" ".join(SCOPES), email="user@example.com"):
        self.got = {}
        me = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def do_POST(self):
                form = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                me.got = {k: v[0] for k, v in form.items()}
                if me.got.get("code") != "good-code":
                    body, code = b'{"error":"invalid_grant"}', 400
                else:
                    body, code = json.dumps({"access_token": "at", "refresh_token": "rt", "expires_in": 3599,
                                             "scope": scope, "id_token": id_token(email=email)}).encode(), 200
                self.send_response(code); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/token" % self.srv.server_address[1]


class StubMCP:
    def __init__(self):
        self.probed = []

    def call(self, fn, timeout=60):
        return asyncio.run(fn(None))

    def close(self):
        pass


def free_port():
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def run_flow(pc, monkeypatch, tmp_path, google, code="good-code", probe_ok=True):
    creds = tmp_path / "creds"
    shown, notes = [], []
    probed = []

    async def probe(_s, email):
        probed.append(email)
        if email.endswith("walter-scope-probe@example.com"):
            return False, "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode({"scope": " ".join(SCOPES)})
        return probe_ok, None

    port = free_port()
    monkeypatch.setattr(pc, "PORT", port)
    monkeypatch.setattr(pc, "CREDS", str(creds))
    monkeypatch.setattr(pc, "TOKEN_URI", google.url)
    monkeypatch.setattr(pc, "load_web_client", lambda *a: {"client_id": CID, "client_secret": SEC})
    monkeypatch.setattr(pc, "public_url", lambda *a: "https://walter.example.com")
    monkeypatch.setattr(pc, "lan_ip", lambda: "127.0.0.1")
    monkeypatch.setattr(pc, "MCP", StubMCP)
    monkeypatch.setattr(pc, "_authorize_mod", lambda: type("M", (), {"probe": staticmethod(probe)}))
    monkeypatch.setattr(pc, "show_qr", lambda url, body, secs: shown.append((url, body)))
    monkeypatch.setattr(pc, "note", lambda t: notes.append(t))
    monkeypatch.setattr(pc, "SHOW", "/bin/true")
    rc = []
    t = threading.Thread(target=lambda: rc.append(pc.main(["--minutes", "1"])), daemon=True)
    t.start()
    for _ in range(100):
        if shown: break
        time.sleep(0.05)
    home = shown[0][0]
    assert home.startswith("http://127.0.0.1:%d/" % port)
    assert "Connect a Google account" in urllib.request.urlopen(home).read().decode()
    op = urllib.request.build_opener(NoRedirect)
    try:
        op.open(home + "go"); raise AssertionError("expected a redirect")
    except urllib.error.HTTPError as e:
        assert e.code == 302
        to_google = e.headers["Location"]
    q = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(to_google).query).items()}
    assert q["redirect_uri"] == "https://walter.example.com/app/oauth/google.html"
    assert q["client_id"] == CID
    # what the relay page would do: forward ?code&state to the address inside the state
    enc = q["state"].split(".", 1)[1]
    back = base64.urlsafe_b64decode(enc + "=" * (-len(enc) % 4)).decode()
    assert back == home + "cb"
    page = urllib.request.urlopen(back + "?" + urllib.parse.urlencode({"code": code, "state": q["state"]})).read().decode()
    replay = urllib.request.urlopen(back + "?" + urllib.parse.urlencode({"code": code, "state": q["state"]})).read().decode()
    urllib.request.urlopen(home + "done").read()
    t.join(10)
    return dict(rc=rc, page=page, replay=replay, q=q, creds=creds, notes=notes, probed=probed, shown=shown)


def test_end_to_end_connects_and_writes_token(pc, monkeypatch, tmp_path):
    g = FakeGoogle()
    r = run_flow(pc, monkeypatch, tmp_path, g)
    assert "All set" in r["page"] and "user@example.com" in r["page"]
    assert "rt" not in r["page"].split() and SEC not in r["page"]
    # PKCE: the verifier we sent matches the challenge Google saw
    v = g.got["code_verifier"]
    assert base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode() == r["q"]["code_challenge"]
    assert g.got["redirect_uri"] == "https://walter.example.com/app/oauth/google.html"
    rec = json.loads((r["creds"] / "user@example.com.json").read_text())
    assert rec["refresh_token"] == "rt" and rec["client_id"] == CID
    assert r["probed"][-1] == "user@example.com"            # one read-only check through the MCP
    assert "already used" in r["replay"]                     # a state works once
    assert r["rc"] == [0] and "user@example.com" in r["notes"][-1]


def test_unticked_boxes_saves_nothing(pc, monkeypatch, tmp_path):
    g = FakeGoogle(scope="openid https://www.googleapis.com/auth/userinfo.email")
    r = run_flow(pc, monkeypatch, tmp_path, g)
    assert "unticked" in r["page"]
    assert not list(r["creds"].glob("*.json")) if r["creds"].exists() else True
    assert r["rc"] == [1]


def test_bad_code_reports_plainly(pc, monkeypatch, tmp_path):
    r = run_flow(pc, monkeypatch, tmp_path, FakeGoogle(), code="stale")
    assert "expired or was already used" in r["page"]
    assert r["rc"] == [1]


# ------------------------------------------------------------------ oauth-drop accepts a Web client
def test_oauth_drop_accepts_both_kinds(tmp_path, monkeypatch):
    od = load("oauth_drop_t", "oauth-drop.py")
    web = json.dumps({"web": {"client_id": CID, "client_secret": SEC}})
    desk = json.dumps({"installed": {"client_id": CID, "client_secret": SEC}})
    assert od.parse({"json": [web]})[2:] == ("web", "")
    assert od.parse({"json": [desk]})[2:] == ("installed", "")
    assert od.parse({"id": [CID], "secret": [SEC]})[2] == "web"
    assert od.parse({"id": [CID], "secret": [SEC], "kind": ["installed"]})[2] == "installed"
    assert od.parse({"json": ["[1]"]})[3]
    monkeypatch.setattr(od, "public_redirect", lambda: "https://walter.example.com/app/oauth/google.html")
    od.save(str(tmp_path / "s" / "w.json"), CID, SEC, "web")
    d = json.loads((tmp_path / "s" / "w.json").read_text())
    assert d["web"]["redirect_uris"] == ["https://walter.example.com/app/oauth/google.html"]
    assert oct(os.stat(tmp_path / "s" / "w.json").st_mode & 0o777) == "0o600"
