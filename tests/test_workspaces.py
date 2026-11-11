"""Workspaces: model, aliases / voice matching, per-face state, privacy (walled) checks."""
import json
import sys
import types

import pytest

from walter import workspaces as W


@pytest.fixture
def store(tmp_path, example_workspaces):
    s = W.WorkspaceStore(tmp_path / "workspaces.json", tmp_path / "state.json", tmp_path / "notes",
                         tmp_path / "tags.json")
    s.ensure()
    return s


def test_not_defined_means_seed_in_memory_only(tmp_path):
    s = W.WorkspaceStore(tmp_path / "ws.json", tmp_path / "st.json", tmp_path / "n", tmp_path / "t.json")
    assert not s.defined()
    assert s.ids() == ["home"]                  # a new install: just Home, no owner projects
    assert s.header() == ""                      # single-workspace behaviour: no turn header
    assert W.voice_match("switch to acme corp", s) is None
    assert not (tmp_path / "ws.json").exists()


def test_seed_fields_and_folders(store, tmp_path):
    assert store.defined()
    d = json.loads((tmp_path / "workspaces.json").read_text())
    assert d["default"] == "home"
    lh = store.get("vault")
    assert lh["privacy"] == "walled" and lh["google_account"] is None
    q = store.get("acme")
    assert q["repos"] == ["~/walter", "~/walter-site"]
    for wid in store.ids():
        nd = tmp_path / "notes" / wid
        assert (nd / "notes.md").exists()
    assert (tmp_path / "notes" / "vault" / "backlog.md").exists()
    assert oct((tmp_path / "notes" / "vault").stat().st_mode & 0o777) == "0o700"


def test_real_seed_is_just_home():
    """What ships: a new install gets only Home, with no emails or project names in it."""
    assert [w["id"] for w in W.SEED["workspaces"]] == ["home"]
    assert W.SEED["workspaces"][0]["budget_share"] == 1.0
    assert "@" not in json.dumps(W.SEED)


@pytest.mark.parametrize("said,wid", [
    ("acme corp", "acme"), ("Acme", "acme"), ("the business", "acme"),
    ("the vault", "vault"), ("Vault", "vault"), ("vault work", "vault"),
    ("home", "home"), ("the home workspace", "home"), ("ACME-corp", "acme"),
])
def test_aliases(store, said, wid):
    assert store.get(said)["id"] == wid


def test_unknown_name(store):
    assert store.get("garage") is None
    assert store.get("") is None


@pytest.mark.parametrize("text,kind,wid", [
    ("switch to acme corp", "switch", "acme"),
    ("Walter, switch to the vault", "switch", "vault"),
    ("work on vawlt", "switch", "vault"),
    ("let's work on acme now", "switch", "acme"),
    ("go back to home", "switch", "home"),
    ("switch to the Acme workspace", "switch", "acme"),
    ("which workspace am I in?", "which", None),
    ("what workspace are we in", "which", None),
    ("list my workspaces", "list", None),
])
def test_voice_match(store, text, kind, wid):
    r = W.voice_match(text, store)
    assert r is not None and r[0] == kind
    if wid:
        assert r[1]["id"] == wid


@pytest.mark.parametrize("text", ["work on the garage", "switch to cloud ears", "what's the weather",
                                  "switch to the kitchen lights and then dim them"])
def test_voice_no_match(store, text):
    assert W.voice_match(text, store) is None


def test_per_face_state(store):
    assert store.current_id() == "home"
    store.switch("acme corp")                         # the wall
    assert store.current_id("wall") == "acme"
    assert store.current_id("kitchen") == "home"   # other faces untouched
    store.switch("vault work", face_id="kitchen")
    assert store.current_id("wall") == "acme"
    assert store.current_id("kitchen") == "vault"
    assert store.header("kitchen") == "[workspace: Vault (walled)]"
    assert store.header("wall") == "[workspace: Acme]"
    with pytest.raises(KeyError):
        store.switch("garage")


def test_add_and_set(store):
    w = store.add("Farm Co", aliases=["the farm"])
    assert w["id"] == "farm-co"
    assert store.get("the farm")["id"] == "farm-co"
    with pytest.raises(ValueError):
        store.add("farm co")
    assert store.set("farm co", "aliases", "+farm")["aliases"][-1] == "farm"
    assert store.set("farm co", "privacy", "walled")["privacy"] == "walled"
    assert store.set("farm co", "budget", "10%")["budget_share"] == pytest.approx(0.1)
    assert store.set("farm co", "google_account", "none")["google_account"] is None
    with pytest.raises(ValueError):
        store.set("farm co", "privacy", "secret")
    with pytest.raises(ValueError):
        store.set("farm co", "colour", "red")
    store.set("acme", "default", "")
    assert store.default_id() == "acme"


def test_face_default_adapter_without_faces_module(store, monkeypatch):
    monkeypatch.setitem(sys.modules, "walter.faces", None)   # import fails like a missing module
    assert W.default_workspace_for_face("office-laptop", store) == "home"


def test_face_default_adapter_with_faces_module(store, monkeypatch):
    fake = types.ModuleType("walter.faces")
    fake.load_faces = lambda: {"faces": [{"id": "office", "default_workspace": "Acme"},
                                         {"id": "kitchen", "default_workspace": "nowhere"}]}
    monkeypatch.setitem(sys.modules, "walter.faces", fake)
    import walter
    monkeypatch.setattr(walter, "faces", fake, raising=False)
    assert W.default_workspace_for_face("office", store) == "acme"
    assert W.default_workspace_for_face("kitchen", store) == "home"   # unknown workspace -> default
    assert store.current_id("office") == "acme"
    store.switch("home", face_id="office")
    assert store.current_id("office") == "home"


def test_face_default_adapter_registry_class(store, monkeypatch):
    class Reg:
        def get(self, fid):
            return {"id": fid, "default_workspace": "vault"} if fid == "laptop" else None
    fake = types.ModuleType("walter.faces")
    fake.FaceRegistry = Reg
    monkeypatch.setitem(sys.modules, "walter.faces", fake)
    import walter
    monkeypatch.setattr(walter, "faces", fake, raising=False)
    assert W.default_workspace_for_face("laptop", store) == "vault"


def test_walled_paths(store, tmp_path):
    lh_notes = tmp_path / "notes" / "vault" / "notes.md"
    q_notes = tmp_path / "notes" / "acme" / "notes.md"
    home_notes = tmp_path / "notes" / "home" / "notes.md"
    assert store.path_allowed(lh_notes, "vault")
    assert not store.path_allowed(lh_notes, "acme")
    assert not store.path_allowed(lh_notes, "home")
    assert not store.path_allowed(q_notes, "vault")
    assert store.path_allowed(q_notes, "home")             # both normal
    assert store.path_allowed(home_notes, "acme")
    assert store.path_allowed(tmp_path / "elsewhere.txt", "vault")   # nobody owns it
    assert store.owner_of(lh_notes)["id"] == "vault"


def test_brief_scoping(store, tmp_path):
    b = store.brief("vault")
    assert b.startswith("[Workspace: Vault")
    assert "WALLED" in b and "by instruction" in b
    assert "~/walter" in b and str(tmp_path / "notes" / "acme") in b      # others are off limits
    q = store.brief("acme")
    assert "Repos: ~/walter, ~/walter-site." in q
    assert str(tmp_path / "notes" / "vault") in q
    assert str(tmp_path / "notes" / "home") not in q.split("Off limits:")[1]  # home is normal
    assert store.brief("garage") == ""


def test_task_tags_and_visibility(store):
    store.tag_task(7, "vault")
    assert store.task_workspace({"id": 7}) == "vault"
    assert store.task_workspace({"id": 8, "workspace": "acme"}) == "acme"
    assert store.task_visible({"id": 7}, "vault")
    assert not store.task_visible({"id": 7}, "acme")
    assert store.task_visible({"id": 99}, "acme")          # untagged (older) workers: everywhere...
    assert not store.task_visible({"id": 99}, "vault")  # ...except walled workspaces


def test_budget_pick(store):
    shares = store.budget_shares()
    assert sum(shares.values()) == pytest.approx(1.0, abs=0.01)
    recent = [{"id": i, "workspace": "acme"} for i in range(6)]
    assert store.pick_next(recent) in ("home", "vault")
    assert store.pick_next([]) == "acme"                    # biggest share first


def test_face_default_with_the_real_faces_registry(store, tmp_path, monkeypatch):
    """Against walter/faces.py (faces track) when it exists: a face's default_workspace is used, and
    switching one face never changes another."""
    faces = pytest.importorskip("walter.faces")
    if not hasattr(faces, "FaceStore"):
        pytest.skip("faces.py has no FaceStore")
    monkeypatch.setenv("WALTER_DATA", str(tmp_path / "facedata"))
    fs = faces.FaceStore(tmp_path / "facedata" / "faces.json")
    try:
        fs.set_workspace("wall", "acme")
    except Exception as e:   # the API may still be moving
        pytest.skip(f"faces.FaceStore.set_workspace: {e}")
    orig = faces.FaceStore.__init__
    monkeypatch.setattr(faces.FaceStore, "__init__",
                        lambda self, path=None, **kw: orig(self, path or tmp_path / "facedata" / "faces.json", **kw))
    assert W.default_workspace_for_face("wall", store) == "acme"
    assert store.current_id("wall") == "acme"
    assert W.default_workspace_for_face("nobody", store) == "home"
    store.switch("home", "wall")
    assert store.current_id("wall") == "home"
