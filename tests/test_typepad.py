"""Phone typing page (home/skills/typepad.py): validation, and the typed text never comes back."""
import importlib.util
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "home" / "skills"))
spec = importlib.util.spec_from_file_location("typepad", REPO / "home" / "skills" / "typepad.py")
tp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tp)
bl = tp.live

SECRET = "Pw-should-never-echo-91"


def fake_browser(sent):
    def send(cmd, timeout=60):
        sent.append(cmd)
        return {"ok": True, "msg": "typed %d characters into the password box" % len(cmd["args"].get("text") or ""),
                "title": "Sign in", "url": "https://accounts.example.test/signin?token=abc", "field": "the password box"}
    return send


def test_type_passes_text_but_never_returns_it():
    sent = []
    res = tp.handle_action({"action": "type", "kind": "password", "text": SECRET, "enter": True}, fake_browser(sent))
    assert sent[0]["cmd"] == "pad" and sent[0]["args"]["text"] == SECRET and sent[0]["args"]["enter"]
    assert SECRET not in json.dumps(res)
    assert res["url"] == "accounts.example.test/signin"          # no query string on the phone either


def test_validation():
    sent = []
    send = fake_browser(sent)
    assert not tp.handle_action({"action": "type", "text": ""}, send)["ok"]
    assert not tp.handle_action({"action": "type", "text": "x" * 501}, send)["ok"]
    assert not tp.handle_action({"action": "key", "key": "Control+W"}, send)["ok"]
    assert not tp.handle_action({"action": "eval"}, send)["ok"]
    assert sent == []
    tp.handle_action({"action": "type", "kind": "weird", "text": "a"}, send)
    assert sent[-1]["args"]["kind"] == "text"
    tp.handle_action({"action": "type", "kind": "code", "text": " 123 456 "}, send)
    assert sent[-1]["args"]["text"] == "123 456"
    tp.handle_action({"action": "key", "key": "Next field"}, send)
    assert sent[-1]["args"] == {"action": "key", "key": "Next field"}


def test_browser_down_is_a_plain_message(monkeypatch, tmp_path):
    monkeypatch.setattr(bl, "SOCK", tmp_path / "nope.sock")
    res = tp.handle_action({"action": "type", "text": SECRET})
    assert not res["ok"] and SECRET not in json.dumps(res)


def test_field_label_never_shows_values():
    assert bl.field_label(None) == "no box"
    assert bl.field_label({"editable": True, "secret": True, "label": "Enter your password"}) == "the password box"
    assert bl.field_label({"editable": True, "label": "Email or phone"}) == 'the "Email or phone" box'
    assert ".value" not in bl.FIELD_JS


def test_server_secret_path_one_device_and_done(monkeypatch):
    monkeypatch.setattr(tp, "handle_action", lambda body, send=None: {"ok": True, "msg": "fine"})
    box = {}
    th = threading.Thread(target=lambda: box.setdefault("done", tp.serve(
        1, 0, show_qr=False, on_ready=lambda u, s: box.update(url=u.replace(u.split("/")[2].split(":")[0], "127.0.0.1")))),
        daemon=True)
    th.start()
    for _ in range(50):
        if "url" in box:
            break
        time.sleep(0.05)
    url = box["url"]
    api = url.rstrip("/") + "/api"
    page = urllib.request.urlopen(url).read().decode()
    assert "Type password" in page and "type=password" in page and "__ENDS__" not in page
    try:
        urllib.request.urlopen(url.rsplit("/", 2)[0] + "/wrong/")
        assert False
    except urllib.error.HTTPError as e:
        assert e.code == 404

    def post(body, cookie=None):
        h = {"Content-Type": "application/json"}
        if cookie:
            h["Cookie"] = cookie
        try:
            r = urllib.request.urlopen(urllib.request.Request(api, json.dumps(body).encode(), h))
            return r.status, r.headers.get("Set-Cookie"), json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, None, None

    code, cookie, res = post({"action": "peek"})
    assert code == 200 and cookie and res["ok"]
    cookie = cookie.split(";")[0]
    assert post({"action": "peek"})[0] == 403                     # a second device is refused
    assert post({"action": "peek"}, cookie)[0] == 200
    code, _, res = post({"action": "done"}, cookie)
    assert res["closed"]
    th.join(5)
    assert not th.is_alive() and box["done"] is True
