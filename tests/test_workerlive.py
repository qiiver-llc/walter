"""Live worker status (walter/workerlive.py) from recorded fake transcripts."""
import json
import os
import time

from walter import workerlive as WL


def _tool(name, inp, ts="2026-11-09T14:40:00.000Z"):
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "content": [{"type": "tool_use", "name": name, "input": inp}]}}


def _setup(tmp_path, monkeypatch, title="Fix the clock", status="running", finished=None):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    data = tmp_path / "data"
    cwd = tmp_path / "home"
    data.mkdir(parents=True)
    cwd.mkdir(parents=True)
    started = time.time() - 125
    (data / "tasks.json").write_text(json.dumps([
        {"id": 7, "title": title, "status": status, "started": started, "finished": finished},
        {"id": 3, "title": "Old one", "status": "done", "started": 1, "finished": 2}]))
    (data / "reminders.json").write_text(json.dumps([
        {"id": 1, "text": "QUEUED [Research flights]: silently run: ..."},
        {"id": 2, "text": "tea is ready"}]))
    proj = WL.project_dir(cwd)
    proj.mkdir(parents=True)
    other = proj / "other.jsonl"
    other.write_text(json.dumps({"type": "queue-operation", "content": "Task: Something else\n\nx"}) + "\n"
                     + json.dumps(_tool("Read", {"file_path": "/x/y.py"})) + "\n")
    mine = proj / "mine.jsonl"
    mine.write_text("\n".join(json.dumps(e) for e in [
        {"type": "session-owner", "sessionId": "s"},
        {"type": "queue-operation", "content": f"You are a background worker...\n\nTask: {title}\n\nDo it."},
        _tool("Read", {"file_path": str(WL.REPO / "README.md")}),
        _tool("Edit", {"file_path": str(WL.REPO / "face/walter.js")}),
        _tool("Write", {"file_path": str(WL.REPO / "face/new.js")}),
        _tool("Edit", {"file_path": str(WL.REPO / "face/walter.js")}),
    ]) + "\n")
    return data, cwd, mine


def test_snapshot_running_queued_and_files(tmp_path, monkeypatch):
    data, cwd, mine = _setup(tmp_path, monkeypatch)
    s = WL.Snapshotter(data, cwd)
    snap = s.snapshot()
    ws = snap["workers"]
    assert [w["status"] for w in ws] == ["running", "queued"]      # old finished one dropped
    w = ws[0]
    assert w["id"] == 7 and w["activity"] == "Editing face/walter.js" and w["files"] == 2
    assert 120 <= w["elapsed"] <= 140 and snap["running"] == 1
    assert ws[1]["title"] == "Research flights" and ws[1]["activity"].startswith("Waiting")
    # incremental: a new step (and a half-written line) arrives
    with open(mine, "a") as f:
        f.write(json.dumps(_tool("Bash", {"command": "python -m pytest tests/test_x.py -q"})) + "\n")
        f.write('{"type": "assistant", "mess')
    assert s.snapshot()["workers"][0]["activity"] == "Running tests"
    with open(mine, "a") as f:
        f.write('age": {"content": [{"type": "tool_use", "name": "WebSearch", "input": {"query": "mpv"}}]}}\n')
    assert s.snapshot()["workers"][0]["activity"] == "Searching the web: mpv"


def test_finished_worker_lingers_then_goes(tmp_path, monkeypatch):
    data, cwd, _ = _setup(tmp_path, monkeypatch, status="done", finished=time.time() - 10)
    ws = WL.Snapshotter(data, cwd).snapshot()["workers"]
    assert [w["status"] for w in ws] == ["queued", "done"] and ws[1]["activity"] == "Finished"
    data, cwd, _ = _setup(tmp_path / "b", monkeypatch, status="done", finished=time.time() - 500)
    assert [w["status"] for w in WL.Snapshotter(data, cwd).snapshot()["workers"]] == ["queued"]


def test_no_transcript_yet(tmp_path, monkeypatch):
    data, cwd, mine = _setup(tmp_path, monkeypatch, title="Brand new")
    mine.unlink()
    assert WL.Snapshotter(data, cwd).snapshot()["workers"][0]["activity"] == "Starting up"


def test_describe_is_short_human_and_redacted():
    d = WL.describe_tool
    assert d("Bash", {"command": "python -m py_compile walter/*.py"}) == "Checking Python syntax"
    assert d("Bash", {"command": "git commit -m x"}) == "Committing to git"
    assert d("Bash", {"command": "ls -la", "description": "List the face folder"}) == "List the face folder"
    assert d("Grep", {"pattern": "def snapshot"}) == "Searching for def snapshot"
    assert d("mcp__google-accounts__search_gmail_messages", {}) == "Checking Gmail"
    assert d("Agent", {"description": "Fetch docs"}) == "Helper: Fetch docs"
    assert WL.short(d("Read", {"file_path": os.path.expanduser("~/.walter/secrets/openai.key")})) == "Handling a secret file"
    assert "sk-abc" not in WL.short("Running curl -H 'Authorization: Bearer sk-abcdefghijklmnop'")
    assert "ghp_" not in WL.short("export GH=ghp_abcdefghijklmnopqrstu")
    assert "[redacted]" in WL.short("token=abcd1234")
    long = WL.short("Reading " + "a/" * 80)
    assert WL.short("Editing tests/fixtures/some/deeply/nested/folder/file_name.py").startswith("Editing tests/")
    assert len(long) <= WL.MAX_LINE and long.endswith("…")


def test_cli_writes_status_file(tmp_path, monkeypatch):
    data, cwd, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(WL, "DATA", data)
    monkeypatch.setattr(WL, "_cfg", lambda: {"brain": {"home": str(cwd), "max_tasks": 2}})
    out = tmp_path / "dashboard-workers.json"
    assert WL.main(["--out", str(out)]) == 0
    d = json.loads(out.read_text())
    assert d["max_tasks"] == 2 and d["workers"][0]["activity"] == "Editing face/walter.js"
