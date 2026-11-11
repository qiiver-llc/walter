"""Companion app Projects screen (walter/app_workspaces.py): list, switch, add, set; walled privacy."""
import asyncio
import json

import pytest

from walter import app_workspaces as P
from walter import workspaces as W
from tests.test_app import PROXY, iso, make_app  # noqa: F401  (iso is a fixture)


@pytest.fixture
def store(tmp_path, example_workspaces):
    s = W.WorkspaceStore(tmp_path / "workspaces.json", tmp_path / "state.json", tmp_path / "notes",
                         tmp_path / "tags.json")
    s.ensure()
    return s


def fake_overview(face, st):
    return [{"id": "acme", "badges": {"running": 2, "ready": 3}, "focus": "Ship the installer"},
            {"id": "vault", "badges": {"running": 1, "ready": 4}, "focus": "SECRET focus text"}]


@pytest.fixture
def pj(store):
    return P.Projects(store, overview=fake_overview, refresh=lambda: None)


def by_id(state):
    return {w["id"]: w for w in state["workspaces"]}


# ---- list
def test_list_and_walled_shows_counts_only(pj, store):
    s = pj.state()
    assert s["defined"] and s["current"] == "home"
    ws = by_id(s)
    assert ws["home"]["current"] and not ws["acme"]["current"]
    assert ws["acme"] == {"id": "acme", "name": "Acme", "current": False, "walled": False, "share": 0.6,
                          "running": 2, "ready": 3, "focus": "Ship the installer", "goals": ["Ship the beta"]}
    assert set(ws["vault"]) == {"id", "name", "current", "walled", "share", "running", "ready"}
    assert ws["vault"]["walled"] and ws["vault"]["running"] == 1 and ws["vault"]["ready"] == 4
    raw = json.dumps(s)
    assert "SECRET" not in raw and "network application" not in raw
    # even while the walled one is current
    store.switch("vault")
    raw = json.dumps(pj.state())
    assert "SECRET" not in raw and "network application" not in raw


# ---- switch
def test_switch_moves_the_wall(pj, store):
    out = pj.apply(P.parse({"action": "switch", "id": "acme"}))
    assert out["id"] == "acme" and store.current_id(W.WALL) == "acme"
    with pytest.raises(P.WsError):
        pj.apply(P.parse({"action": "switch", "id": "nope"}))
    for bad in ("Acme", "../x", "", 5, None, "a" * 41):
        with pytest.raises(P.WsError):
            P.parse({"action": "switch", "id": bad})


# ---- add
def test_add_normal_and_walled(pj, store, tmp_path):
    pj.apply(P.parse({"action": "add", "name": "  Beta   Labs "}))
    w = store.get("beta-labs")
    assert w["name"] == "Beta Labs" and w["privacy"] == "normal"
    assert (tmp_path / "notes" / "beta-labs" / "notes.md").exists()
    assert (tmp_path / "notes" / "beta-labs" / "backlog.md").exists()
    pj.apply(P.parse({"action": "add", "name": "Locked Box", "walled": True}))
    assert store.get("locked-box")["privacy"] == "walled"
    assert oct((tmp_path / "notes" / "locked-box").stat().st_mode & 0o777) == "0o700"


@pytest.mark.parametrize("name", ["", "   ", "x" * 41, "../etc", "a/b", "Acme; rm -rf", "<script>", "na\nme",
                                  "---", 7, None, ["Acme"]])
def test_add_rejects_bad_names(name):
    with pytest.raises(P.WsError):
        P.parse({"action": "add", "name": name})


def test_add_duplicate_and_walled_type(pj):
    for dup in ("Acme", "acme", "ACME", "Vault"):
        with pytest.raises(P.WsError) as e:
            pj.apply(P.parse({"action": "add", "name": dup}))
        assert e.value.code == "duplicate"
    with pytest.raises(P.WsError):
        P.parse({"action": "add", "name": "Ok", "walled": "yes"})


def test_add_caps_the_count(pj, monkeypatch):
    monkeypatch.setattr(P, "WORKSPACES_MAX", 3)
    with pytest.raises(P.WsError) as e:
        pj.apply(P.parse({"action": "add", "name": "Fourth"}))
    assert e.value.code == "too_many"


# ---- set
def test_set_whitelist(pj, store):
    pj.apply(P.parse({"action": "set", "id": "acme", "key": "name", "value": "Acme Corp"}))
    assert store.get("acme")["name"] == "Acme Corp"
    pj.apply(P.parse({"action": "set", "id": "acme", "key": "goals", "value": ["Ship it", " Ship it ", "Sell it"]}))
    assert store.get("acme")["goals"] == ["Ship it", "Sell it"]
    pj.apply(P.parse({"action": "set", "id": "acme", "key": "budget_share", "value": 0.35}))
    assert store.get("acme")["budget_share"] == 0.35
    pj.apply(P.parse({"action": "set", "id": "acme", "key": "budget_share", "value": 1}))
    assert store.get("acme")["budget_share"] == 1.0
    pj.apply(P.parse({"action": "set", "id": "acme", "key": "aliases", "value": ["The Shop", "shop"]}))
    assert store.get("acme")["aliases"] == ["the shop", "shop"]
    pj.apply(P.parse({"action": "set", "id": "acme", "key": "privacy", "value": "walled"}))
    assert store.get("acme")["privacy"] == "walled"


@pytest.mark.parametrize("body", [
    {"key": "repos", "value": ["~/x"]},
    {"key": "notes_dir", "value": "/tmp"},
    {"key": "google_account", "value": "x"},
    {"key": "default", "value": "acme"},
    {"key": "budget_share", "value": 1.5},
    {"key": "budget_share", "value": -0.1},
    {"key": "budget_share", "value": "0.5"},
    {"key": "budget_share", "value": True},
    {"key": "budget_share", "value": float("nan")},
    {"key": "privacy", "value": "open"},
    {"key": "goals", "value": "one"},
    {"key": "goals", "value": ["x"] * 9},
    {"key": "goals", "value": ["x" * 121]},
    {"key": "goals", "value": ["line\nbreak"]},
    {"key": "goals", "value": [3]},
    {"key": "aliases", "value": ["a"] * 11},
    {"key": "name", "value": "bad/name"},
])
def test_set_rejects(body):
    with pytest.raises(P.WsError):
        P.parse({"action": "set", "id": "acme", **body})


def test_set_unknown_id_and_rename_clash(pj):
    with pytest.raises(P.WsError) as e:
        pj.apply(P.parse({"action": "set", "id": "ghost", "key": "name", "value": "Ghost"}))
    assert e.value.status == 404
    with pytest.raises(P.WsError) as e:
        pj.apply(P.parse({"action": "set", "id": "acme", "key": "name", "value": "Vault"}))
    assert e.value.code == "duplicate"
    pj.apply(P.parse({"action": "set", "id": "acme", "key": "name", "value": "acme"}))   # its own name is fine


def test_unlock_needs_confirm(pj, store):
    with pytest.raises(P.WsError) as e:
        pj.apply(P.parse({"action": "set", "id": "vault", "key": "privacy", "value": "normal"}))
    assert e.value.status == 409 and e.value.code == "confirm_unlock"
    assert store.get("vault")["privacy"] == "walled"
    with pytest.raises(P.WsError):
        P.parse({"action": "set", "id": "vault", "key": "privacy", "value": "normal", "confirm": "yes"})
    pj.apply(P.parse({"action": "set", "id": "vault", "key": "privacy", "value": "normal", "confirm": True}))
    assert store.get("vault")["privacy"] == "normal"


def test_bad_shapes():
    for d in (None, [], "x", {}, {"action": "delete", "id": "acme"}, {"action": "set", "id": "acme"}):
        with pytest.raises(P.WsError):
            P.parse(d)


def test_rate_limit():
    t = [0.0]
    p = P.Projects(refresh=lambda: None, now=lambda: t[0])
    assert all(p.rate_ok() for _ in range(P.RATE_N))
    assert not p.rate_ok()
    t[0] = P.RATE_S + 1
    assert p.rate_ok()


# ---- the routes: pairing required, validation, end to end
def test_routes_need_pairing_and_validate(iso, example_workspaces, monkeypatch):  # noqa: F811
    import walter.cockpit as cockpit
    monkeypatch.setattr(cockpit, "DATA", iso / "data")       # no real tasks.json / loop.json
    W.WorkspaceStore().ensure()

    async def go():
        app, apphub, _, _ = make_app(iso)
        async with TestClient(TestServer(app)) as c:
            assert (await c.get("/api/app/workspaces", headers=PROXY)).status == 401
            r = await c.post("/api/app/workspaces", json={"action": "switch", "id": "acme"}, headers=PROXY)
            assert r.status == 401
            assert W.WorkspaceStore().current_id() == "home"
            code = apphub.store.start_pairing()["code"]
            token = (await (await c.post("/api/app/pair", json={"code": code, "name": "Phone"},
                                         headers=PROXY)).json())["token"]
            auth = {**PROXY, "Authorization": f"Bearer {token}"}
            j = await (await c.get("/api/app/workspaces", headers=auth)).json()
            assert {w["id"] for w in j["workspaces"]} == {"home", "acme", "vault"}
            assert "network application" not in json.dumps(j)
            r = await c.post("/api/app/workspaces", json={"action": "switch", "id": "acme"}, headers=auth)
            j = await r.json()
            assert r.status == 200 and j["current"] == "acme" and W.WorkspaceStore().current_id() == "acme"
            r = await c.post("/api/app/workspaces", json={"action": "delete", "id": "acme"}, headers=auth)
            assert r.status == 400
            r = await c.post("/api/app/workspaces", data="nope", headers={**auth, "Content-Type": "application/json"})
            assert r.status == 400
            r = await c.post("/api/app/workspaces", json={"action": "add", "name": "x" * 5000}, headers=auth)
            assert r.status == 413
            r = await c.post("/api/app/workspaces", json={"action": "set", "id": "vault", "key": "privacy",
                                                          "value": "normal"}, headers=auth)
            assert r.status == 409 and (await r.json())["code"] == "confirm_unlock"
            r = await c.post("/api/app/workspaces", json={"action": "add", "name": "Beta", "walled": True},
                             headers=auth)
            j = await r.json()
            assert r.status == 200 and {"id": "beta", "walled": True}.items() <= \
                next(w for w in j["workspaces"] if w["id"] == "beta").items()
    from aiohttp.test_utils import TestClient, TestServer
    asyncio.run(go())
