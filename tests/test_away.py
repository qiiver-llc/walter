"""'While you were away' feed, typed dashboard commands, parked ideas, wall cards and
Web Push. Never touches ~/.walter, the real face files or the network (conftest
points the away feed, card and push keys at tmp_path)."""
import asyncio
import json
import os
import time
from unittest.mock import MagicMock

import pytest

from walter import dashboard as D
from walter import devices as DV
from walter.webpush import WebPush


# ---------------------------------------------------------------- the feed
def test_add_away_stores_privately_caps_and_mirrors(tmp_path, monkeypatch):
    feed = D.AwayFeed()
    monkeypatch.setattr(D, "AWAY_MAX", 5)
    for n in range(7):
        D.add_away(f"Reminder {n}", "call the vet", kind="reminder")
    items = feed.items()
    assert [i["title"] for i in items] == [f"Reminder {n}" for n in range(2, 7)]
    assert all(not i["seen"] and i["t"] > 0 for i in items)
    assert oct(os.stat(feed.path).st_mode & 0o777) == "0o600"
    face = json.loads(feed.face_out.read_text())
    assert face["unseen"] == 5
    assert face["items"][0]["title"] == "Reminder 6"          # newest first on the dashboard
    assert len(face["items"]) <= D.AWAY_FACE_MAX
    assert os.stat(feed.face_out).st_mtime == D.FIXED_MTIME   # never reloads the face


def test_mark_seen_and_clear():
    feed = D.AwayFeed()
    a = D.add_away("One")
    D.add_away("Two")
    assert feed.mark_seen([a["id"]]) == 1
    assert [i["title"] for i in feed.unseen()] == ["Two"]
    assert feed.mark_seen() == 1 and feed.unseen() == []
    face = json.loads(feed.face_out.read_text())
    assert face["unseen"] == 0 and len(face["items"]) == 2   # seen ones stay (dimmed) for a while
    assert feed.clear() == 2 and feed.items() == []


def test_hooks_get_the_item_and_never_lose_it():
    got = []
    D.on_away(lambda it: got.append(it))
    D.on_away(lambda it: 1 / 0)                  # a broken hook must not lose the entry
    it = D.add_away("Task #3 done", "built it", kind="task", push=False)
    assert got and got[0]["title"] == "Task #3 done" and got[0]["push"] is False
    assert D.AwayFeed().items()[0]["id"] == it["id"]


def test_card_body_and_format():
    D.add_away("Reminder", "the tea is ready")
    D.add_away("Task #4 done", "x" * 300)
    items = D.AwayFeed().unseen()
    body = D.away_card_body(items)
    first = body.splitlines()[0]
    assert "Task #4 done: xxx" in first and first.endswith("…") and len(first) < 140
    assert "Reminder: the tea is ready" in body
    assert "* #1" in D.format_away(items)
    assert D.format_away([]).startswith("(nothing")


def test_show_card_goes_to_the_test_path(tmp_path):
    c = D.show_card("Added to To do", "eggs", 5)
    data = json.loads(D.CARD_OUT.read_text())
    assert data["title"] == "Added to To do" and data["body"] == "eggs" and data["until"] > data["id"]
    assert c["id"] == data["id"]


# ---------------------------------------------------------------- the list
@pytest.mark.parametrize("text,want", [
    ("wake", "wake"), ("wake up", "wake"), ("Walter wake up", "wake"), ("walter, wake up!", "wake"),
    ("hey walter", "wake"), ("WAKEUP", "wake"), ("sleep", "sleep"), ("go to sleep", "sleep"),
    ("mute", "mute"), ("unmute", "unmute"), ("un-mute please", "unmute"),
    ("wake up early tomorrow", None), ("buy milk", None), ("mute the tv", None), ("", None),
    ("sleep study appointment", None),
])
def test_parse_command(text, want):
    assert D.parse_command(text) == want


def test_ideas_section(tmp_path):
    st = D.ListStore(tmp_path / "d.json", tmp_path / "f.json")
    it = st.add("Research local models", "ideas")
    assert it["section"] == "ideas"
    assert st.add("x", "idea")["section"] == "ideas"
    assert D.parse_quick("idea: use the closet laptops") == ("ideas", "use the closet laptops")
    assert D.parse_quick("parked try a new voice") == ("ideas", "try a new voice")


# ---------------------------------------------------------------- web push
def fake_sub(n=1):
    return {"endpoint": f"https://push.example.com/s/{n}", "keys": {"p256dh": "B" * 87, "auth": "a" * 22}}


def test_push_keys_subscriptions_and_gone(tmp_path):
    sent = []

    def sender(sub, payload):
        sent.append((sub["endpoint"], json.loads(payload)))
        return 410 if sub["endpoint"].endswith("/2") else 201
    wp = WebPush(base=tmp_path / "w", sender=sender)
    key = wp.public_key()
    assert len(key) == 87 and wp.public_key() == key            # stable once made
    assert oct(os.stat(wp.key_path).st_mode & 0o777) == "0o600"
    with pytest.raises(ValueError):
        wp.subscribe("dev1", {"endpoint": "http://insecure", "keys": {}})
    wp.subscribe("dev1", fake_sub(1))
    wp.subscribe("dev1", fake_sub(1))                            # same endpoint: once
    wp.subscribe("dev2", fake_sub(2))
    assert wp.count() == 2 and wp.count("dev1") == 1
    assert oct(os.stat(wp.subs_path).st_mode & 0o777) == "0o600"
    assert wp.send_now("Reminder", "tea", tag="reminder") == 1
    assert sent[0][1]["title"] == "Reminder" and sent[0][1]["body"] == "tea"
    assert wp.count() == 1                                       # the 410 one is gone
    assert wp.prune(["other"]) == 1 and wp.count() == 0


def test_push_without_subscriptions_is_a_no_op(tmp_path):
    wp = WebPush(base=tmp_path / "w", sender=lambda s, p: 201)
    assert wp.send("x") is None and wp.send_now("x") == 0


# ---------------------------------------------------------------- app routes
def make_app(tmp_path, monkeypatch, conductor=None):
    from walter.app_server import AppHub
    from walter.config import Config
    from walter.server import Hub, build_app
    monkeypatch.setenv("WALTER_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(D, "FACE_DIR", tmp_path / "face")
    cfg = Config()
    cfg.brain.home = str(tmp_path / "home")
    (tmp_path / "home").mkdir(exist_ok=True)
    conductor = conductor or MagicMock(state="idle", sleeping=False, user_muted=False, busy=False, speech=None)
    apphub = AppHub(cfg, conductor, DV.DeviceStore(tmp_path / "data" / "devices.json"))
    app = build_app(cfg, Hub(), conductor, MagicMock(items=[]), MagicMock(), MagicMock(items=[]),
                    MagicMock(), MagicMock(), apphub)
    return app, apphub, conductor


PROXY = {"Host": "walter.example.ts.net", "X-Forwarded-For": "100.64.0.9"}


def test_app_away_and_push_routes(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        app, apphub, _ = make_app(tmp_path, monkeypatch)
        sent = []
        apphub.push._sender = lambda sub, payload: sent.append(json.loads(payload)) or 201
        async with TestClient(TestServer(app)) as c:
            code = apphub.store.start_pairing()["code"]
            r = await c.post("/api/app/pair", json={"code": code, "name": "Phone"}, headers=PROXY)
            auth = {**PROXY, "Authorization": "Bearer " + (await r.json())["token"]}
            assert (await c.get("/api/app/away", headers=PROXY)).status == 401
            D.add_away("Reminder", "tea", push=False)
            D.add_away("Task #9 done", "it worked")
            j = await (await c.get("/api/app/away", headers=auth)).json()
            assert j["unseen"] == 2 and j["items"][0]["title"] == "Task #9 done"
            assert (await (await c.get("/api/app/me", headers=auth)).json())["away_unseen"] == 2
            r = await c.post("/api/app/away", json={"action": "seen", "ids": [j["items"][1]["id"]]}, headers=auth)
            assert (await r.json())["unseen"] == 1
            r = await c.post("/api/app/away", json={"action": "seen", "ids": "all"}, headers=auth)
            assert r.status == 400
            await c.post("/api/app/away", json={"action": "clear"}, headers=auth)
            assert (await (await c.get("/api/app/away", headers=auth)).json())["items"] == []
            # push: key, subscribe, test, unsubscribe
            info = await (await c.get("/api/app/push", headers=auth)).json()
            assert info["available"] in (True, False) and info["subscribed"] == 0
            r = await c.post("/api/app/push", json={"action": "subscribe", "subscription": {"endpoint": "x"}},
                             headers=auth)
            assert r.status == 400
            r = await c.post("/api/app/push", json={"action": "subscribe", "subscription": fake_sub()}, headers=auth)
            assert (await r.json())["subscribed"] == 1
            r = await c.post("/api/app/push", json={"action": "test"}, headers=auth)
            assert (await r.json())["delivered"] == 1 and sent[-1]["tag"] == "test"
            # an away entry with push=True reaches the phone; a reminder toast with push too
            D.add_away("Held request", "the answer")
            apphub.notify("reminder", "call the vet", "Reminder", push=True)
            for _ in range(50):
                if len(sent) >= 3:
                    break
                await asyncio.sleep(0.02)
            assert {s["title"] for s in sent[1:]} == {"Held request", "Reminder"}
            r = await c.post("/api/app/push", json={"action": "unsubscribe"}, headers=auth)
            assert (await r.json())["subscribed"] == 0
            # revoking the device drops its subscriptions
            await c.post("/api/app/push", json={"action": "subscribe", "subscription": fake_sub(5)}, headers=auth)
            apphub.store.revoke("Phone")
            apphub.kick_revoked()
            assert apphub.push.count() == 0
    asyncio.run(go())


# ---------------------------------------------------------------- the conductor
def conductor():
    from test_barge_in import make
    c, sent = make("aec")
    c.app = MagicMock()
    return c, sent


def test_unheard():
    from types import SimpleNamespace
    c, _ = conductor()
    assert not c.unheard()
    c.user_muted = True
    assert c.unheard()
    c.user_muted = False
    c.presence = SimpleNamespace(enabled=True, tracker=SimpleNamespace(empty=True))
    assert c.unheard()
    c.presence.tracker.empty = False
    assert not c.unheard()
    c.sleeping = True
    assert c.unheard()


def test_reminder_while_muted_goes_on_the_wall_feed_and_phone(tmp_path):
    from walter.conductor import Reminders

    async def go():
        c, _ = conductor()
        c.user_muted = True
        r = Reminders(tmp_path / "rem.json", c)
        r.add(time.time() - 1, "Get the network application done")
        task = asyncio.ensure_future(r.run())
        for _ in range(80):
            if not r.items:
                break
            await asyncio.sleep(0.1)
        task.cancel()
        assert c.inbox.get_nowait()[0] == "reminder"
        card = json.loads(D.CARD_OUT.read_text())
        assert card["title"] == "Reminder" and "network application" in card["body"]
        assert [i["kind"] for i in D.AwayFeed().unseen()] == ["reminder"]
        c.app.notify.assert_called_with("reminder", "Get the network application done", "Reminder", push=True)
    asyncio.run(go())


def test_reminder_when_heard_is_only_spoken_and_pushed(tmp_path):
    from walter.conductor import Reminders

    async def go():
        c, _ = conductor()
        r = Reminders(tmp_path / "rem.json", c)
        r.add(time.time() - 1, "tea")
        task = asyncio.ensure_future(r.run())
        for _ in range(80):
            if not r.items:
                break
            await asyncio.sleep(0.1)
        task.cancel()
        assert D.AwayFeed().items() == [] and not D.CARD_OUT.exists()
        assert c.app.notify.call_args.kwargs["push"] is True
    asyncio.run(go())


def test_finished_worker_lands_away_only_when_unheard():
    c, _ = conductor()
    c.notify_app("task", "all good", "Task #7 done: build")
    assert D.AwayFeed().items() == []
    assert c.app.notify.call_args.kwargs["push"] is False
    c.sleeping = True
    c.notify_app("task", "all good", "Task #8 done: build")
    assert [i["title"] for i in D.AwayFeed().items()] == ["Task #8 done: build"]
    assert c.app.notify.call_args.kwargs["push"] is True


def test_welcome_back_card_once_and_marks_seen():
    c, _ = conductor()
    c.welcome_back("unmuted")
    assert not D.CARD_OUT.exists()            # nothing missed: no card
    D.add_away("Reminder", "tea")
    D.add_away("Task #2 done", "ok")
    c.welcome_back("unmuted")
    card = json.loads(D.CARD_OUT.read_text())
    assert card["title"] == "While you were away: 2 things" and "tea" in card["body"]
    assert D.AwayFeed().unseen() == []
    c.app.away_changed.assert_called()


def test_routine_said_while_muted_is_kept():
    c, _ = conductor()
    c._routine_away({"said": ["[happy] Morning! It's sunny."]})
    assert D.AwayFeed().items() == []         # heard: nothing to keep
    c.user_muted = True
    c._routine_away({"said": ["[happy] Morning! It's sunny."]})
    assert D.AwayFeed().items()[0]["body"] == "Morning! It's sunny."
    c._routine_away({"said": []})             # silent routine: nothing
    assert len(D.AwayFeed().items()) == 1


def test_add_away_dedupes_the_same_thing_twice():
    a = D.add_away("Reminder", "tea")
    b = D.add_away("Reminder", "tea")
    assert a["id"] == b["id"] and len(D.AwayFeed().items()) == 1


def test_typed_commands_on_the_dashboard_route(tmp_path, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        cond = MagicMock(state="sleeping", sleeping=True, user_muted=True, busy=False, speech=None)
        app, _, _ = make_app(tmp_path, monkeypatch, conductor=cond)
        async with TestClient(TestServer(app)) as c:
            for text, want in (("walter wake up", "wake"), ("sleep", "sleep"), ("mute", "mute")):
                r = await c.post("/api/dashboard", json={"action": "add", "text": text, "quick": True},
                                 headers={"Content-Type": "application/json"})
                j = await r.json()
                assert j["command"] == want and j["items"] == [], text
            cond.set_user_mute.assert_any_call(False)
            cond.go_to_sleep.assert_called_once()
            cond.set_user_mute.assert_called_with(True)
            r = await c.post("/api/dashboard", json={"action": "add", "text": "wake up early", "quick": True},
                             headers={"Content-Type": "application/json"})
            assert (await r.json())["item"]["text"] == "wake up early"
            r = await c.post("/api/mute", json={"on": False})
            cond.welcome_back.assert_called()
    asyncio.run(go())


# ---------------------------------------------------------------- evening wrap-up
def test_wrapup_build():
    import datetime as dt
    import runpy
    from pathlib import Path
    mod = runpy.run_path(str(Path(__file__).resolve().parent.parent / "bin" / "walter-wrapup"))
    now = dt.datetime(2026, 11, 5, 20, 30)
    t = now.timestamp()
    items = [{"id": 1, "text": "Pay the water bill", "section": "tasks", "done_at": t - 3600},
             {"id": 2, "text": "Old thing", "section": "tasks", "done_at": t - 86400 - 60},
             {"id": 3, "text": "Milk", "section": "shopping"},
             {"id": 4, "text": "Gate code changed", "section": "notes"},
             {"id": 5, "text": "Local models", "section": "ideas"}]
    evs = [{"title": "Football", "time": "All day"}, {"title": "Standup", "time": "9:00 AM"}]
    w = mod["build"](now, items, evs)
    assert w["done"] == 1 and w["open"] == 1
    assert w["lines"][0] == "Done today: Pay the water bill"
    assert w["lines"][1] == "Still open: Milk"
    assert w["lines"][2] == "Tomorrow: All day Football; 9:00 AM Standup"
    assert w["lines"][3] == "Parked ideas: Local models"
    assert "Standup at 9:00 AM" in w["sentence"] and w["sentence"].count(".") <= 3
    empty = mod["build"](now, [], [])
    assert "All clear" in empty["lines"][1] and "nothing on the calendar" in empty["lines"][2]
