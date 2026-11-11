"""Watchable browser (home/skills/browse_live.py): helpers, the face control file,
and the token-guarded stream/events server. No real browser is started here."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("browse_live", REPO / "home" / "skills" / "browse_live.py")
bl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bl)


def test_selector_or_words():
    for sel in ("#q", ".btn", "input[name=q]", "text=Next", "//a[1]", "a.more", "button", "css=div > a"):
        assert bl.looks_like_selector(sel), sel
    for words in ("More news", "Next page", "Weather", "Sandhill crane", "Sign in"):
        assert not bl.looks_like_selector(words), words


def test_risky_buttons_need_a_yes():
    for label in ("Buy now", "Place your order", "Proceed to checkout", "Send", "I agree", "Accept all cookies",
                  "Delete account", "Subscribe", "Pay $12.99", "Submit"):
        assert bl.is_risky(label), label
    for label in ("Search", "Next", "Weather radar", "Read more", "Show forecast", "Sandhill crane"):
        assert not bl.is_risky(label), label


def test_display_url_hides_query_and_fragment():
    assert bl.display_url("https://example.com/") == "example.com"
    assert bl.display_url("https://shop.example.com/a/b?token=SECRET#x") == "shop.example.com/a/b"
    assert "SECRET" not in bl.display_url("https://x.test/?session=SECRET")
    assert bl.display_url("about:blank") == "about:blank"


def test_normalise_url():
    assert bl.normalise_url("example.com") == "https://example.com"
    assert bl.normalise_url("http://example.com/x") == "http://example.com/x"
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "chrome://settings"):
        with pytest.raises(ValueError):
            bl.normalise_url(bad)


def test_pages_cannot_reach_this_machine():
    for url in ("http://127.0.0.1:8765/api/config", "http://localhost:8765/", "http://[::1]/",
                "http://0.0.0.0:8771/stream.mjpg", "http://169.254.169.254/latest/", "http://app.localhost/"):
        assert bl.blocked_host(url), url
    for url in ("https://example.com/", "https://en.wikipedia.org/wiki/Ohio", "http://192.168.1.1/"):
        assert not bl.blocked_host(url), url


def test_cors_only_for_the_face():
    assert bl.allowed_origin("http://127.0.0.1:8765") == "http://127.0.0.1:8765"
    assert bl.allowed_origin("http://localhost:8765") == "http://localhost:8765"
    assert bl.allowed_origin("https://evil.example") == ""
    assert bl.allowed_origin("http://127.0.0.1.evil.example") == ""


def test_control_file_has_fixed_old_mtime(tmp_path):
    face = tmp_path / "face"
    face.mkdir()
    p = face / "browser.json"
    bl.write_ctrl({"id": "1", "open": True}, p)
    assert json.loads(p.read_text()) == {"id": "1", "open": True}
    assert p.stat().st_mtime == bl.FIXED_MTIME       # the face's auto-reload ignores it
    assert not list(face.glob("*.tmp"))              # temp file was outside face/
    assert bl.read_ctrl(p)["open"] is True


def test_daemon_args_disable_back_forward_cache():
    out = bl.daemon_args(["--mute-audio", "--disable-features=Translate,MediaRouter"])
    assert "--disable-features=Translate,MediaRouter,BackForwardCache" in out
    assert bl.daemon_args(["--mute-audio"])[-1] == "--disable-features=BackForwardCache"


def test_daemon_args_let_the_wall_browser_play_sound():
    out = bl.daemon_args(["--mute-audio", "--no-first-run"])
    assert "--mute-audio" not in out and "--no-first-run" in out
    assert "--autoplay-policy=no-user-gesture-required" in out


def test_media_actions_are_a_fixed_list():
    assert "pause" in bl.MEDIA_ACTIONS and "volume" in bl.MEDIA_ACTIONS
    assert "f" in bl.MEDIA_KEYS and "Enter" not in bl.MEDIA_KEYS


def test_parse_request_head():
    m, path, q, h = bl.parse_request_head(b"GET /events?t=abc HTTP/1.1\r\nOrigin: http://127.0.0.1:8765\r\n\r\n")
    assert (m, path, q["t"], h["origin"]) == ("GET", "/events", "abc", "http://127.0.0.1:8765")


def test_clean_text():
    assert bl.clean_text("a  \t b\n\n\n\nc", 100) == "a b\n\nc"
    assert bl.clean_text("x" * 50, 10).startswith("x" * 10 + "\n[... truncated")


async def _get(port, path, origin="http://127.0.0.1:8765", nbytes=4096):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"GET {path} HTTP/1.1\r\nHost: x\r\nOrigin: {origin}\r\n\r\n".encode())
    await w.drain()
    data = b""
    try:
        while len(data) < nbytes:
            chunk = await asyncio.wait_for(r.read(nbytes), 2)
            if not chunk:
                break
            data += chunk
            if b"\r\n\r\n" in data and (b"--wframe" in data and data.endswith(b"\r\n") or b"data:" in data
                                         or b"403" in data.split(b"\r\n")[0]):
                break
    except asyncio.TimeoutError:
        pass
    w.close()
    return data


def test_stream_and_events_need_the_token():
    async def go():
        live = bl.Live(None, "", [], Path("/nonexistent"))
        live.frame, live.frame_seq = b"\xff\xd8JPEGDATA\xff\xd9", 1
        srv = await asyncio.start_server(live.http, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            assert b"403" in (await _get(port, "/stream.mjpg?t=wrong")).split(b"\r\n")[0]
            assert b"403" in (await _get(port, "/events")).split(b"\r\n")[0]
            mj = await _get(port, f"/stream.mjpg?t={live.token}")
            assert b"multipart/x-mixed-replace" in mj and b"JPEGDATA" in mj
            ev = await _get(port, f"/events?t={live.token}")
            assert b"Access-Control-Allow-Origin: http://127.0.0.1:8765" in ev
            body = ev.split(b"data: ", 1)[1].split(b"\n\n")[0]
            assert json.loads(body)["type"] == "state"
            ev2 = await _get(port, f"/events?t={live.token}", origin="https://evil.example")
            assert b"Access-Control-Allow-Origin" not in ev2
            await asyncio.sleep(0.2)
            assert live.viewers == 0                  # the stream noticed the viewer left
        finally:
            srv.close()
    asyncio.run(go())


def test_face_loads_the_browser_panel():
    html = (REPO / "face" / "index.html").read_text()
    assert '<script src="browser.js"></script>' in html
    js = (REPO / "face" / "browser.js").read_text()
    assert "stream.mjpg" in js and "Escape" in js and "textContent" in js and "innerHTML = d" not in js
