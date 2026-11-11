"""Setup never handles Claude credentials (beta prep step 3).

* pasted sign-in codes / sk-ant-oat tokens are refused and never stored,
* Verify only runs `claude auth status --json`,
* the owner's own Anthropic API key is stored mode 600 and never logged,
* the sign-in window is Claude Code's own, with nothing piped through Walter.
"""
import asyncio
import json
import logging
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
FAKE = REPO / "tests" / "fake_claude.py"
GOOD_KEY = "sk-ant-api03-" + "A1b2C3d4E5f6G7h8I9j0" * 3


@pytest.fixture
def env(monkeypatch, tmp_path):
    import walter.config as wc
    import walter.setup as ws
    monkeypatch.setattr(wc, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(ws, "ADMIN_REQUEST_DIR", tmp_path / "nope")
    marker = tmp_path / "logged-in"
    monkeypatch.setenv("FAKE_CLAUDE_LOGGED_OUT", str(marker))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    exe = tmp_path / "claude"
    exe.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    exe.chmod(0o755)
    cfg = SimpleNamespace(brain=SimpleNamespace(claude_bin=str(exe)),
                          server=SimpleNamespace(host="127.0.0.1", port=1, setup_port=2))
    s = ws.Setup(cfg, lambda m: None, lambda: None)
    s.needed = True
    return SimpleNamespace(ws=ws, wc=wc, s=s, marker=marker, data=tmp_path / "data", tmp=tmp_path)


def run(coro):
    return asyncio.run(coro)


def body(resp):
    return json.loads(resp.body)


def all_files(root: Path):
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


@pytest.mark.parametrize("action,payload", [
    ("code", {"code": "abc123#state456"}),
    ("code", {"code": "sk-ant-oat01-" + "x" * 40}),
    ("token", {"token": "sk-ant-oat01-" + "x" * 40}),
    ("login", {}),
])
def test_old_code_and_token_paths_are_refused(env, action, payload):
    r = run(env.s.handle(action, payload))
    assert r.status == 410 and not body(r)["ok"]
    assert "doesn't accept Claude sign-in codes or tokens" in body(r)["message"]
    assert all_files(env.data) == []  # nothing stored anywhere


@pytest.mark.parametrize("bad", ["sk-ant-oat01-" + "x" * 40, "abc123#state456", "sk-proj-123", ""])
def test_api_key_box_refuses_tokens_and_codes(env, bad):
    ok, msg = run(env.s.save_api_key(bad))
    assert not ok
    assert all_files(env.data) == []


def test_api_key_saved_600_and_never_logged(env, caplog):
    env.marker.touch()  # the (fake) CLI says it's signed in
    caplog.set_level(logging.DEBUG)
    r = run(env.s.handle("apikey", {"key": "  " + GOOD_KEY + "\n"}))
    assert body(r)["ok"], body(r)
    p = env.wc.anthropic_key_path()
    assert p.read_text() == GOOD_KEY + "\n"
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700
    assert GOOD_KEY not in caplog.text and GOOD_KEY not in r.body.decode()
    assert [f.name for f in all_files(env.data)] == ["anthropic.key"]  # no temp files
    # passed to Claude Code only through its environment
    assert env.wc.claude_env(env.s.cfg)["ANTHROPIC_API_KEY"] == GOOD_KEY


def test_old_oauth_token_in_secrets_env_is_ignored(env):
    env.data.mkdir(parents=True)
    env.wc.secrets_path().write_text("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-zzz\nANTHROPIC_API_KEY=k\n")
    e = env.wc.claude_env(env.s.cfg)
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in e or e["CLAUDE_CODE_OAUTH_TOKEN"] == os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    assert e["ANTHROPIC_API_KEY"] == "k"


def test_verify_only_asks_the_cli(env, monkeypatch):
    calls = []
    real = asyncio.create_subprocess_exec

    async def spy(*argv, **kw):
        calls.append(argv[1:])
        assert "stdin" not in kw or kw["stdin"] is None
        return await real(*argv, **kw)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    ok, msg = run(env.s.verify())
    assert not ok and "isn't signed in" in msg
    env.marker.touch()
    ok, _ = run(env.s.verify())
    assert ok
    assert calls == [("auth", "status", "--json")] * 2


def test_verify_wants_a_clear_yes(env, tmp_path):
    exe = tmp_path / "weird"
    exe.write_text("#!/bin/sh\necho not json\n")
    exe.chmod(0o755)
    env.s.cfg.brain.claude_bin = str(exe)
    ok, _ = run(env.s.verify())
    assert not ok


def test_setup_code_never_reads_claude_credentials():
    src = (REPO / "walter" / "setup.py").read_text() + (REPO / "walter" / "config.py").read_text()
    assert ".credentials" not in src and "CLAUDE_CODE_OAUTH_TOKEN=" not in src
    assert "submit_code" not in src and "start_login" not in src
    assert "stdin=asyncio.subprocess.PIPE" not in (REPO / "walter" / "setup.py").read_text()


def test_signin_window_is_claude_codes_own_and_unpiped(env, monkeypatch):
    run_dir = env.tmp / "run"
    run_dir.mkdir()
    (run_dir / "wayland-0").touch()
    bindir = env.tmp / "bin"
    bindir.mkdir()
    (bindir / "foot").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "foot").chmod(0o755)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(run_dir))
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    seen = {}

    async def fake_exec(*argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return SimpleNamespace(returncode=None, wait=lambda: asyncio.sleep(0))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    r = run(env.s.handle("signin", {}))
    assert body(r)["ok"]
    argv, kw = seen["argv"], seen["kw"]
    assert argv[0] == "foot" and "auth login" in argv[-1]
    for k in ("stdin", "stdout", "stderr"):
        assert kw[k] == asyncio.subprocess.DEVNULL
    assert kw["env"]["WAYLAND_DISPLAY"] == "wayland-0"
    assert kw["env"]["BROWSER"].endswith("deploy/signin-browser.sh")


def test_no_screen_gives_official_instructions(env, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "empty"))
    ok, msg = run(env.s.open_signin())
    assert not ok and "claude auth login" in msg


def test_pages_promise_and_have_no_code_box():
    phone = (REPO / "face" / "setup-phone.html").read_text()
    face = (REPO / "face" / "index.html").read_text()
    promise = "Walter uses your own Claude account or API key; Walter never sees your Claude password or login code."
    assert promise in phone and promise in face
    assert 'id="code"' not in phone and "setup-token" not in phone
    assert 'id="setupCode"' not in face
    js = (REPO / "face" / "setup.js").read_text()
    assert '"code"' not in js and "which=login" not in js
    assert "which=login" not in (REPO / "walter" / "server.py").read_text()
