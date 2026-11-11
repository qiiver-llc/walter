"""Cloud Link install side (walter/cloud_link.py, walter/app_cloud_link.py) against a fake
Cloud Link service that checks the contract v1 signatures. Never touches the real
systemd units, tunnel or ~/.walter."""
import asyncio
import base64
import hashlib
import json
import os
import stat
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from walter import cloud_link as CL

HOST = "bcdfg2345678.wallter.app"


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ------------------------------------------------------------ a fake Cloud Link service
class Broker:
    def __init__(self):
        self.keys = {}          # install_id -> pubkey bytes
        self.calls = []
        self.codes = {"goodcode42"}
        self.tokens = 0
        self.status = "active"
        self.released = False

    def token(self):
        self.tokens += 1
        return f"eyJhIjoidG9rZW4ifQ-{self.tokens:04d}-" + "x" * 24

    def verify(self, request, raw: bytes) -> str:
        iid = request.headers.get("X-Walter-Install", "")
        ts = int(request.headers.get("X-Walter-Ts", "0"))
        sig = b64d(request.headers.get("X-Walter-Sig", ""))
        assert abs(time.time() - ts) <= 300
        msg = f"{request.method}\n{request.path}\n{ts}\n{hashlib.sha256(raw).hexdigest()}".encode()
        Ed25519PublicKey.from_public_bytes(self.keys[iid]).verify(sig, msg)   # raises if wrong
        return iid

    def app(self):
        routes = web.RouteTableDef()
        hh = {"slug": "bcdfg2345678", "host": HOST}

        @routes.post("/v1/claim")
        async def claim(request):
            raw = await request.read()
            d = json.loads(raw)
            self.calls.append(("claim", d))
            if set(d) != {"invite_code", "install_id", "install_pubkey", "walter_version"}:
                return web.json_response({"error": "bad"}, status=400)
            if d["install_id"] in self.keys:
                self.verify(request, raw)
                return web.json_response({"household": hh, "tunnel_token": self.token(), "status": "active"},
                                         status=409)
            if d["invite_code"] not in self.codes:
                return web.json_response({"error": "invalid"}, status=403)
            self.codes.discard(d["invite_code"])
            self.keys[d["install_id"]] = b64d(d["install_pubkey"])
            self.verify(request, raw)   # the claim is signed too
            return web.json_response({"household": hh, "tunnel_token": self.token(), "status": "active"})

        @routes.get("/v1/status")
        async def status(request):
            self.verify(request, await request.read())
            self.calls.append(("status", None))
            return web.json_response({"household": hh, "status": self.status, "last_seen": "2026-11-11T12:00:00Z"})

        @routes.post("/v1/heartbeat")
        async def heartbeat(request):
            raw = await request.read()
            self.verify(request, raw)
            self.calls.append(("heartbeat", json.loads(raw)))
            return web.json_response({"status": self.status})

        @routes.post("/v1/rotate")
        async def rotate(request):
            self.verify(request, await request.read())
            self.calls.append(("rotate", None))
            return web.json_response({"tunnel_token": self.token()})

        @routes.post("/v1/release")
        async def release(request):
            self.verify(request, await request.read())
            self.calls.append(("release", None))
            self.released = True
            return web.json_response({"status": "released"})

        a = web.Application()
        a.add_routes(routes)
        return a


class Env:
    def __init__(self, tmp_path, base):
        self.paths = CL.Paths(data=tmp_path / "data", units=tmp_path / "units", system_units=tmp_path / "sys")
        self.ran = []
        self.url = [""]
        self.active = True

        def run(*args):
            self.ran.append(args)
            if args[0] == "is-active":
                return (0, "active") if self.active else (3, "inactive")
            return 0, ""

        async def health(host, s):
            return True

        self.cl = CL.CloudLink(base, paths=self.paths, run=run, cloudflared=lambda: "/usr/bin/cloudflared",
                               get_public_url=lambda: self.url[0],
                               set_public_url=lambda u: self.url.__setitem__(0, u), health=health)


def with_broker(fn):
    async def go(tmp_path):
        b = Broker()
        async with TestServer(b.app()) as srv:
            e = Env(tmp_path, str(srv.make_url("")).rstrip("/"))
            await fn(b, e)
    return go


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


# ------------------------------------------------------------ identity + signature format
def test_identity_file_is_private_and_reused(tmp_path):
    p = tmp_path / "secrets" / "cloud-link.key"
    a = CL.Identity.load_or_create(p)
    assert mode(p) == 0o600 and mode(p.parent) == 0o700
    b = CL.Identity.load_or_create(p)
    assert a.install_id == b.install_id and a.public_b64u == b.public_b64u
    assert len(b64d(a.public_b64u)) == 32 and "=" not in a.public_b64u
    import uuid
    assert uuid.UUID(a.install_id).version == 4


def test_signature_verifies_with_the_public_key(tmp_path):
    ident = CL.Identity.load_or_create(tmp_path / "k")
    body = b'{"version":"1","healthy":true}'
    h = ident.sign("POST", "/v1/heartbeat", body, ts=1_700_000_000)
    assert h["X-Walter-Install"] == ident.install_id and h["X-Walter-Ts"] == "1700000000"
    msg = ("POST\n/v1/heartbeat\n1700000000\n" + hashlib.sha256(body).hexdigest()).encode()
    assert CL.signing_message("post", "/v1/heartbeat", 1_700_000_000, body) == msg
    pub = Ed25519PublicKey.from_public_bytes(b64d(ident.public_b64u))
    pub.verify(b64d(h["X-Walter-Sig"]), msg)
    with pytest.raises(Exception):
        pub.verify(b64d(h["X-Walter-Sig"]), msg.replace(b"POST", b"GET"))


def test_damaged_key_file_is_an_error_not_a_new_identity(tmp_path):
    p = tmp_path / "k"
    p.write_text("{nope")
    with pytest.raises(CL.CloudLinkError):
        CL.Identity.load_or_create(p)
    assert p.read_text() == "{nope"


def test_api_base_must_be_https(tmp_path):
    ident = CL.Identity.load_or_create(tmp_path / "k")
    with pytest.raises(CL.CloudLinkError):
        CL.Api("http://evil.example", ident)
    CL.Api("https://api.wallter.app", ident)


def test_unit_text_keeps_the_token_off_the_command_line(tmp_path):
    txt = CL.render_unit("/usr/bin/cloudflared", tmp_path / "secrets" / "cloud-link.tunnel")
    assert f"EnvironmentFile={tmp_path}/secrets/cloud-link.tunnel" in txt
    assert "ExecStart=/usr/bin/cloudflared tunnel --no-autoupdate run\n" in txt
    assert "--token" not in txt and "TUNNEL_TOKEN" not in txt
    assert "WantedBy=default.target" in txt and "Restart=always" in txt
    with pytest.raises(CL.CloudLinkError):
        CL.render_unit("/bin/x y", tmp_path)


def test_clean_code():
    assert CL.clean_code(" GOOD-code 42 ") == "goodcode42"
    assert CL.clean_code(None) == ""


# ------------------------------------------------------------ enable / status / rotate / disable
def test_enable_status_rotate_disable_round_trip(tmp_path):
    async def body(b, e):
        e.url[0] = "http://192.168.1.5:8765"
        r = await e.cl.enable("GOOD-CODE-42")
        assert r["ok"] and r["host"] == HOST and r["url"] == f"https://{HOST}" and r["healthy"]
        # token: mode 600, EnvironmentFile format; the unit has no token in it
        tok = e.paths.tunnel.read_text()
        assert tok.startswith("TUNNEL_TOKEN=eyJ") and mode(e.paths.tunnel) == 0o600
        assert mode(e.paths.key) == 0o600 and mode(e.paths.state) == 0o600
        unit = e.paths.unit.read_text()
        assert "EnvironmentFile=" + str(e.paths.tunnel) in unit and tok.split("=", 1)[1].strip() not in unit
        assert ("enable", "--now", CL.UNIT) in e.ran and ("daemon-reload",) in e.ran
        assert e.url[0] == f"https://{HOST}"
        st = e.cl.state()
        assert st["enabled"] and st["previous_public_url"] == "http://192.168.1.5:8765"
        assert b.calls[0][1]["invite_code"] == "goodcode42"
        # status asks the service (signed GET)
        s = await e.cl.status()
        assert s["enabled"] and s["connector"] == "running" and s["remote_status"] == "active"
        assert s["last_seen"]
        # heartbeat
        await e.cl.heartbeat()
        assert b.calls[-1] == ("heartbeat", {"version": CL.walter_version(), "healthy": True})
        assert e.cl.state()["last_heartbeat"] > 0
        # rotate: new token, connector restarted
        await e.cl.rotate()
        assert e.paths.tunnel.read_text() != tok and ("restart", CL.UNIT) in e.ran
        # enabling twice is refused
        with pytest.raises(CL.CloudLinkError) as ex:
            await e.cl.enable("goodcode42")
        assert ex.value.code == "already_on"
        # disable: released at the service, unit + token gone, old address back
        r = await e.cl.disable()
        assert r["released"] and b.released and r["restored_url"] == "http://192.168.1.5:8765"
        assert e.url[0] == "http://192.168.1.5:8765"
        assert not e.paths.unit.exists() and not e.paths.tunnel.exists()
        assert ("disable", "--now", CL.UNIT) in e.ran
        assert not e.cl.enabled and e.paths.key.exists()     # identity kept
        assert (await e.cl.disable())["was_on"] is False
    asyncio.run(with_broker(body)(tmp_path))


def test_bad_code_writes_nothing(tmp_path):
    async def body(b, e):
        with pytest.raises(CL.CloudLinkError) as ex:
            await e.cl.enable("wrongcode1")
        assert ex.value.code == "invalid_code" and ex.value.status == 403
        assert not e.paths.tunnel.exists() and not e.paths.unit.exists() and e.ran == []
        with pytest.raises(CL.CloudLinkError) as ex:
            await e.cl.enable("no!")
        assert ex.value.code == "bad_code"
    asyncio.run(with_broker(body)(tmp_path))


def test_reclaim_by_the_same_install_gets_a_fresh_token(tmp_path):
    async def body(b, e):
        await e.cl.enable("goodcode42")
        e.cl.save_state({})        # lost local state, same key
        r = await e.cl.enable("goodcode42")
        assert r["host"] == HOST and b.tokens == 2
    asyncio.run(with_broker(body)(tmp_path))


def test_unit_failure_cleans_up(tmp_path):
    async def body(b, e):
        e.cl.run = lambda *a: (1, "boom") if a[0] == "enable" else (0, "")
        with pytest.raises(CL.CloudLinkError) as ex:
            await e.cl.enable("goodcode42")
        assert ex.value.code == "unit_failed"
        assert not e.paths.tunnel.exists() and not e.paths.unit.exists() and not e.cl.enabled
    asyncio.run(with_broker(body)(tmp_path))


def test_revoked_household_is_remembered(tmp_path):
    async def body(b, e):
        await e.cl.enable("goodcode42")
        b.status = "revoked"
        e.active = False
        await e.cl.heartbeat()
        assert b.calls[-1][1]["healthy"] is False and e.cl.state()["remote_status"] == "revoked"
    asyncio.run(with_broker(body)(tmp_path))


def test_no_cloudflared_is_refused_before_claiming(tmp_path):
    async def body(b, e):
        e.cl.find_cloudflared = lambda: ""
        with pytest.raises(CL.CloudLinkError) as ex:
            await e.cl.enable("goodcode42")
        assert ex.value.code == "no_cloudflared" and b.calls == [] and "goodcode42" in b.codes
    asyncio.run(with_broker(body)(tmp_path))


# ------------------------------------------------------------ an existing tunnel is never touched
EXISTING = ("[Service]\nExecStart=%h/.local/bin/cloudflared tunnel --no-autoupdate run "
            "--token-file %h/.walter/secrets/cloudflared.token\n")


def test_refuses_when_the_install_already_has_a_tunnel_unless_replace(tmp_path):
    async def body(b, e):
        e.paths.units.mkdir(parents=True)
        own = e.paths.units / "walter-tunnel.service"
        own.write_text(EXISTING)
        e.url[0] = "https://walter.example.com"
        with pytest.raises(CL.CloudLinkError) as ex:
            await e.cl.enable("goodcode42")
        assert ex.value.code == "existing_tunnel" and "walter-tunnel.service" in str(ex.value)
        assert "--replace" in str(ex.value) and b.calls == [] and e.ran == []
        r = await e.cl.enable("goodcode42", replace=True)
        assert r["ok"] and len(r["replaced"]) == 2
        assert own.read_text() == EXISTING                        # untouched
        assert not any("walter-tunnel" in " ".join(a) for a in e.ran)
        assert e.cl.state()["previous_public_url"] == "https://walter.example.com"
        await e.cl.disable()
        assert e.url[0] == "https://walter.example.com" and own.read_text() == EXISTING
    asyncio.run(with_broker(body)(tmp_path))


def test_existing_tunnels_detection(tmp_path):
    p = CL.Paths(data=tmp_path, units=tmp_path / "u", system_units=tmp_path / "s")
    assert CL.existing_tunnels(p, "") == []
    assert CL.existing_tunnels(p, "http://192.168.1.5:8765") == []         # plain LAN address is fine
    assert CL.existing_tunnels(p, "https://x.ts.net")                       # someone's own https proxy
    assert CL.existing_tunnels(p, f"https://{HOST}", our_host=HOST) == []
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "cloudflared.service").write_text("[Service]\nExecStart=/usr/bin/cloudflared tunnel run\n")
    (tmp_path / "u").mkdir()
    (tmp_path / "u" / CL.UNIT).write_text("[Service]\nExecStart=/usr/bin/cloudflared tunnel run\n")   # ours: ignored
    assert len(CL.existing_tunnels(p, "")) == 1


# ------------------------------------------------------------ config update + restore (the real file path)
def test_public_url_written_to_config_and_restored(tmp_path):
    p = CL.Paths(data=tmp_path, units=tmp_path / "u")
    p.config.write_text("# mine\n[server]\nport = 1\n\n[app]\npublic_url = \"http://lan:8765\"  # keep me\n")
    note = CL.file_set_public_url(p, f"https://{HOST}")
    assert "isn't running" in note                       # port 1: nobody to reload
    assert CL.read_public_url(p) == f"https://{HOST}" and "# mine" in p.config.read_text()
    CL.file_set_public_url(p, "http://lan:8765")
    assert CL.read_public_url(p) == "http://lan:8765" and "# keep me" in p.config.read_text()


def test_config_has_a_cloud_link_section():
    from walter.config import Config
    c = Config()
    assert c.cloud_link.api_base == "https://api.wallter.app" and c.cloud_link.heartbeat_h == 6.0


# ------------------------------------------------------------ CLI wording
def test_cli_plain_output(tmp_path):
    async def body(b, e):
        out = []
        # cli() runs its own event loop: call it from a worker thread
        rc = await asyncio.to_thread(CL.cli, ["status"], out.append, None, e.cl)
        assert rc == 0 and out[0] == "Cloud Link is OFF."
        e.paths.units.mkdir(parents=True)
        (e.paths.units / "walter-tunnel.service").write_text(EXISTING)
        out.clear()
        rc = await asyncio.to_thread(CL.cli, ["enable", "goodcode42"], out.append, None, e.cl)
        assert rc == 3 and "already has its own tunnel" in out[-1]
        out.clear()
        rc = await asyncio.to_thread(CL.cli, ["enable", "goodcode42", "--replace"], out.append, None, e.cl)
        assert rc == 0 and any("Cloud Link is ON" in x for x in out) and any("untouched" in x for x in out)
        out.clear()
        rc = await asyncio.to_thread(CL.cli, ["status"], out.append, None, e.cl)
        assert out[0] == f"Cloud Link is ON at https://{HOST}/app"
        out.clear()
        rc = await asyncio.to_thread(CL.cli, ["disable"], out.append, None, e.cl)
        assert rc == 0 and out[0].startswith("Cloud Link is OFF")
        assert (await asyncio.to_thread(CL.cli, ["bogus"], out.append, None, e.cl)) == 2
    asyncio.run(with_broker(body)(tmp_path))


def test_heartbeat_loop_only_when_on(tmp_path, monkeypatch):
    from walter.config import Config
    monkeypatch.setenv("WALTER_DATA", str(tmp_path))
    sent = []

    async def fake_hb(self):
        sent.append(1)

    monkeypatch.setattr(CL.CloudLink, "heartbeat", fake_hb)
    cfg = Config()

    async def go():
        t = asyncio.create_task(CL.heartbeat_loop(cfg, first_delay=0))
        await asyncio.sleep(0.05)
        assert sent == []                                    # off: nothing sent
        t.cancel()
        CL.CloudLink(paths=CL.Paths(data=tmp_path)).save_state({"enabled": True, "last_heartbeat": 0})
        t = asyncio.create_task(CL.heartbeat_loop(cfg, first_delay=0))
        await asyncio.sleep(0.05)
        t.cancel()
        assert sent == [1]
    asyncio.run(go())


# ------------------------------------------------------------ the app endpoint
def test_app_endpoint_auth_confirm_and_remote_warning(tmp_path, monkeypatch):
    from tests.test_app import PROXY, make_app
    monkeypatch.setenv("WALTER_DATA", str(tmp_path / "data"))
    import walter.dashboard as dash
    monkeypatch.setattr(dash, "FACE_DIR", tmp_path / "face")

    async def go():
        b = Broker()
        async with TestServer(b.app()) as srv:
            e = Env(tmp_path, str(srv.make_url("")).rstrip("/"))
            app, apphub, cond, hub = make_app(tmp_path)
            apphub.cloud_link._make = lambda: e.cl
            async with TestClient(TestServer(app)) as c:
                assert (await c.get("/api/app/cloud-link", headers=PROXY)).status == 401
                assert (await c.post("/api/app/cloud-link", json={"action": "rotate"}, headers=PROXY)).status == 401
                code = apphub.store.start_pairing()["code"]
                token = (await (await c.post("/api/app/pair", json={"code": code, "name": "Phone"},
                                             headers=PROXY)).json())["token"]
                auth = {**PROXY, "Authorization": f"Bearer {token}"}
                j = await (await c.get("/api/app/cloud-link", headers=auth)).json()
                assert j["enabled"] is False and "api_base" not in j
                r = await c.post("/api/app/cloud-link", json={"action": "enable", "code": "wrongcode1"}, headers=auth)
                assert r.status == 400 and (await r.json())["code"] == "invalid_code"
                r = await c.post("/api/app/cloud-link", json={"action": "enable", "code": "goodcode42"}, headers=auth)
                assert r.status == 200 and (await r.json())["host"] == HOST
                # through the link itself: off needs a yes, and says you'll lose remote access
                via = {**auth, "Host": HOST, "X-Forwarded-For": "203.0.113.9"}
                j = await (await c.get("/api/app/cloud-link", headers=via)).json()
                assert j["enabled"] and j["via_link"] is True
                r = await c.post("/api/app/cloud-link", json={"action": "disable"}, headers=via)
                j = await r.json()
                assert r.status == 409 and j["needs_confirm"] and "lose remote access" in j["warning"] and j["via_link"]
                assert e.cl.enabled
                r = await c.post("/api/app/cloud-link", json={"action": "disable", "confirm": "yes"}, headers=via)
                assert r.status == 409 and e.cl.enabled                   # only a real true
                r = await c.post("/api/app/cloud-link", json={"action": "disable", "confirm": True}, headers=via)
                assert r.status == 200 and (await r.json())["released"] and not e.cl.enabled
                assert (await c.post("/api/app/cloud-link", json={"action": "nope"}, headers=auth)).status == 400
                # enable attempts are rate-limited
                codes = []
                for _ in range(5):
                    codes.append((await c.post("/api/app/cloud-link", json={"action": "enable", "code": "zzzzzz9"},
                                               headers=auth)).status)
                assert codes[-1] == 429
    asyncio.run(go())


def test_warning_wording():
    from walter import app_cloud_link as A
    assert "lose remote access" in A.warning(True) and "lose remote access" in A.warning(False)
