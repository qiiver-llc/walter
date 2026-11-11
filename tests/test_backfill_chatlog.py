"""tools/backfill_chatlog.py: the transcript parser and the idempotent import."""
import importlib.util
import json
import time
from pathlib import Path

from walter import chatlog as C

spec = importlib.util.spec_from_file_location("backfill_chatlog", Path(__file__).parent.parent / "tools" / "backfill_chatlog.py")
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)


def ts(sec):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 3600 + sec))


def user(sec, content):
    return {"type": "user", "timestamp": ts(sec), "message": {"role": "user", "content": content}}


def said(sec, *blocks):
    return {"type": "assistant", "timestamp": ts(sec), "message": {"role": "assistant", "content": list(blocks)}}


def text(t):
    return {"type": "text", "text": t}


def write(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def fake_session(tmp_path):
    rows = [
        user(0, "[heartbeat Tuesday 10 November 2026, 09:00] Nobody has talked to you for a while."),
        said(1, text("[silent]")),
        user(10, "[workspace: Acme]\n[heard through the microphone at Tuesday 10 November 2026, 09:10; "
                 "transcription may have small errors; speaker: the owner (voice match)]\nWhat's the weather?"),
        said(11, {"type": "tool_use", "name": "Bash", "input": {"command": "curl wttr.in"}}),
        user(12, [{"type": "tool_result", "content": "sunny"}]),
        said(13, text("[happy] Sunny all day. [action:grow]")),
        user(20, "[background task finished, Tuesday]\nTask #1 done."),
        said(21, text("The build finished.")),
        user(30, "[You already said \"One sec.\" out loud as an instant acknowledgement.]\n[typed in the companion "
                 "app on Phone (your person) at Tuesday; they may be away from home. Your reply goes back to the "
                 "app only.]\nadd milk to the list"),
        said(31, text("Added milk. ```code``` Done.")),
        user(40, "[heard on face Kitchen (room: kitchen) at Tuesday; best mic, score 0.80; transcription may have "
                 "small errors. You are talking in that room.]\nturn it up"),
        said(41, text("[think:hard]")),
        user(42, "[heard on face Kitchen (room: kitchen) at Tuesday; best mic, score 0.80; transcription may have "
                 "small errors. You are talking in that room.]\nturn it up"),
        said(43, text("Louder it is.")),
        user(50, "[scheduled routine due now, Tuesday] morning briefing"),
        said(51, text("Morning! Sunny.")),
        user(60, "[typed on your keyboard at Tuesday]\nsk-ant-" "api03-abcdefghijklmnopqrstu"),
        said(61, text("Please don't paste keys here.")),
    ]
    write(tmp_path / "brain.jsonl", rows)
    write(tmp_path / "worker.jsonl", [user(0, "You are a background worker for Walter. Task: x"),
                                      said(1, text("[heard through the microphone at x]\nnot a turn"))])
    return tmp_path


def test_parser_keeps_only_the_conversation(tmp_path):
    msgs = B.prepare(B.collect(fake_session(tmp_path), days=1))
    got = [(m["role"], m["source"], m["text"]) for m in msgs]
    assert got == [
        ("person", "wall", "What's the weather?"),
        ("walter", "wall", "Sunny all day."),
        ("person", "app", "add milk to the list"),
        ("walter", "app", "Added milk. Done."),
        ("person", "face:Kitchen", "turn it up"),
        ("walter", "face:Kitchen", "Louder it is."),
        ("walter", "wall", "Please don't paste keys here."),
    ]
    assert msgs[0]["workspace"] == "acme"


def test_backfill_is_idempotent_and_merges_live(tmp_path):
    (tmp_path / "t").mkdir()
    src = fake_session(tmp_path / "t")
    log = C.ChatLog(tmp_path / "chat-history.jsonl")
    live = log.append("person", "a live message", "app", ts=time.time())
    res = B.backfill(log, B.collect(src, days=1))
    assert res["imported"] == 7
    rows = [json.loads(x) for x in open(log.path)]
    assert [r["id"] for r in rows] == list(range(1, 9))
    assert rows[-1]["text"] == live["text"] and "imported" not in rows[-1]
    assert all(r.get("imported") for r in rows[:-1])
    assert B.backfill(log, B.collect(src, days=1)) == {"imported": 0, "skipped": "already backfilled"}
    assert len(open(log.path).readlines()) == 8
    assert log.append("walter", "next", ts=time.time() + 1)["id"] == 9
