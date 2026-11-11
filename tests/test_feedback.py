"""Beta feedback (walter/feedback.py) and the diagnostics bundle (walter/diagnostics.py).
All sample data is made up; nothing here reaches the network."""
import asyncio
import json
import os

import pytest

from walter import diagnostics as G
from walter import feedback as F
from tests.test_app import PROXY, iso, make_app  # noqa: F401  (iso is a fixture)

WORDS = ["Bramblewick", "Quentessa", "Fernhollow"]
RED = G.Redactor(WORDS, home="/home/alice", user="alice")


# ------------------------------------------------------------------ redaction
@pytest.mark.parametrize("secret", [
    "contact zed.q@example.org now", "token=abcdef1234567890XYZ", "Authorization: Bearer abc.def.ghi",
    "key sk-" "proj-AbCdEf0123456789abcdef", "ghp_0123456789abcdefABCDEF0123", "github_pat_" "11ABCDEFG0123456789_xyz",  # split: keep scanners quiet
    "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123def456", "peer 192.168.1.50:8765",
    "v6 fe80:0:0:0:1ff:fe23:4567:890a", "sha 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "blob QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0123==", "open https://walter.tail99.ts.net/app?x=1",
    "file /home/alice/.walter/secrets/openai.key", "mac 02:00:5e:01:aa:9e", "call 212-555-0199",
])
def test_redactor_removes_secrets(secret):
    out = RED(secret)
    for bit in ("zed.q@", "abcdef1234567890XYZ", "abc.def.ghi", "sk-proj", "ghp_0123", "github_pat", "eyJhbGci",
                "192.168.1.50", "fe80:0:0", "9f86d081", "QUJDREVG", "tail99", "openai.key", "02:00:5e", "555-0199"):
        assert bit not in out, (secret, out)


def test_private_words_home_and_user_never_leak():
    out = RED("Bramblewick said hi to quentessa at /home/alice/walter/x.py in FERNHOLLOW")
    assert "Bramblewick" not in out and "quentessa" not in out.lower() and "fernhollow" not in out.lower()
    assert "/home/alice" not in out and "~/walter/x.py" in out
    # words inside other words are left alone; technical lines survive
    assert RED("walter.ears: whisper 1.6 s at 10:43:56,629") == "walter.ears: whisper 1.6 s at 10:43:56,629"


def test_log_lines_lose_what_people_said():
    lines = ["2026-01-01 INFO walter.ears: heard (1.6s): Hey there, call Quentessa about the vet",
             "2026-01-01 INFO walter.conductor: ack: 'Bear with me.'",
             "2026-01-01 ERROR walter.tasks: task 7 failed: please book the dentist for tomorrow morning",
             "2026-01-01 INFO walter.voice: reply voice: local"]
    log, errors = G.log_sections(lines, RED)
    joined = "\n".join(log)
    for bit in ("Hey there", "Quentessa", "Bear with me", "dentist"):
        assert bit not in joined
    assert "heard (1.6s): [text]" in joined and log[-1].endswith("reply voice: local")
    assert len(errors) == 1 and "ERROR" in errors[0] and "dentist" not in errors[0]


def test_config_allow_list():
    raw = {"brain": {"model": "sonnet", "home": "/home/alice/.walter/home", "max_tasks": 3,
                     "extra_args": ["--x"]},
           "dashboard": {"lat": 47.61, "lon": -122.33, "list_title": "Quentessa's list", "enabled": True,
                         "sports_teams": ["Somewhere High"]},
           "app": {"public_url": "https://walter.tail99.ts.net", "allow_lan": True},
           "speech": {"tts_voice": "ash", "tts_instructions": "Talk like Bramblewick", "key_file": "/x"},
           "mystery": {"anything": "zed.q@example.org"}, "ears": {"device": "zed.q@example.org"}}
    out = G.redacted_config(raw, RED)
    s = json.dumps(out)
    for bit in ("47.61", "-122", "Quentessa", "tail99", "Bramblewick", "/home/alice", "example.org", "Somewhere",
                "mystery", "--x"):
        assert bit not in s, bit
    assert out["brain"] == {"model": "sonnet", "max_tasks": 3}
    assert out["dashboard"] == {"enabled": True} and out["app"] == {"allow_lan": True}
    assert out["speech"] == {"tts_voice": "ash"} and out["ears"]["device"] == "[redacted]"
    assert out["_omitted_keys"] == 10


def test_bundle_contents(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "private-words.txt").write_text("\n".join(WORDS) + "\n")
    (data / "tasks.json").write_text(json.dumps([
        {"id": 1, "title": "Email Quentessa the plan", "status": "done", "started": 100, "finished": 400},
        {"id": 2, "title": "secret project", "status": "running", "started": 500}]))
    b = G.build(data_dir=data, raw_config={"brain": {"model": "opus", "home": "/somewhere/Fernhollow"}},
                log_lines=["INFO walter.ears: heard (2s): ask Bramblewick", "ERROR walter.x: boom token=abc123456789"])
    assert set(b) == {"kind", "schema", "created", "version", "system", "audio", "service", "config", "tasks",
                      "errors", "log"}
    s = json.dumps(b)
    for bit in ("Quentessa", "Bramblewick", "Fernhollow", "secret project", "abc123456789", "Email"):
        assert bit not in s, bit
    assert b["tasks"]["counts"] == {"done": 1, "running": 1}
    assert b["tasks"]["recent"][0] == {"id": 1, "status": "done", "minutes": 5.0}
    assert b["config"]["brain"] == {"model": "opus"} and b["version"]["app"]
    assert len(b["log"]) == 2 and len(b["errors"]) == 1
    assert any("Settings" in x for x in G.summary(b))


def test_cli_out_is_private(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(G, "build", lambda: {"kind": "walter-diagnostics"})
    out = tmp_path / "d.json"
    assert G.main(["--out", str(out)]) == 0
    assert oct(os.stat(out).st_mode & 0o777) == "0o600"
    assert json.loads(out.read_text())["kind"] == "walter-diagnostics"
    assert "Nothing was sent" in capsys.readouterr().out


# ------------------------------------------------------------------ store
def test_store_limits_and_mode(tmp_path):
    st = F.FeedbackStore(tmp_path / "fb")
    with pytest.raises(F.FeedbackError):
        st.add("rant", "hi")
    with pytest.raises(F.FeedbackError):
        st.add("bug", "   ")
    with pytest.raises(F.FeedbackError) as e:
        st.add("bug", "x" * 2001)
    assert e.value.status == 413
    r = st.add("bug", "mic\x00 died", now=1_800_000_000)
    assert r["text"] == "mic died" and r["status"] == "saved"
    p = tmp_path / "fb" / f"{r['id']}.json"
    assert oct(os.stat(p).st_mode & 0o777) == "0o600" and oct(os.stat(tmp_path / "fb").st_mode & 0o777) == "0o700"
    r2 = st.add("idea", "same second", now=1_800_000_000)
    assert r2["id"] != r["id"] and st.get(r2["id"])["text"] == "same second"
    for i in range(F.MAX_PER_DAY - 2):
        st.add("praise", f"nice {i}", now=1_800_000_000)
    with pytest.raises(F.FeedbackError) as e:
        st.add("praise", "one too many", now=1_800_000_000)
    assert e.value.status == 429
    assert st.path("../../etc/passwd") is None and st.get("nope") is None


# ------------------------------------------------------------------ endpoints
def test_endpoints_auth_limits_send_and_download(iso, monkeypatch):  # noqa: F811
    from aiohttp.test_utils import TestClient, TestServer
    import walter.diagnostics as diag
    monkeypatch.setattr(diag, "build", lambda *a, **k: {"kind": "walter-diagnostics", "version": {"app": "9"},
                                                         "log": ["fine"], "errors": [], "config": {}})
    sent = []

    def fake_post(url, body, timeout=15):
        sent.append((url, body))
        return True, "HTTP 200"
    monkeypatch.setattr(F, "post_json", fake_post)

    async def go():
        app, apphub, cond, hub = make_app(iso)
        async with TestClient(TestServer(app)) as c:
            for m, path in (("get", "/api/app/feedback"), ("get", "/api/app/feedback/diagnostics"),
                            ("post", "/api/app/feedback"), ("post", "/api/app/feedback/2026-01-01-000000/send"),
                            ("get", "/api/app/feedback/2026-01-01-000000/download")):
                r = await getattr(c, m)(path, headers=PROXY, **({"json": {}} if m == "post" else {}))
                assert r.status == 401, path
            assert not (iso / "data" / "feedback").exists() or not list((iso / "data" / "feedback").iterdir())
            code = apphub.store.start_pairing()["code"]
            token = (await (await c.post("/api/app/pair", json={"code": code, "name": "Phone"},
                                         headers=PROXY)).json())["token"]
            auth = {**PROXY, "Authorization": f"Bearer {token}"}
            # limits
            r = await c.post("/api/app/feedback", json={"type": "bug", "text": "x" * 2001}, headers=auth)
            assert r.status == 413
            r = await c.post("/api/app/feedback", json={"type": "nope", "text": "hi"}, headers=auth)
            assert r.status == 400
            r = await c.post("/api/app/feedback", json={"type": "bug", "text": "x" * 20000}, headers=auth)
            assert r.status == 413
            r = await c.post("/api/app/feedback", data="text=hi", headers={**auth, "Content-Type": "text/plain"})
            assert r.status == 415
            r = await c.post("/api/app/feedback", json={"type": "bug", "text": "hi", "image": "bogus"}, headers=auth)
            assert r.status == 400
            # preview
            j = await (await c.get("/api/app/feedback/diagnostics", headers=auth)).json()
            assert j["bundle"]["kind"] == "walter-diagnostics" and j["summary"]
            # save with diagnostics
            r = await c.post("/api/app/feedback", json={"type": "bug", "text": "Mic died", "diagnostics": True},
                             headers=auth)
            assert r.status == 200
            item = (await r.json())["item"]
            assert item["has_diagnostics"] and item["status"] == "saved" and "diagnostics" not in item
            fid = item["id"]
            f = iso / "data" / "feedback" / f"{fid}.json"
            assert oct(os.stat(f).st_mode & 0o777) == "0o600"
            lst = await (await c.get("/api/app/feedback", headers=auth)).json()
            assert [i["id"] for i in lst["items"]] == [fid] and lst["can_send"] is False
            # no endpoint: Send refuses and points at the download; nothing posted
            r = await c.post(f"/api/app/feedback/{fid}/send", json={}, headers=auth)
            assert r.status == 409 and (await r.json())["download"].endswith("/download") and not sent
            r = await c.get(f"/api/app/feedback/{fid}/download", headers=auth)
            assert r.status == 200 and "attachment" in r.headers["Content-Disposition"]
            p = json.loads(await r.read())
            assert p["text"] == "Mic died" and p["diagnostics"]["kind"] == "walter-diagnostics"
            assert (await c.get("/api/app/feedback/../../x/download", headers=auth)).status in (403, 404)
            assert (await c.get("/api/app/feedback/nope/download", headers=auth)).status == 404
            # endpoint configured: only an explicit Send posts
            apphub.cfg.feedback.endpoint = "https://example.invalid/api/feedback"
            assert not sent
            r = await c.post(f"/api/app/feedback/{fid}/send", json={}, headers=auth)
            assert r.status == 200 and (await r.json())["item"]["status"] == "sent"
            assert sent[0][0] == "https://example.invalid/api/feedback" and sent[0][1]["text"] == "Mic died"
            # without the diagnostics box, none is attached
            r = await c.post("/api/app/feedback", json={"type": "idea", "text": "Dark mode"}, headers=auth)
            fid2 = (await r.json())["item"]["id"]
            assert json.loads((iso / "data" / "feedback" / f"{fid2}.json").read_text())["diagnostics"] is None
    asyncio.run(go())


def test_post_json_refuses_plain_http():
    ok, why = F.post_json("http://example.com/x", {})
    assert not ok and "https" in why
