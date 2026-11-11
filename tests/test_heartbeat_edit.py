"""Editing the heartbeat from the app: the wake-up line ([life] heartbeat_prompt) and <home>/initiative.md."""
import asyncio
import os
from types import SimpleNamespace

import pytest

from walter import app_schedule as S
from walter import heartbeat as H
from walter.conductor import Reminders
from tests.test_app import PROXY, iso, make_app  # noqa: F401  (iso is a fixture)

NOW = 1_794_000_000.0


class FakeLive:
    def __init__(self, life):
        self.life, self.calls = life, []

    def set(self, key, value):
        from walter.live import parse_value
        self.calls.append((key, value))
        setattr(self.life, key.split(".", 1)[1], parse_value(value))
        return {"ok": True, "applied": [key]}


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    h.mkdir()
    (h / "initiative.md").write_text("# checklist\n1. look around\n")
    return h


@pytest.fixture
def sc(tmp_path, home):
    life = SimpleNamespace(heartbeat_minutes=30, quiet_hours=[22, 8], sleep_review=True, heartbeat_prompt="")
    clock = {"t": NOW}
    s = S.Schedule(Reminders(tmp_path / "rem.json", conductor=None),
                   SimpleNamespace(life=life, brain=SimpleNamespace(home=str(home))),
                   live=None, now=lambda: clock["t"], timers=lambda: [], backup_dir=tmp_path / "bk")
    lv = FakeLive(life)
    s._live = lambda: lv
    s.fake_live, s.clock, s.life = lv, clock, life
    return s


def apply(sc, body):
    return sc.apply(S.parse(body))


# ---- the wake-up line
def test_default_render_matches_the_old_text(home, tmp_path):
    t = H.render("", "Wednesday 11 November 2026, 10:58", home)
    assert t.startswith("[heartbeat Wednesday 11 November 2026, 10:58] Nobody has talked to you")
    assert f"Follow your initiative checklist in {home}/initiative.md." in t
    assert t.endswith("Otherwise reply only [silent].") and t.count("[silent]") == 1
    bare = H.render("", "now", tmp_path)          # no checklist: no pointer to it
    assert "initiative" not in bare and bare.endswith("[silent].")


def test_placeholders_and_silent_auto_append(home):
    t = H.render("Check {home}/initiative.md at {time}, be brief.", "noon", home)
    assert t.startswith("[heartbeat {time}] ".replace("{time}", "noon"))
    assert f"Check {home}/initiative.md at noon, be brief." in t
    assert t.endswith(H.SILENT_RULE)
    t2 = H.render("[heartbeat {time}] Do the list. Say [silent] if nothing.", "noon", home)
    assert t2 == "[heartbeat noon] Do the list. Say [silent] if nothing."
    assert "{other}" in H.render("keep {other} as is", "x", home)


def test_conductor_uses_the_live_prompt(home):
    from walter.conductor import Conductor
    fake = SimpleNamespace(realtime=None, cfg=SimpleNamespace(life=SimpleNamespace(heartbeat_prompt="Beat at {time}."),
                                               brain=SimpleNamespace(home=str(home))))
    t = Conductor._frame(fake, "heartbeat", "", {})
    assert t.startswith("[heartbeat ") and "Beat at " in t and t.endswith(H.SILENT_RULE)
    fake.cfg.life.heartbeat_prompt = ""        # changed live: next beat uses the default again
    assert "Nobody has talked to you" in Conductor._frame(fake, "heartbeat", "", {})


def test_set_and_reset_prompt(sc):
    r = apply(sc, {"action": "set_heartbeat_prompt", "text": 'Line one\nline "two"'})
    assert r["done"].startswith("Wake-up line saved")
    assert sc.life.heartbeat_prompt == 'Line one line "two"'
    assert sc.state()["loops"]["heartbeat"]["custom_prompt"] is True
    assert sc.heartbeat()["prompt"] == 'Line one line "two"'
    apply(sc, {"action": "reset_heartbeat_prompt"})
    assert sc.life.heartbeat_prompt == "" and sc.fake_live.calls[-1] == ("life.heartbeat_prompt", '""')
    assert sc.heartbeat()["default_prompt"] == H.DEFAULT_PROMPT


def test_prompt_limits():
    S.parse({"action": "set_heartbeat_prompt", "text": "x" * H.PROMPT_MAX})
    for bad in ("x" * (H.PROMPT_MAX + 1), 5, None, "a\x00b", "bad \ud800 surrogate"):
        with pytest.raises(S.ScheduleError):
            S.parse({"action": "set_heartbeat_prompt", "text": bad})


def test_prompt_round_trips_through_the_real_config_file(tmp_path):
    from walter import config as C
    from walter.live import set_in_file
    p = tmp_path / "config.toml"
    p.write_text("[life]\nheartbeat_minutes = 30\n")
    text = S.parse({"action": "set_heartbeat_prompt", "text": 'Say "hi" \\ {time} [silent]'})["text"]
    set_in_file(p, "life.heartbeat_prompt", S.toml_value(text))
    assert C.load(p).life.heartbeat_prompt == text
    set_in_file(p, "life.heartbeat_prompt", S.toml_value("true"))      # a word that looks like TOML stays text
    assert C.load(p).life.heartbeat_prompt == "true"


# ---- the checklist
def test_get_and_save(sc, home):
    g = sc.heartbeat()
    assert g["initiative_md"] == "# checklist\n1. look around\n" and g["backups"] == []
    r = apply(sc, {"action": "set_initiative", "text": "# new\r\nstep\n", "base_mtime": g["initiative_md_mtime"]})
    assert r["done"] == "Checklist saved." and r["backup"]
    assert (home / "initiative.md").read_text() == "# new\nstep\n"
    g2 = sc.heartbeat()
    assert [b["id"] for b in g2["backups"]] == [r["backup"]]
    assert (sc.initiative.bdir / f"{r['backup']}.md").read_text() == "# checklist\n1. look around\n"
    assert not list(home.glob(".*tmp"))


def test_conflict_when_changed_since_read(sc, home):
    g = sc.heartbeat()
    (home / "initiative.md").write_text("Walter edited this\n")
    os.utime(home / "initiative.md", (g["initiative_md_mtime"] + 5, g["initiative_md_mtime"] + 5))
    with pytest.raises(S.ScheduleError) as e:
        apply(sc, {"action": "set_initiative", "text": "phone edit\n", "base_mtime": g["initiative_md_mtime"]})
    assert e.value.status == 409 and e.value.code == "conflict"
    assert (home / "initiative.md").read_text() == "Walter edited this\n"
    with pytest.raises(S.ScheduleError) as e:       # no base_mtime while the file exists = stale too
        apply(sc, {"action": "set_initiative", "text": "phone edit\n", "base_mtime": None})
    assert e.value.status == 409


def test_empty_needs_allow_and_size_limits(sc):
    m = sc.heartbeat()["initiative_md_mtime"]
    with pytest.raises(S.ScheduleError) as e:
        apply(sc, {"action": "set_initiative", "text": "  \n", "base_mtime": m})
    assert e.value.code == "empty"
    apply(sc, {"action": "set_initiative", "text": "", "base_mtime": m, "allow_empty": True})
    assert sc.heartbeat()["initiative_md"] == ""
    big = "é" * (H.INITIATIVE_MAX // 2 + 1)           # 2 bytes each: just over the byte limit
    with pytest.raises(S.ScheduleError) as e:
        S.parse({"action": "set_initiative", "text": big, "base_mtime": m})
    assert e.value.status == 413
    for bad in ({"text": "a\x01b"}, {"text": 7}, {"text": "ok", "base_mtime": "1"}, {"text": "ok", "base_mtime": True},
                {"text": "bad \udc80"}):
        with pytest.raises(S.ScheduleError):
            S.parse({"action": "set_initiative", **bad})


def test_backup_rotation_and_restore(sc, home):
    for i in range(14):
        sc.clock["t"] = NOW + i                     # one backup per second
        apply(sc, {"action": "set_initiative", "text": f"version {i}\n", "base_mtime": sc.initiative.mtime()})
    bs = sc.heartbeat()["backups"]
    assert len(bs) == H.KEEP
    assert (sc.initiative.bdir / f"{bs[0]['id']}.md").read_text() == "version 12\n"     # newest first
    assert (sc.initiative.bdir / f"{bs[-1]['id']}.md").read_text() == "version 3\n"
    r = apply(sc, {"action": "restore_initiative", "backup": bs[-1]["id"]})
    assert (home / "initiative.md").read_text() == "version 3\n"
    assert (sc.initiative.bdir / f"{r['backup']}.md").read_text() == "version 13\n"     # restore is undoable
    assert len(sc.heartbeat()["backups"]) == H.KEEP


def test_restore_only_listed_backups(sc, tmp_path):
    for bad in ("../../etc/passwd", "initiative-2026", "/tmp/x", 3, "initiative-20261111-105800/../x"):
        with pytest.raises(S.ScheduleError):
            apply(sc, {"action": "restore_initiative", "backup": bad})
    with pytest.raises(S.ScheduleError) as e:
        apply(sc, {"action": "restore_initiative", "backup": "initiative-20261111-105800"})
    assert e.value.status == 404
    sc.initiative.bdir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "secret.md").write_text("secret")
    (sc.initiative.bdir / "initiative-20261111-105801.md").symlink_to(tmp_path / "secret.md")
    with pytest.raises(S.ScheduleError):
        apply(sc, {"action": "restore_initiative", "backup": "initiative-20261111-105801"})
    assert "initiative-20261111-105801" not in [b["id"] for b in sc.heartbeat()["backups"]]


# ---- over HTTP
def test_http_auth_get_set_conflict(iso):
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        app, apphub, _, _ = make_app(iso)
        ini = iso / "home" / "initiative.md"
        ini.write_text("start\n")
        async with TestClient(TestServer(app)) as c:
            assert (await c.get("/api/app/schedule/heartbeat", headers=PROXY)).status == 401
            r = await c.post("/api/app/schedule", json={"action": "set_initiative", "text": "x", "base_mtime": None},
                             headers=PROXY)
            assert r.status == 401 and ini.read_text() == "start\n"
            code = apphub.store.start_pairing()["code"]
            token = (await (await c.post("/api/app/pair", json={"code": code, "name": "Phone"},
                                         headers=PROXY)).json())["token"]
            auth = {**PROXY, "Authorization": f"Bearer {token}"}
            g = await (await c.get("/api/app/schedule/heartbeat", headers=auth)).json()
            assert g["initiative_md"] == "start\n" and g["default_prompt"] == H.DEFAULT_PROMPT
            r = await c.post("/api/app/schedule", json={"action": "set_initiative", "text": "phone\n",
                                                        "base_mtime": g["initiative_md_mtime"]}, headers=auth)
            j = await r.json()
            assert r.status == 200 and j["heartbeat"]["initiative_md"] == "phone\n" and j["routines"] == []
            assert len(j["heartbeat"]["backups"]) == 1
            assert (iso / "data" / "backups" / "initiative").is_dir()          # under the (temp) data dir
            r = await c.post("/api/app/schedule", json={"action": "set_initiative", "text": "stale\n",
                                                        "base_mtime": g["initiative_md_mtime"] - 100}, headers=auth)
            assert r.status == 409 and (await r.json())["code"] == "conflict"
            r = await c.post("/api/app/schedule", json={"action": "set_initiative", "text": "x" * 70_000,
                                                        "base_mtime": None}, headers=auth)
            assert r.status == 413
            r = await c.post("/api/app/schedule", json={"action": "set_initiative", "text": "x" * 300_000},
                             headers=auth)
            assert r.status == 413                  # over the body limit
            assert ini.read_text() == "phone\n"
    asyncio.run(go())


def test_oversized_file_is_read_only(sc, home):
    (home / "initiative.md").write_text("x" * (H.INITIATIVE_MAX + 10))
    g = sc.heartbeat()
    assert g["initiative_md_truncated"] is True and len(g["initiative_md"]) == H.INITIATIVE_MAX
    with pytest.raises(S.ScheduleError) as e:
        apply(sc, {"action": "set_initiative", "text": "short\n", "base_mtime": g["initiative_md_mtime"]})
    assert e.value.code == "too_big"
    assert len((home / "initiative.md").read_text()) == H.INITIATIVE_MAX + 10
