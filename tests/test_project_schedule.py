"""Workers always carry a workspace tag, and reminders / do-later jobs / routines can belong to a project
(walter/tasks.py default tag, walter/sleeplearn.py, conductor.Reminders, walter/app_schedule.py, walterctl).
Uses the generic Acme / Vault example workspaces; never the real ~/.walter (tests/conftest.py)."""
import asyncio
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

import walter.conductor as C
import walter.sleeplearn as sl
import walter.tasks as T
import walter.workspaces as W
from walter import app_schedule as S
from walter.conductor import Conductor, Reminders
from tests.test_app import PROXY, iso, make_app  # noqa: F401  (iso is a fixture)
from tests.test_tasks import env, make, wait_for  # noqa: F401  (env is a fixture)

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def ws(example_workspaces):
    st = W.WorkspaceStore()
    st.ensure()
    return st


@pytest.fixture
def rem(tmp_path):
    return Reminders(tmp_path / "reminders.json", conductor=None)


# ---------------------------------------------------------------- (A) tagging
def test_no_workspaces_means_no_tag():
    assert T.default_workspace() is None           # single-workspace install: unchanged
    assert sl.review_workspace() is None


def test_default_tag_is_the_wall_workspace(ws):
    assert T.default_workspace() == "home"
    ws.switch("acme")
    assert T.default_workspace() == "acme"


def test_turn_project_wins_then_expires(ws):
    ws.switch("acme")
    ws.set_turn_workspace("vault")
    assert ws.task_default() == "vault" and T.default_workspace() == "vault"
    ws.set_turn_workspace("vault", ttl=-1)          # stale
    assert ws.task_default() == "acme"
    ws.set_turn_workspace("nope")                   # a project that doesn't exist is ignored
    assert ws.task_default() == "acme"
    ws.set_turn_workspace(None)
    assert not ws.turn_path.exists()


def test_tasks_without_workspace_are_tagged(env, ws):
    cfg, _ = env
    ws.switch("acme")

    async def go():
        t, finished, _ = make(cfg)
        a = await t.start("untagged job", "do a thing")
        b = await t.start("tagged job", "do a thing", workspace="vault")
        await wait_for(lambda: len(finished) == 2)
        return a, b
    a, b = asyncio.run(go())
    assert a["workspace"] == "acme" and b["workspace"] == "vault"


def test_sleep_review_belongs_to_the_walter_repo(tmp_path, example_workspaces):
    d = json.loads(json.dumps(example_workspaces))
    d["workspaces"][1]["repos"] = [str(REPO)]       # Acme owns this repo here
    st = W.WorkspaceStore()
    st.save(d)
    assert sl.review_workspace() == "acme"
    d["workspaces"][1]["repos"] = []                # nobody owns it: the default workspace
    st.save(d)
    assert sl.review_workspace() == "home"


def test_sleep_review_starts_tagged(tmp_path, ws, monkeypatch):
    from tests.test_sleeplearn import NOW, FakeTasks, learner

    class Rec(FakeTasks):
        async def start(self, title, prompt, quiet=False, model=None, workspace=None):
            self.ws = workspace
            return await super().start(title, prompt, quiet, model)
    monkeypatch.setattr(sl, "review_workspace", lambda: "acme")
    t = Rec()
    lr = learner(tmp_path, t)
    asyncio.run(lr.tick(sleeping=True, dozing=False, hour=23, now=NOW))
    assert asyncio.run(lr.tick(sleeping=True, dozing=False, hour=23, now=NOW + 700)) == "started"
    assert t.ws == "acme"


def test_walterctl_task_uses_the_routines_project(ws, monkeypatch):
    ctl = runpy.run_path(str(REPO / "bin" / "walterctl"), run_name="walterctl")
    ws.switch("home")
    assert ctl["_task_workspace"](None)[1] == "home"
    ws.set_turn_workspace("acme")
    assert ctl["_task_workspace"](None)[1] == "acme"
    assert ctl["_task_workspace"]("vault")[1] == "vault"   # an explicit --workspace still wins
    assert ctl["_schedule_workspace"](["--workspace", "Acme", "20m", "tea"]) == ({"workspace": "acme"}, ["20m", "tea"])
    assert ctl["_schedule_workspace"](["20m", "tea"]) == ({}, ["20m", "tea"])
    with pytest.raises(SystemExit):
        ctl["_schedule_workspace"](["--workspace", "nowhere", "20m", "tea"])


# ---------------------------------------------------------------- (B) reminders / routines with a project
def test_store_add_edit_clear(rem):
    r = rem.add(None, "acme standup notes", "do", {"days": [0, 1, 2, 3, 4], "time": "09:00"}, workspace="acme")
    assert rem.get(r["id"])["workspace"] == "acme"
    rem.update(r["id"], text="acme notes")             # leaving workspace alone keeps it
    assert rem.get(r["id"])["workspace"] == "acme"
    rem.update(r["id"], workspace="vault")
    assert rem.get(r["id"])["workspace"] == "vault"
    rem.update(r["id"], workspace=None)
    assert "workspace" not in rem.get(r["id"])
    g = rem.add(1e12, "global", "remind")
    assert "workspace" not in g


def _fire_once(rem, monkeypatch):
    """Run Reminders.run for exactly one pass; returns (inbox items, app notifications)."""
    calls = {"n": 0}

    async def fake_sleep(_s):
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError
    monkeypatch.setattr(C.asyncio, "sleep", fake_sleep)
    q, notes = asyncio.Queue(), []
    rem.conductor = SimpleNamespace(faces=None, unheard=lambda: False, sleeping=True, inbox=q,
                                    notify_app=lambda *a: notes.append(a))

    async def go():
        with pytest.raises(asyncio.CancelledError):
            await rem.run()
        out = []
        while not q.empty():
            out.append(q.get_nowait())
        return out
    return asyncio.run(go()), notes


def test_fire_carries_the_project(rem, ws, monkeypatch):
    rem.add(1.0, "ship the acme release notes", "do", workspace="acme")
    rem.add(2.0, "acme call", "remind", workspace="acme")
    rem.add(3.0, "plain reminder", "remind")
    items, notes = _fire_once(rem, monkeypatch)
    assert items[0] == ("routine", "ship the acme release notes", {"workspace": "acme", "workspace_name": "Acme"})
    assert items[1] == ("reminder", "acme call", {"workspace": "acme", "workspace_name": "Acme"})
    assert items[2] == ("reminder", "plain reminder")
    assert ("reminder", "acme call", "Reminder (Acme)") in notes
    assert ("reminder", "plain reminder", "Reminder") in notes


def test_deleted_project_fires_as_global(rem, ws, monkeypatch):
    rem.add(1.0, "old job", "do", workspace="gone")
    items, _ = _fire_once(rem, monkeypatch)
    assert items == [("routine", "old job")]


def test_routine_turn_header_and_worker_tag(ws):
    me = SimpleNamespace(FACE_ID=W.WALL, realtime=None)
    ws.switch("home")
    note = Conductor._workspace_note(me, "routine", {"workspace": "acme", "workspace_name": "Acme"})
    assert note.startswith("[workspace: Acme;") and "'acme'" in note and "the wall is on Home" in note
    assert ws.task_default() == "acme"                # walterctl task in this turn tags workers acme
    # the next ordinary turn (heartbeat, voice...) is headed with the wall's workspace and clears it
    assert Conductor._workspace_note(me, "heartbeat", None) == "[workspace: Home]"
    assert ws.task_default() == "home"
    # a reminder only names its project; it doesn't move the turn
    assert Conductor._workspace_note(me, "reminder", {"workspace": "acme"}) == "[workspace: Home]"
    frame = Conductor._frame(me, "reminder", "acme call", {"workspace": "acme", "workspace_name": "Acme"})
    assert "project: Acme]" in frame
    frame = Conductor._frame(me, "routine", "do it", {"workspace": "vault", "workspace_name": "Vault"})
    assert "project: Vault]" in frame


def test_routine_header_walled(ws):
    me = SimpleNamespace(FACE_ID=W.WALL, realtime=None)
    note = Conductor._workspace_note(me, "routine", {"workspace": "vault"})
    assert note.startswith("[workspace: Vault (walled);")


# ---------------------------------------------------------------- app API
@pytest.fixture
def sc(rem):
    return S.Schedule(rem, SimpleNamespace(life=SimpleNamespace()), now=lambda: 1_800_000_000.0, timers=lambda: [])


def apply(sc, body):
    return sc.apply(S.parse(body))


def test_api_add_edit_with_project(sc, rem, ws):
    out = apply(sc, {"action": "add", "type": "routine", "text": "acme digest", "time": "07:00", "days": "weekdays",
                     "workspace": "acme"})
    assert rem.get(out["id"])["workspace"] == "acme"
    out2 = apply(sc, {"action": "add", "type": "later", "text": "global job", "time": "07:00"})
    assert "workspace" not in rem.get(out2["id"])
    apply(sc, {"action": "edit", "id": out["id"], "workspace": "vault"})      # walled ones are fine
    assert rem.get(out["id"])["workspace"] == "vault"
    apply(sc, {"action": "edit", "id": out["id"], "workspace": None})
    assert "workspace" not in rem.get(out["id"])
    st = sc.state()
    assert st["workspaces"] == [{"id": "home", "name": "Home", "walled": False},
                                {"id": "acme", "name": "Acme", "walled": False},
                                {"id": "vault", "name": "Vault", "walled": True}]
    assert st["current_workspace"] == "home"
    assert st["later"][0]["workspace"] is None


@pytest.mark.parametrize("bad", ["nowhere", "Acme", "acme corp", 7, ["acme"], "x" * 41, "../etc"])
def test_api_unknown_workspace_refused(sc, rem, ws, bad):
    with pytest.raises(S.ScheduleError) as e:
        apply(sc, {"action": "add", "type": "reminder", "text": "x", "time": "08:00", "workspace": bad})
    assert e.value.code == "unknown_workspace"
    assert rem.items == []
    r = rem.add(1e12, "y", "remind")
    with pytest.raises(S.ScheduleError):
        apply(sc, {"action": "edit", "id": r["id"], "workspace": bad})
    assert "workspace" not in rem.get(r["id"])


def test_api_without_workspaces(sc, rem):
    assert sc.state()["workspaces"] == [] and sc.state()["current_workspace"] is None
    with pytest.raises(S.ScheduleError):
        apply(sc, {"action": "add", "type": "reminder", "text": "x", "time": "08:00", "workspace": "home"})
    apply(sc, {"action": "add", "type": "reminder", "text": "x", "time": "08:00", "workspace": ""})
    assert len(rem.items) == 1


def test_filter(sc, rem, ws):
    rem.add(None, "acme r", "do", {"days": [0], "time": "08:00"}, workspace="acme")
    rem.add(None, "home r", "do", {"days": [0], "time": "09:00"}, workspace="home")
    rem.add(None, "global r", "do", {"days": [0], "time": "10:00"})
    rem.add(1e12, "acme rem", "remind", workspace="acme")
    st = sc.state()
    assert [r["text"] for r in S.Schedule.only(st, "acme")["routines"]] == ["acme r"]
    assert [r["text"] for r in S.Schedule.only(st, "acme")["reminders"]] == ["acme rem"]
    assert [r["text"] for r in S.Schedule.only(st, "none")["routines"]] == ["global r"]
    assert len(S.Schedule.only(st, None)["routines"]) == 3


def test_http_filter_and_validation(iso, tmp_path, ws):
    from aiohttp.test_utils import TestClient, TestServer
    rem = Reminders(tmp_path / "reminders.json", conductor=None)
    rem.add(None, "acme r", "do", {"days": [0], "time": "08:00"}, workspace="acme")
    rem.add(None, "global r", "do", {"days": [0], "time": "09:00"})

    async def go():
        app, apphub, _, _ = make_app(iso, reminders=rem)
        async with TestClient(TestServer(app)) as c:
            code = apphub.store.start_pairing()["code"]
            token = (await (await c.post("/api/app/pair", json={"code": code, "name": "Phone"},
                                         headers=PROXY)).json())["token"]
            auth = {**PROXY, "Authorization": f"Bearer {token}"}
            j = await (await c.get("/api/app/schedule?workspace=acme", headers=auth)).json()
            assert [r["text"] for r in j["routines"]] == ["acme r"] and j["filter"] == "acme"
            assert {w["id"] for w in j["workspaces"]} == {"home", "acme", "vault"}
            r = await c.get("/api/app/schedule?workspace=nowhere", headers=auth)
            assert r.status == 400
            r = await c.post("/api/app/schedule", json={"action": "add", "type": "routine", "text": "x",
                                                        "time": "08:00", "days": "daily", "workspace": "nowhere"},
                             headers=auth)
            assert r.status == 400 and (await r.json())["code"] == "unknown_workspace"
            r = await c.post("/api/app/schedule", json={"action": "add", "type": "routine", "text": "vault job",
                                                        "time": "08:00", "days": "daily", "workspace": "vault"},
                             headers=auth)
            j = await r.json()
            assert r.status == 200 and rem.get(j["id"])["workspace"] == "vault"
    asyncio.run(go())
