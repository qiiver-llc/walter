import json
import subprocess

from walter import buildview as B


def test_clean_subject_strips_noise_paths_and_private_words():
    s = "Fix the ears for Alice in Springfield [skip ci] Co-Authored-By: X <x@y.z>"
    out = B.clean_subject(s, ["Alice", "Springfield"])
    assert "skip ci" not in out and "Co-Authored" not in out
    assert "Alice" not in out and "Springfield" not in out
    assert out.startswith("Fix the ears for")
    assert B.clean_subject("edit /home/walter/.walter/home/memory.md now", []) == "edit memory.md now"
    assert "@" not in B.clean_subject("mail bob@example.com", [])
    assert "sk-" not in B.clean_subject("key sk-abcdefghijklmnop", [])


def test_clean_subject_trims_to_60():
    out = B.clean_subject("word " * 20, [])
    assert len(out) <= 60 and out.endswith("…")


def test_ago():
    assert B.ago(100, 110) == "just now"
    assert B.ago(0, 12 * 60) == "12 min ago"
    assert B.ago(0, 3 * 3600) == "3 hours ago"
    assert B.ago(0, 86400 + 5) == "yesterday"


def test_snapshot_reads_git_newest_first(tmp_path, monkeypatch):
    r = tmp_path / "r"
    r.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "init", "-q", str(r)], check=True, env=env)
    for i, d in enumerate(["2026-11-09T10:00:00", "2026-11-09T11:00:00"]):
        subprocess.run(["git", "-C", str(r), "commit", "-q", "--allow-empty", "-m", f"step {i} [skip ci]"],
                       check=True, env={**env, "GIT_AUTHOR_DATE": d, "GIT_COMMITTER_DATE": d})
    monkeypatch.setattr(B, "RELEASE_FILES", [tmp_path / "none.json"])
    monkeypatch.setattr(B, "DATA", tmp_path)
    (tmp_path / "last-test.json").write_text(json.dumps({"passed": 10, "failed": 1, "when": 0}))
    snap = B.snapshot(repos=[("Walter", r)], words=[])
    assert [c["line"] for c in snap["shipped"]] == ["step 1", "step 0"]
    assert snap["release"] is None
    assert snap["tests"]["ok"] is False and snap["tests"]["passed"] == 10


def test_release_info(tmp_path):
    p = tmp_path / "latest.json"
    p.write_text(json.dumps({"version": "0.1.1", "date": "2026-11-09T18:50:43Z"}))
    assert B.release_info([p])["version"] == "0.1.1"


# ---- voice-IDE deep view ------------------------------------------------------------------------

def _repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(r), *a], check=True, capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (r / "app.py").write_text("".join(f"line {i}\n" for i in range(40)))
    run("add", ".")
    run("commit", "-qm", "first")
    return r, run


def test_secret_files_never_shown(tmp_path):
    for f in ["secrets/openai.key", "x/server.pem", ".env", "a/.env.local", "id_rsa", "cloudflare.token"]:
        assert B.is_secret_file(f), f
    assert not B.is_secret_file("walter/buildview.py")
    r, run = _repo(tmp_path)
    (r / "secrets").mkdir()
    (r / "secrets" / "k.txt").write_text("sk-abcdefghijklmnopqrstuv\n")
    (r / "my.key").write_text("x\n")
    (r / "app.py").write_text((r / "app.py").read_text().replace("line 30", "changed thirty"))
    ch = B.changed_files(r)
    assert [c["file"] for c in ch] == ["app.py"]


def test_live_diff_last_hunk_redacted(tmp_path):
    r, run = _repo(tmp_path)
    (r / "app.py").write_text((r / "app.py").read_text().replace("line 30", "mail bob@example.com sk-abcdefghijklmnopqrst"))
    d = B.diff_of(r, B.changed_files(r), ["Springfield"])
    assert d["file"] == "app.py" and d["live"]
    plus = [x["s"] for x in d["lines"] if x["k"] == "+"]
    assert plus and "@" not in plus[0] and "sk-abc" not in plus[0]
    assert d["lines"][0]["k"] == "@" and d["lines"][-1]["k"] == " "
    assert len(d["lines"]) <= B.DIFF_MAX_LINES


def test_diff_falls_back_to_last_commit_and_graph(tmp_path):
    r, run = _repo(tmp_path)
    (r / "app.py").write_text("new\n")
    run("commit", "-qam", "second [skip ci]")
    d = B.diff_of(r, B.changed_files(r), [])
    assert d and not d["live"] and any(x["k"] == "-" for x in d["lines"])
    g = B.git_graph(r, [], n=8)
    assert [x["s"] for x in g] == ["second", "first"]


def test_parse_diff_caps_lines():
    text = "@@ -1,200 +1,200 @@\n" + "".join(f"+x{i}\n" for i in range(200))
    out = B.parse_diff(text, [])
    assert len(out) == B.DIFF_MAX_LINES and out[-1]["s"] == "x199"


def test_pytest_summary_and_record(tmp_path):
    assert B.parse_pytest("....\n===== 2 failed, 120 passed, 3 skipped in 4.51s =====")["failed"] == 2
    d = B.parse_pytest("120 passed in 3.2s")
    assert d["ok"] and d["passed"] == 120
    assert B.parse_pytest("nothing here") is None
    p = tmp_path / "last.json"
    B.record_test("5 passed in 1.0s", p, now=10)
    t = B.test_info(p)
    assert t["passed"] == 5 and t["ok"] and t["when"] == 10
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "x.log").write_text("1 failed, 2 passed in 0.5s\n")
    assert B.scan_logs(logs, tmp_path / "new.json")["failed"] == 1
