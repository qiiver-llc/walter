
import pytest as _pytest


@_pytest.fixture(autouse=True)
def _isolated_mute_file(tmp_path, monkeypatch):
    """Never let tests touch the real ~/.walter/mute.json."""
    from walter.conductor import Conductor
    monkeypatch.setattr(Conductor, "MUTE_FILE", tmp_path / "mute.json")
    monkeypatch.setattr(Conductor, "BRAIN_MODE_FILE", tmp_path / "brain_mode.json")


@_pytest.fixture(autouse=True)
def _isolated_speech(tmp_path, monkeypatch):
    """Speech switches, usage and the API key: never the real ~/.walter, never a real key."""
    import walter.speech
    monkeypatch.setattr(walter.speech, "DATA_DIR", tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@_pytest.fixture(autouse=True)
def _isolated_away(tmp_path, monkeypatch):
    """The away feed, wall cards and Web Push keys/subscriptions: never the real
    ~/.walter or the real face/card.json; no hooks leak between tests."""
    import walter.dashboard as dash
    import walter.webpush as wp
    monkeypatch.setattr(dash.AwayFeed, "DEFAULT_PATH", tmp_path / "away" / "away.json")
    monkeypatch.setattr(dash.AwayFeed, "DEFAULT_FACE", tmp_path / "away" / "dashboard-away.json")
    monkeypatch.setattr(dash, "CARD_OUT", tmp_path / "away" / "card.json")
    monkeypatch.setattr(wp.WebPush, "DEFAULT_BASE", tmp_path / "away")
    monkeypatch.setattr(dash, "_away_hooks", [])


@_pytest.fixture(autouse=True)
def _isolated_focus(tmp_path, monkeypatch):
    """Focus boxes, their log and face/focus.json: never the real ones."""
    import walter.focus as focus
    monkeypatch.setattr(focus.FocusStore, "DEFAULT_PATH", tmp_path / "focus" / "focus.json")
    monkeypatch.setattr(focus.FocusStore, "DEFAULT_LOG", tmp_path / "focus" / "focus-log.json")
    monkeypatch.setattr(focus.FocusStore, "DEFAULT_FACE", tmp_path / "focus" / "face-focus.json")


@_pytest.fixture(autouse=True)
def _isolated_loop(tmp_path, monkeypatch):
    """Autonomy loop state and face/dashboard-loop.json: never the real ones."""
    import walter.loop as loop
    monkeypatch.setattr(loop.LoopStore, "DEFAULT_PATH", tmp_path / "loop" / "loop.json")
    monkeypatch.setattr(loop.LoopStore, "DEFAULT_FACE", tmp_path / "loop" / "dashboard-loop.json")


@_pytest.fixture(autouse=True)
def _isolated_workspaces(tmp_path, monkeypatch):
    """Workspaces, per-face state and worker tags: never the real ~/.walter ones (so by default
    no workspaces are defined and everything behaves as single-workspace)."""
    import walter.workspaces as ws
    monkeypatch.setattr(ws.WorkspaceStore, "DEFAULT_PATH", tmp_path / "ws" / "workspaces.json")
    monkeypatch.setattr(ws.WorkspaceStore, "DEFAULT_STATE", tmp_path / "ws" / "workspace-state.json")
    monkeypatch.setattr(ws.WorkspaceStore, "DEFAULT_NOTES_ROOT", tmp_path / "ws" / "notes")
    monkeypatch.setattr(ws.WorkspaceStore, "DEFAULT_TAGS", tmp_path / "ws" / "task-workspaces.json")
    monkeypatch.setattr(ws.WorkspaceStore, "DEFAULT_FACE_OUT", tmp_path / "ws" / "dashboard-workspace.json")


@_pytest.fixture(autouse=True)
def _isolated_cockpit_face(tmp_path, monkeypatch):
    """The cockpit's face files (per-workspace plans and test mirrors, the plan migration): a temp folder,
    never the real face/ (a test with workspaces would otherwise move the real plan)."""
    from walter import cockpit
    face = tmp_path / "cockpit-face"
    face.mkdir()
    monkeypatch.setattr(cockpit, "FACE", face)
    monkeypatch.setattr(cockpit, "FACE_TESTS", face / "dashboard-tests.json")


@_pytest.fixture(autouse=True)
def _isolated_chatlog(tmp_path, monkeypatch):
    """The app's chat history (walter/chatlog.py): never the real ~/.walter/chat-history.jsonl."""
    import walter.chatlog as chatlog
    monkeypatch.setattr(chatlog, "default_path", lambda: tmp_path / "chatlog" / "chat-history.jsonl")
    monkeypatch.setattr(chatlog, "_default", None)


# Generic example projects for workspace tests. A new install's real seed is just Home
# (walter/workspaces.py SEED); tests that need several workspaces, one of them walled, use these.
EXAMPLE_WORKSPACES = {
    "version": 1,
    "default": "home",
    "workspaces": [
        {"id": "home", "name": "Home", "aliases": ["home", "house", "family", "personal", "home stuff"],
         "repos": [], "google_account": None, "calendars": [], "goals": ["Keep the household running smoothly"],
         "budget_share": 0.2, "privacy": "normal"},
        {"id": "acme", "name": "Acme",
         "aliases": ["acme", "acme corp", "akme", "the business"],
         "repos": ["~/walter", "~/walter-site"], "google_account": None, "calendars": [],
         "goals": ["Ship the beta"], "budget_share": 0.6, "privacy": "normal"},
        {"id": "vault", "name": "Vault",
         "aliases": ["vault", "the vault", "vault work", "vawlt"],
         "repos": [], "google_account": None, "calendars": [],
         "goals": ["Finish the network application"], "budget_share": 0.2, "privacy": "walled"},
    ],
}


@_pytest.fixture
def example_workspaces(monkeypatch):
    """Seed Home + Acme (normal) + Vault (walled) instead of the real just-Home seed. Returns the dict
    (write it as JSON to a data dir for tests that run walterctl in a subprocess)."""
    import copy
    import walter.workspaces as ws
    monkeypatch.setattr(ws, "SEED", copy.deepcopy(EXAMPLE_WORKSPACES))
    return copy.deepcopy(EXAMPLE_WORKSPACES)
