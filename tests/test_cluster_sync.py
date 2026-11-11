"""Shared state replication (walter/cluster.py SharedState): snapshots, follower pull, follower
edits pushed to the lead, last-writer-wins with conflict copies, the step-down rule, and that a
walled workspace is never shared with a node it isn't assigned to. Temp folders only."""
import json

import pytest

from walter import cluster as C

WS = [{"id": "home", "privacy": "normal"}, {"id": "acme", "privacy": "normal"},
      {"id": "vault", "privacy": "walled"}]


def node(tmp_path, name, me):
    data, home = tmp_path / name / "data", tmp_path / name / "home"
    data.mkdir(parents=True)
    home.mkdir(parents=True)
    return C.SharedState(me, data, home, workspaces=lambda: WS, clock=lambda: 1000.0)


def put(s, key, text):
    p = s.path(key)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def get(s, key):
    p = s.path(key)
    return p.read_text() if p.exists() else None


def round_trip(lead, fol, walled_ok=()):
    """One follower sync round without HTTP (what ClusterNode.sync_once does)."""
    snap = json.loads(json.dumps(lead.snapshot(fol.have(), walled_ok)))
    pushed = []
    for key, content in fol.dirty(set(snap["files"]), walled_ok).items():
        r = lead.accept_write(key, fol.state["synced"].get(key), content.decode(), "fol", walled_ok)
        fol.pushed(key, r["sha"])
        pushed.append(key)
    return pushed, fol.apply(snap, walled_ok, skip=pushed)


@pytest.fixture
def pair(tmp_path):
    lead, fol = node(tmp_path, "lead", "n-aaaaaaaaaaaa"), node(tmp_path, "fol", "n-bbbbbbbbbbbb")
    lead.become_lead()
    return lead, fol


def test_follower_pulls_everything_shared(pair):
    lead, fol = pair
    put(lead, "home/memory.md", "# memory\n- likes tea\n")
    put(lead, "data/dashboard.json", '{"items": []}')
    put(lead, "ws/acme/notes.md", "acme notes")
    _, pulled = round_trip(lead, fol)
    assert set(pulled) == {"home/memory.md", "data/dashboard.json", "ws/acme/notes.md"}
    assert get(fol, "home/memory.md") == "# memory\n- likes tea\n"
    assert oct(fol.path("data/dashboard.json").stat().st_mode & 0o777) == "0o600"
    # nothing new: the next snapshot carries no content
    snap = lead.snapshot(fol.have())
    assert all("content" not in v for v in snap["files"].values())


def test_lead_edits_bump_versions(pair):
    lead, fol = pair
    put(lead, "home/backlog.md", "a")
    v1 = lead.manifest()["home/backlog.md"]["v"]
    put(lead, "home/backlog.md", "b")
    assert lead.manifest()["home/backlog.md"]["v"] == v1 + 1
    round_trip(lead, fol)
    assert get(fol, "home/backlog.md") == "b"


def test_follower_edit_is_pushed_to_the_lead(pair):
    lead, fol = pair
    put(lead, "home/memory.md", "one")
    round_trip(lead, fol)
    put(fol, "home/memory.md", "one\ntwo")
    pushed, _ = round_trip(lead, fol)
    assert pushed == ["home/memory.md"] and get(lead, "home/memory.md") == "one\ntwo"
    assert lead.conflict_log() == []
    # a brand-new file on the follower that the lead doesn't have goes up too
    put(fol, "ws/home/notes.md", "fresh")
    pushed, _ = round_trip(lead, fol)
    assert "ws/home/notes.md" in pushed and get(lead, "ws/home/notes.md") == "fresh"


def test_concurrent_edits_last_writer_wins_with_conflict_copy(pair):
    lead, fol = pair
    put(lead, "home/memory.md", "base")
    round_trip(lead, fol)
    put(lead, "home/memory.md", "lead edit")
    lead.refresh()
    put(fol, "home/memory.md", "follower edit")
    round_trip(lead, fol)
    assert get(lead, "home/memory.md") == "follower edit" and get(fol, "home/memory.md") == "follower edit"
    log = lead.conflict_log()
    assert len(log) == 1 and log[0]["key"] == "home/memory.md"
    from pathlib import Path
    assert Path(log[0]["copy"]).read_text() == "lead edit"


def test_joining_install_keeps_its_old_copy_as_a_conflict(pair):
    lead, fol = pair
    put(lead, "home/memory.md", "the lead's memory")
    put(fol, "home/memory.md", "the new install's own memory")
    round_trip(lead, fol)
    assert get(fol, "home/memory.md") == "the lead's memory"
    log = fol.conflict_log()
    assert log and log[0]["key"] == "home/memory.md"


def test_stepped_down_lead_never_pushes_its_split_brain_edits(pair, tmp_path):
    lead, old = pair
    old.become_lead()
    put(old, "home/memory.md", "written while wrongly lead")
    old.refresh()
    put(lead, "home/memory.md", "the real lead")
    old.step_down()
    pushed, pulled = round_trip(lead, old)
    assert pushed == [] and pulled == ["home/memory.md"]
    assert get(old, "home/memory.md") == "the real lead" and get(lead, "home/memory.md") == "the real lead"
    assert any("split" in c["why"] for c in old.conflict_log())


def test_walled_workspace_not_shared_unless_assigned(pair):
    lead, fol = pair
    put(lead, "ws/vault/notes.md", "private")
    put(lead, "ws/acme/notes.md", "ok")
    snap = lead.snapshot({})
    assert "ws/vault/notes.md" not in snap["files"] and "ws/acme/notes.md" in snap["files"]
    round_trip(lead, fol)
    assert get(fol, "ws/vault/notes.md") is None
    # a follower can't write it either
    with pytest.raises(PermissionError):
        lead.accept_write("ws/vault/notes.md", None, "x", "fol")
    # a follower's own walled notes are never pushed when it isn't assigned
    put(fol, "ws/vault/notes.md", "follower private")
    assert "ws/vault/notes.md" not in fol.dirty(set())
    # once assigned (walterctl cluster assign NODE vault), it flows
    snap = lead.snapshot({}, walled_ok=["vault"])
    assert snap["files"]["ws/vault/notes.md"]["content"] == "private"


def test_keys_are_never_paths(pair):
    lead, fol = pair
    for bad in ("../../etc/passwd", "home/../secrets", "data/cluster.json", "ws/../x/notes.md",
                "data/devices.json", "/etc/passwd"):
        assert lead.path(bad) is None
        with pytest.raises(PermissionError):
            lead.accept_write(bad, None, "x", "fol")
    # an evil snapshot with a bad key is ignored
    assert fol.apply({"files": {"data/devices.json": {"sha": C.sha(b"x"), "content": "x"}}}) == []
    assert not (fol.data / "devices.json").exists()


def test_secrets_are_not_in_the_shared_set(pair):
    lead, _ = pair
    keys = set(lead.files(walled_ok=["vault"]))
    assert not any(k.endswith(("devices.json", "cluster.json", "config.toml")) or "secrets" in k for k in keys)
    assert "data/faces.json" in keys and "data/reminders.json" in keys
