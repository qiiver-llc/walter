#!/usr/bin/env python3
"""Stand-in for `claude -p --input-format stream-json ...` used in tests.

Replies to every user message with a canned, tagged, streamed answer and
emits the same event shapes real Claude Code does.
"""
import json
import sys
import time

SID = "00000000-0000-4000-8000-000000000000"


def emit(obj):
    obj.setdefault("session_id", SID)
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


if sys.argv[1:3] == ["auth", "status"]:
    import os
    marker = os.environ.get("FAKE_CLAUDE_LOGGED_OUT")  # path; logged out until it exists
    print(json.dumps({"loggedIn": not marker or os.path.exists(marker)}))
    sys.exit(0)

if sys.argv[1:3] == ["auth", "login"]:
    print("Opening browser to sign in…")
    print("If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?code=true&state=fake")
    sys.stdout.write("Paste code here if prompted > ")
    sys.stdout.flush()
    code = sys.stdin.readline().strip()
    if code == "good-code":
        import os
        if os.environ.get("FAKE_CLAUDE_LOGGED_OUT"):
            open(os.environ["FAKE_CLAUDE_LOGGED_OUT"], "w").close()
        print("Login successful.")
        sys.exit(0)
    print("Login failed: That code wasn't accepted.")
    sys.exit(1)

if "--input-format" not in sys.argv:
    # one-shot mode (background task workers): `claude -p "<prompt>" --output-format json`
    import os
    time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "1.5")))
    if os.environ.get("FAKE_CLAUDE_TASK_ID_FILE"):  # lets tests check the worker's environment
        with open(os.environ["FAKE_CLAUDE_TASK_ID_FILE"], "w") as f:
            f.write(os.environ.get("WALTER_TASK_ID", "") + " " + os.getcwd())
    prompt = sys.argv[sys.argv.index("-p") + 1]
    title = prompt.split("Task: ", 1)[-1].splitlines()[0]
    emit({"type": "result", "subtype": "success", "is_error": False,
          "result": f"Finished '{title}'. Found 3 options; the cheapest is 42 pounds."})
    sys.exit(0)

model = sys.argv[sys.argv.index("--model") + 1] if "--model" in sys.argv else "default"
emit({"type": "system", "subtype": "init", "cwd": ".", "tools": [], "model": model})
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get("type") == "control_request" and msg["request"].get("subtype") == "set_model":
        model = msg["request"]["model"]
        emit({"type": "control_response", "response": {"subtype": "success", "request_id": msg["request_id"],
                                                       "response": {"model": model}}})
        continue
    if msg.get("type") != "user":
        continue
    text = msg["message"]["content"]
    if "which model" in text:
        reply = [f"[happy] I am {model}."]
    elif "[heartbeat" in text:
        reply = ["[silent]"]
    elif "say:" in text:        # reply with exactly the words after "say:"
        reply = [text.split("say:", 1)[1].splitlines()[0].strip()]
    elif "QUICKSTOP" in text:   # notices an interrupt at once, like real Claude between
        # tokens: ends the turn with is_error / error_during_execution
        emit({"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
                                                "content_block": {"type": "text", "text": ""}}, "parent_tool_use_id": None})
        emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                "delta": {"type": "text_delta", "text": "[thinking] Let me see. "}},
              "parent_tool_use_id": None})
        nxt = json.loads(sys.stdin.readline() or "{}")
        if nxt.get("type") == "control_request":
            emit({"type": "control_response", "response": {"subtype": "success",
                                                           "request_id": nxt.get("request_id")}})
        emit({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": ""})
        continue
    elif "SLOWSTORY" in text:   # a long reply (tests interrupt it; like real Claude
        # mid-tool, this fake only reads the interrupt after the turn: a late result)
        reply = ["[happy] Once upon a time. "] + [f"Story part {i}. " for i in range(12)]
    else:
        reply = ["[hap", "py] Hello there, nice to", " meet you! [action:bou", "nce] ",
                 "I'm Walter.", " [curious] What should we", " do today?"]
    emit({"type": "stream_event", "event": {"type": "content_block_start", "index": 0,
                                            "content_block": {"type": "text", "text": ""}}, "parent_tool_use_id": None})
    for chunk in reply:
        time.sleep(0.05)
        emit({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                "delta": {"type": "text_delta", "text": chunk}}, "parent_tool_use_id": None})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "".join(reply)})
