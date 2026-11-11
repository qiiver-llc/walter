import json
import subprocess

from walter import cockpit as C
from walter import workerlive as WL

BACKLOG = """# Backlog
- [ready] **Thinking indicator**: show it
- [in progress] **Cockpit for Alice**: build mode v2
- [done] **Sticky build mode**: done
- [ask] **Ask first**: no column
- [ready] Plain title. With details
not a line
"""


def test_parse_backlog_and_redaction():
    rows = C.parse_backlog(BACKLOG, ["Alice"])
    assert [r["status"] for r in rows] == ["ready", "in progress", "done", "ask", "ready"]
    assert rows[0]["title"] == "Thinking indicator"
    assert "Alice" not in rows[1]["title"] and rows[1]["title"].startswith("Cockpit for")
    assert rows[4]["title"] == "Plain title"


def test_task_board_columns(tmp_path):
    (tmp_path / "b.md").write_text(BACKLOG)
    tasks = [{"id": 1, "title": "Running job", "status": "running", "started": 90},
             {"id": 2, "title": "Old job", "status": "done", "started": 0, "finished": 50},
             {"id": 3, "title": "Ancient", "status": "done", "started": 0, "finished": 100 - 90000},
             {"id": 4, "title": "Mail bob@example.com", "status": "failed", "started": 0, "finished": 80}]
    (tmp_path / "t.json").write_text(json.dumps(tasks))
    b = C.task_board(100, tmp_path / "b.md", tmp_path / "t.json", words=[])
    assert [c["title"] for c in b["ready"]] == ["Thinking indicator", "Plain title"]
    assert b["building"][0] == {"id": "w:1", "title": "Running job", "kind": "worker", "n": 1, "status": "running"}
    assert b["building"][1]["kind"] == "backlog"
    done = [c["title"] for c in b["done"]]
    assert done[0].startswith("Mail") and "@" not in done[0]  # newest finished first, email redacted
    assert "Ancient" not in done and "Sticky build mode" in done
    assert b["counts"] == {"ready": 2, "building": 2, "done": 3}


def test_pipeline_states():
    lanes = [{"repo": "Walter", "dirty": False, "commits": [{"h": "abc1234", "ago": "2 min ago"}]},
             {"repo": "Site", "dirty": False, "commits": [{"h": "def", "ago": "1 hour ago"}]}]
    p = {s["stage"]: s for s in C.pipeline(lanes, {"ok": True, "passed": 10}, {"version": "0.1.1", "signed": True,
                                                                             "date": "2026-11-09T18:50:43Z"}, None)}
    assert [p[k]["state"] for k in ("build", "test", "sign", "publish", "live")] == ["ok"] * 5
    assert p["publish"]["note"].startswith("v0.1.1")
    p = {s["stage"]: s for s in C.pipeline([dict(lanes[0], dirty=True)], {"running": True}, None, None)}
    assert p["build"]["state"] == "run" and p["test"]["state"] == "run"
    assert p["sign"]["state"] == p["publish"]["state"] == p["live"]["state"] == "idle"
    p = {s["stage"]: s for s in C.pipeline(lanes, {"ok": False, "failed": 2}, None, {"when": 0, "ok": False})}
    assert p["test"]["state"] == "bad" and p["test"]["note"] == "2 failing" and p["live"]["state"] == "bad"


def test_tests_state_running_and_stale(tmp_path):
    f = tmp_path / "t.json"
    f.write_text(json.dumps({"running": True, "started": 1000, "done": 5, "total": 50, "passed": 5, "failed": 0,
                             "failures": ["tests/x.py::test_a - token=abcdef1234567890"]}))
    t = C.tests_state(f, now=1010, words=[])
    assert t["running"] and t["done"] == 5 and t["total"] == 50
    assert "abcdef1234567890" not in t["failures"][0]
    assert C.tests_state(f, now=1000 + 4000, words=[])["running"] is False  # died without a result
    assert C.tests_state(tmp_path / "none.json") is None


def test_remote_release_is_cached(tmp_path):
    calls = []

    def fetch(url):
        calls.append(url)
        return json.dumps({"version": "1.2.3", "date": "2026-11-09", "signature": "x"})

    cache = tmp_path / "c.json"
    d = C.remote_release(1000, cache, fetch=fetch)
    assert d["version"] == "1.2.3" and d["signed"] is True
    assert C.remote_release(1100, cache, fetch=fetch)["version"] == "1.2.3" and len(calls) == 1
    C.remote_release(1000 + C.RELEASE_MAX_AGE + 1, cache, fetch=lambda u: (_ for _ in ()).throw(OSError()))
    assert C.remote_release(1000 + C.RELEASE_MAX_AGE + 2, cache, fetch=fetch)["version"] == "1.2.3"


def test_gauges_from_proc(tmp_path):
    cache = tmp_path / "cpu.json"
    assert C.cpu_percent("cpu 100 0 100 800 0 0 0", cache) is None  # first call: no baseline
    assert C.cpu_percent("cpu 150 0 150 900 0 0 0", cache) == 50.0
    assert C.mem_percent("MemTotal: 1000 kB\nMemAvailable: 250 kB\n") == 75.0


def test_plan_install_redacts(tmp_path):
    src = tmp_path / "PLAN.md"
    src.write_text("# Plan for Alice\n- read /home/walter/.walter/home/memory.md\n- mail someone@example.com key sk-abcdefghijklmnop\n")
    out = C.install_plan(src, ["Alice"], tmp_path / "out.md")
    body = out.read_text()
    assert "Alice" not in body and "someone@example.com" not in body and "sk-abcdefghijklmnop" not in body
    assert "memory.md" in body and "/home/walter" not in body and body.startswith("# Plan for")


def test_run_tests_writes_running_then_result(tmp_path):
    repo = tmp_path / "r"
    (repo / "tests").mkdir(parents=True)
    (repo / "tests" / "test_a.py").write_text("def test_ok():\n    pass\n\ndef test_bad():\n    assert 1 == 2\n")
    out = tmp_path / "last.json"
    face = tmp_path / "face.json"
    orig = C.FACE_TESTS
    C.FACE_TESTS = face
    try:
        rc = C.run_tests(repo, path=out, node=False, echo=None)
    finally:
        C.FACE_TESTS = orig
    d = json.loads(out.read_text())
    assert rc == 1 and d["running"] is False and d["passed"] == 1 and d["failed"] == 1 and d["ok"] is False
    assert any("test_bad" in f for f in d["failures"])
    assert json.loads(face.read_text())["failed"] == 1


def test_parse_failures():
    assert C.parse_failures("x\nFAILED tests/a.py::t - AssertionError\nERROR tests/b.py\n") == [
        "tests/a.py::t - AssertionError", "tests/b.py"]


def test_workerlive_keeps_40_redacted_steps(tmp_path, monkeypatch):
    p = tmp_path / "s.jsonl"
    lines = []
    for i in range(50):
        lines.append(json.dumps({"type": "assistant", "timestamp": "2026-11-09T10:00:00Z", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": f"/tmp/f{i}.py"}}]}}))
    lines.append(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q"}}]}}))
    lines.append(json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "WebSearch", "input": {"query": "ask bob@example.com"}}]}}))
    p.write_text("\n".join(lines) + "\n")
    tr = WL.Tracker(p)
    tr.update()
    assert len(tr.log) == WL.STEP_KEEP == 40
    assert tr.log[-2]["k"] == "test" and tr.log[0]["k"] == "read"
    assert "@" not in tr.log[-1]["s"] and tr.log[-1]["k"] == "web"


def test_snapshot_has_cockpit(tmp_path, monkeypatch):
    from walter import buildview as B
    r = tmp_path / "r"
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "init", "-q", str(r)], check=True, env=env)
    subprocess.run(["git", "-C", str(r), "commit", "-q", "--allow-empty", "-m", "first [skip ci]"], check=True, env=env)
    monkeypatch.setattr(C, "remote_release", lambda now=None: None)
    monkeypatch.setattr(C, "BACKLOG", tmp_path / "none.md")
    monkeypatch.setattr(C, "DATA", tmp_path)
    monkeypatch.setattr(B, "DATA", tmp_path)
    monkeypatch.setattr(B, "RELEASE_FILES", [tmp_path / "none.json"])
    snap = B.snapshot(now=2e9, repos=[("Walter", r)], words=[])
    ck = snap["cockpit"]
    assert ck["lanes"][0]["repo"] == "Walter" and ck["lanes"][0]["commits"][0]["s"] == "first"
    assert [s["stage"] for s in ck["pipeline"]] == ["build", "test", "sign", "publish", "live"]
    assert set(ck["gauges"]) >= {"cpu", "mem", "load", "week", "workers"}
