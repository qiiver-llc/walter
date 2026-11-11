"""Faces registry + pairing + per-face settings + best-mic arbitration (walter/faces.py).
Never touches ~/.walter or the real face/ files."""
import json
import os

import pytest

from walter import display_settings as disp
from walter import faces as F


@pytest.fixture
def iso(tmp_path, monkeypatch):
    monkeypatch.setenv("WALTER_DATA", str(tmp_path / "data"))
    import walter.dashboard as dash
    (tmp_path / "face").mkdir()
    monkeypatch.setattr(dash, "FACE_DIR", tmp_path / "face")
    return tmp_path


def store(tmp_path, **kw):
    return F.FaceStore(tmp_path / "faces.json", **kw)


# ---------------------------------------------------------------- registry
def test_wall_is_always_there_and_named_from_config(tmp_path):
    st = store(tmp_path, wall_name="Walter")
    faces = st.list()
    assert [f["id"] for f in faces] == ["wall"]
    assert faces[0]["name"] == "Walter" and faces[0]["kind"] == "wall"
    assert faces[0]["default_workspace"] is None
    assert st.get("WALL")["id"] == "wall" and st.get("walter")["id"] == "wall"
    assert not (tmp_path / "faces.json").exists()   # listing writes nothing
    assert store(tmp_path, wall_name="Living Room").get("wall")["name"] == "Living Room"


def test_pairing_happy_path_token_hashed_single_use(tmp_path):
    st = store(tmp_path)
    p = st.start_pairing("Kitchen")
    assert len(p["code"]) == 6 and p["code"].isdigit() and st.pairing_open()
    face, token = st.pair(p["code"], kind="tablet", room="kitchen")
    assert face["name"] == "Kitchen" and face["id"] == "kitchen" and face["kind"] == "tablet"
    assert face["room"] == "kitchen" and "token_sha256" not in face and len(token) >= 40
    raw = (tmp_path / "faces.json").read_text()
    assert token not in raw and F.token_hash(token) in raw and p["code"] not in raw
    assert oct(os.stat(tmp_path / "faces.json").st_mode & 0o777) == "0o600"
    assert st.check(token)["id"] == "kitchen"
    assert st.check("nope") is None and st.check("") is None and st.check(None) is None
    assert st.check("x" * 500) is None
    assert not st.pairing_open()
    with pytest.raises(PermissionError):   # single use
        st.pair(p["code"], "Garage")


def test_pairing_ttl_and_wrong_codes_burn(tmp_path):
    st = store(tmp_path)
    code = st.start_pairing(now=1000, ttl=60)["code"]
    with pytest.raises(PermissionError):
        st.pair(code, "Kitchen", now=1061)
    code = st.start_pairing()["code"]
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    for _ in range(F.PAIR_MAX_TRIES):
        with pytest.raises(PermissionError):
            st.pair(wrong, "Kitchen")
    with pytest.raises(PermissionError):   # burned: even the right code is dead
        st.pair(code, "Kitchen")
    assert [f["id"] for f in st.list()] == ["wall"]


def test_pairing_needs_a_name_and_cleans_it(tmp_path):
    st = store(tmp_path)
    code = st.start_pairing()["code"]
    with pytest.raises(ValueError):
        st.pair(code, "")
    face, _ = st.pair(code, '  <Garage>\n"x"  ', kind="wall")
    assert face["name"] == "Garage x" and face["kind"] == "other"   # nobody else may be "wall"
    code = st.start_pairing()["code"]
    face2, _ = st.pair(code, "garage X")
    assert face2["name"] == "garage X 2" and face2["id"] == "garage-x-2"
    code = st.start_pairing()["code"]
    face3, _ = st.pair(code, "Wall")
    assert face3["id"] == "wall-2"   # "wall" is reserved


def test_rename_room_kind_workspace_forget(tmp_path):
    st = store(tmp_path)
    face, token = st.pair(st.start_pairing()["code"], "Kitchen")
    assert st.rename("kitchen", "Cook Top")["name"] == "Cook Top"
    assert st.get("cook top")["id"] == "kitchen"   # the id never changes
    with pytest.raises(ValueError):
        st.rename("kitchen", "Walter")   # the wall's name is taken
    assert st.set_room("Cook Top", "kitchen")["room"] == "kitchen"
    assert st.set_kind("kitchen", "laptop")["kind"] == "laptop"
    with pytest.raises(ValueError):
        st.set_kind("kitchen", "wall")
    with pytest.raises(ValueError):
        st.set_kind("wall", "tablet")
    assert st.set_workspace("kitchen", "Home")["default_workspace"] == "Home"
    assert st.set_workspace("kitchen", "none")["default_workspace"] is None
    assert st.rename("wall", "Living Room")["name"] == "Living Room"
    assert st.get("wall")["name"] == "Living Room"
    with pytest.raises(KeyError):
        st.set_room("nobody", "x")
    assert st.forget("wall") == []          # the wall can't be forgotten
    assert st.forget("cook top") == ["Cook Top"]
    assert st.check(token) is None


def test_settings_overrides_are_validated(tmp_path):
    st = store(tmp_path)
    st.pair(st.start_pairing()["code"], "Kitchen")
    assert st.set_settings("kitchen", {"volume": 40, "muted": True})["settings"] == {"volume": 40, "muted": True}
    for bad in ({"volume": 101}, {"volume": True}, {"volume": "5"}, {"muted": 1}, {"brain.model": "x"},
                {"config": {}}):
        with pytest.raises(ValueError):
            st.set_settings("kitchen", bad)
    assert st.get("kitchen")["settings"] == {"volume": 40, "muted": True}


def test_touch_is_throttled_and_records_mic(tmp_path):
    st = store(tmp_path)
    face, _ = st.pair(st.start_pairing(now=100)["code"], "Kitchen", now=100)
    st.touch("kitchen", "192.168.1.5", now=110)
    assert st.get("kitchen")["last_ip"] == "192.168.1.5"
    st.touch("kitchen", "192.168.1.5", now=120)
    assert st.get("kitchen")["last_seen"] == 110
    st.touch("kitchen", "192.168.1.5", mic_quality=0.8, now=125)
    assert st.get("kitchen")["mic_quality"] == 0.8
    st.touch("wall", "x")   # never written for the wall
    assert st.get("wall")["last_ip"] == ""


def test_module_get_face_for_workspaces(iso):
    st = F.FaceStore()
    st.pair(st.start_pairing()["code"], "Office")
    st.set_workspace("office", "Acme")
    assert F.get_face("office")["default_workspace"] == "Acme"
    assert F.get_face("nobody") is None
    from walter import workspaces
    if hasattr(workspaces, "default_workspace_for_face"):
        assert workspaces.default_workspace_for_face("wall")   # never raises, falls back


# ---------------------------------------------------------------- per-face display
def test_face_display_files_are_separate(iso):
    face_dir = iso / "face"
    disp.write_display(None, theme="cream", text_scale=1.1, rotate=90)
    assert disp.face_display_path("wall") == face_dir / "display.json"
    assert disp.face_display_path(None) == face_dir / "display.json"
    assert disp.face_display_path("kitchen") == face_dir / "display-kitchen.json"
    with pytest.raises(ValueError):
        disp.face_display_path("../x")
    seeded = disp.seed_face_display("kitchen")
    assert seeded["theme"] == "cream" and seeded["text_scale"] == 1.1 and "rotate" not in seeded
    disp.set_theme("ocean", disp.face_display_path("kitchen"))
    assert disp.get_theme(disp.face_display_path("kitchen")) == "ocean"
    assert disp.get_theme() == "cream"   # the wall's stays
    assert disp.seed_face_display("kitchen")["theme"] == "ocean"   # never overwritten


def test_face_data_files_never_reload_the_wall():
    from pathlib import PurePosixPath as P
    from walter.server import _face_data
    assert _face_data(P("display-kitchen.json")) and _face_data(P("card-kitchen.json"))
    assert not _face_data(P("faceid.js"))


# ---------------------------------------------------------------- mic score
def test_mic_score():
    assert F.mic_score([]) == 0.0
    near = F.mic_score([0.5, 0.6, 0.55, 0.1], floor_rms=60)
    far = F.mic_score([0.05, 0.08, 0.06, 0.01], floor_rms=60)
    assert 0 < far < near <= 1
    noisy = F.mic_score([0.5, 0.6, 0.55, 0.1], floor_rms=1500)
    assert noisy < near
    assert F.mic_score([float("nan"), "x", -1]) == 0.0
    assert F.parse_score(2) == 1.0 and F.parse_score(-1) == 0.0 and F.parse_score(True) == 0.0
    assert F.parse_score(float("inf")) == 0.0 and F.parse_score("0.5") == 0.0


# ---------------------------------------------------------------- names
FACES = [{"id": "wall", "name": "Walter"}, {"id": "kitchen", "name": "Kitchen"},
         {"id": "garage", "name": "Garage"}, {"id": "office", "name": "Home Office"}]


@pytest.mark.parametrize("text,want", [
    ("Hey Kitchen, set a timer for ten minutes", "kitchen"),
    ("hey kitchen what's the weather", "kitchen"),
    ("Garage, play the radio", "garage"),
    ("OK garage turn it up", "garage"),
    ("hey home office, read my list", "office"),
    ("Walter, what time is it", None),          # plain wake word -> best mic, not a target
    ("hey walter", None),
    ("I left it in the kitchen", None),          # just mentioning a room
    ("put the kitchen timer on", None),
    ("", None),
])
def test_name_target(text, want):
    got = F.name_target(text, FACES)
    assert (got or {}).get("id") == want


def test_wall_renamed_is_a_target():
    faces = [{"id": "wall", "name": "Living Room"}, {"id": "kitchen", "name": "Kitchen"}]
    assert F.name_target("hey living room, lights off", faces)["id"] == "wall"


def test_similar():
    assert F.similar("Walter what's the weather", "walter whats the weather") > 0.85
    assert F.similar("Walter what's the weather", "set a timer for ten minutes") < 0.5
    assert F.similar("", "x") == 0.0


# ---------------------------------------------------------------- arbiter
def test_best_mic_wins_within_window():
    clock = [0.0]
    a = F.Arbiter(window_s=1.2, clock=lambda: clock[0])
    assert a.offer("wall", "Walter what's the weather", 0.3)[0] == "new"
    clock[0] = 0.4
    assert a.offer("kitchen", "walter what's the weather?", 0.8)[0] == "joined"
    clock[0] = 0.6
    assert a.offer("garage", "walter what is the weather", 0.5)[0] == "joined"
    assert a.due() == []
    clock[0] = 1.25
    (g,) = a.due()
    d = F.decide(g, FACES)
    assert d["face"] == "kitchen" and not d["named"] and d["score"] == 0.8
    assert d["heard_by"] == ["wall", "kitchen", "garage"]
    assert d["text"] == "walter what's the weather?"
    assert a.groups == []


def test_outside_window_or_different_words_is_a_new_turn():
    clock = [0.0]
    a = F.Arbiter(window_s=1.2, clock=lambda: clock[0])
    a.offer("wall", "Walter what's the weather", 0.3)
    clock[0] = 0.5
    assert a.offer("kitchen", "walter set a timer for ten minutes", 0.9)[0] == "new"
    clock[0] = 1.4
    assert a.offer("garage", "Walter what's the weather", 0.9)[0] == "new"   # too late: its own turn
    clock[0] = 1.5
    assert len(a.due()) == 1   # only the first group closed
    clock[0] = 3.0
    assert len(a.due()) == 2


def test_same_face_twice_keeps_the_better_score():
    a = F.Arbiter(window_s=1.2, clock=lambda: 0.0)
    a.offer("kitchen", "walter lights", 0.2)
    assert a.offer("kitchen", "walter lights", 0.7)[0] == "dup"
    g = a.close(1)
    assert len(g["entries"]) == 1 and g["entries"][0]["score"] == 0.7
    assert a.close(1) is None


def test_name_beats_best_mic_even_if_that_face_did_not_hear_it():
    a = F.Arbiter(window_s=1.2, clock=lambda: 0.0)
    a.offer("wall", "hey garage, play the radio", 0.9)
    a.offer("kitchen", "hey garage play the radio", 0.6)
    d = F.decide(a.close(1), FACES)
    assert d["face"] == "garage" and d["named"] and d["text"] == "hey garage, play the radio"
    a.offer("garage", "hey garage play the radio", 0.1)
    a.offer("wall", "hey garage play the radio", 0.9)
    d = F.decide(a.close(2), FACES)
    assert d["face"] == "garage" and d["score"] == 0.1


def test_tie_goes_to_the_earliest():
    clock = [0.0]
    a = F.Arbiter(window_s=1.2, clock=lambda: clock[0])
    a.offer("garage", "walter hi there", 0.5)
    clock[0] = 0.2
    a.offer("kitchen", "walter hi there", 0.5)
    assert F.decide(a.close(1), FACES)["face"] == "garage"
