"""Hybrid realtime mode (realtime.py) against a fake Realtime WebSocket server.

No network (the fake server listens on 127.0.0.1), no real key, nothing under
~/.walter (conftest isolates speech.json / usage; brain home is a temp dir)."""
import asyncio
import base64
import json
import logging
import time

import numpy as np
import pytest
from aiohttp import web

from walter.config import Config
from walter.conductor import Conductor
from walter.realtime import (CaptionCutter, Realtime, StreamPlayer, TOOLS, Upsampler,
                             instructions, word_timings)
from walter.speech import Speech

FAKE_KEY = "sk-test-FAKEREALTIME000000000000"
SECRET_REQUEST = "what is on my calendar zebra-request"
SECRET_ANSWER = "You have the dentist at 3 PM, quokka-answer."


# ----------------------------------------------------------------- fakes
class FakeRealtimeServer:
    """Stands in for wss://api.openai.com/v1/realtime."""

    def __init__(self):
        self.events: list[dict] = []
        self.headers: dict = {}
        self.query: dict = {}
        self.reject = 0               # HTTP status to refuse the handshake with
        self.ws: web.WebSocketResponse | None = None
        self.responses = 0
        self.script = None            # async fn(server, n) for the n-th response.create
        self.connections = 0
        self.runner = None
        self.url = ""

    async def start(self):
        app = web.Application()
        app.router.add_get("/v1/realtime", self.handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/v1/realtime"

    async def stop(self):
        await self.runner.cleanup()

    async def handler(self, request):
        self.connections += 1
        self.headers = dict(request.headers)
        self.query = dict(request.query)
        if self.reject:
            return web.Response(status=self.reject)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.ws = ws
        await ws.send_json({"type": "session.created", "session": {"type": "realtime"}})
        async for msg in ws:
            ev = json.loads(msg.data)
            self.events.append(ev)
            if ev["type"] == "session.update":
                await ws.send_json({"type": "session.updated", "session": ev["session"]})
            elif ev["type"] == "response.create":
                self.responses += 1
                if self.script:
                    asyncio.ensure_future(self.script(self, self.responses))
        return ws

    def of(self, kind: str) -> list[dict]:
        return [e for e in self.events if e["type"] == kind]

    async def send(self, ev: dict):
        await self.ws.send_json(ev)

    # canned replies
    async def function_call(self, n: int, name: str, args: dict, call_id: str = "call_1"):
        rid = f"resp_{n}"
        await self.send({"type": "response.created", "response": {"id": rid, "status": "in_progress"}})
        await self.send({"type": "response.function_call_arguments.done", "response_id": rid,
                         "item_id": "fc_1", "call_id": call_id, "name": name,
                         "arguments": json.dumps(args), "output_index": 0})
        await self.send({"type": "response.done", "response": {
            "id": rid, "status": "completed",
            "output": [{"type": "function_call", "name": name, "call_id": call_id,
                        "arguments": json.dumps(args)}],
            "usage": {"input_token_details": {"text_tokens": 900, "audio_tokens": 0,
                                              "cached_tokens": 0},
                      "output_token_details": {"text_tokens": 20, "audio_tokens": 0}}}})

    async def audio_reply(self, n: int, text: str, seconds: float = 0.3, done: bool = True,
                          pause: float = 0.0):
        rid, item = f"resp_{n}", f"item_{n}"
        await self.send({"type": "response.created", "response": {"id": rid, "status": "in_progress"}})
        await self.send({"type": "response.output_item.added", "response_id": rid,
                         "item": {"id": item, "type": "message", "role": "assistant"}})
        pcm = (np.sin(np.arange(int(24000 * seconds)) * 0.05) * 3000).astype("<i2").tobytes()
        step = 4800
        for i in range(0, len(pcm), step):
            await self.send({"type": "response.output_audio.delta", "response_id": rid, "item_id": item,
                             "content_index": 0, "output_index": 0,
                             "delta": base64.b64encode(pcm[i:i + step]).decode()})
            if pause:
                await asyncio.sleep(pause)
        for w in text.split(" "):
            await self.send({"type": "response.output_audio_transcript.delta", "response_id": rid,
                             "item_id": item, "delta": w + " "})
        if not done:
            return
        await self.send({"type": "response.output_audio_transcript.done", "response_id": rid,
                         "item_id": item, "transcript": text})
        await self.send({"type": "response.done", "response": {
            "id": rid, "status": "completed",
            "usage": {"input_token_details": {"text_tokens": 1000, "audio_tokens": 40,
                                              "cached_tokens": 800,
                                              "cached_tokens_details": {"text_tokens": 800,
                                                                        "audio_tokens": 0}},
                      "output_token_details": {"text_tokens": 12, "audio_tokens": 70}}}})


class FakeBrain:
    def __init__(self, reply=f"[happy] {SECRET_ANSWER} [action:bounce]"):
        self.asked: list[str] = []
        self.reply = reply
        self.delay = 0.0
        self.interrupted = 0

    async def ask(self, text, model=None):
        self.asked.append(text)
        if self.delay:
            await asyncio.sleep(self.delay)
        for piece in (self.reply[:9], self.reply[9:]):
            yield ("text", piece)
        yield ("done", "")

    async def interrupt(self):
        self.interrupted += 1


class FakeVoice:
    def __init__(self):
        self.said, self.generation, self.hushed = [], 0, False

    def say(self, t, provider=None):
        self.said.append(t)

    def put(self, k, v=None, sound=True):
        pass

    def stop(self):
        self.generation += 1


class FakeEars:
    def __init__(self):
        import threading
        self.audio_tap = None
        self.muted, self.speaking, self.user_muted = (threading.Event() for _ in range(3))


def make(tmp_path, server_url, **speech):
    cfg = Config()
    cfg.brain.home = str(tmp_path / "home")
    cfg.speech.key_file = str(tmp_path / "openai.key")
    cfg.speech.realtime_url = server_url
    cfg.speech.mode = "realtime"
    cfg.speech.realtime_idle_close_s = 30
    for k, v in speech.items():
        setattr(cfg.speech, k, v)
    (tmp_path / "openai.key").write_text(FAKE_KEY)
    sp = Speech(cfg, data_dir=tmp_path)
    sent: list[dict] = []
    brain = FakeBrain()
    c = Conductor(cfg, brain=brain, send=sent.append)
    c.voice = FakeVoice()
    c.speech = sp
    c.ears = FakeEars()
    c.barge_mode = "aec"          # echo canceller on: the mic keeps streaming while he talks
    rt = Realtime(cfg, sp, c)
    rt.bind_player(StreamPlayer(enabled=False), asyncio.get_running_loop())
    c.realtime = rt
    _made.append(rt)
    return c, rt, sp, brain, sent


_made: list = []


async def until(cond, timeout=4.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def run_with_server(fn):
    async def go():
        srv = FakeRealtimeServer()
        await srv.start()
        try:
            await fn(srv)
        finally:
            while _made:
                await _made.pop().shutdown()
            await srv.stop()
    asyncio.run(go())


# ----------------------------------------------------------------- pure helpers
def test_upsampler_rate_and_continuity():
    up = Upsampler()
    t = np.arange(16000)
    x = (np.sin(2 * np.pi * 440 * t / 16000) * 10000).astype("<i2")
    out = b"".join(up(x[i:i + 480].tobytes()) for i in range(0, len(x), 480))
    y = np.frombuffer(out, "<i2")
    assert abs(len(y) - 24000) <= 2
    ref = np.sin(2 * np.pi * 440 * np.arange(len(y)) / 24000) * 10000
    assert np.max(np.abs(y - ref)) < 400       # linear interpolation, no seams at frame edges


def test_word_timings_and_caption_cutter():
    w = word_timings("Oh nice, that works.", 2.0)
    assert [x[0] for x in w] == ["Oh", "nice,", "that", "works."]
    assert w[0][1] == 0 and w[-1][2] <= 2.0 and all(a[1] < b[1] for a, b in zip(w, w[1:]))
    cut = CaptionCutter()
    assert cut.feed("Hello there") == []
    got = cut.feed("! How are ")
    assert got == [("Hello there!", 0)]
    assert cut.flush() == [("How are", 13)]


def test_instructions_persona_override_and_time(tmp_path):
    import datetime as dt
    text = instructions(tmp_path, dt.datetime(2026, 11, 4, 15, 5))
    assert "Walter" in text and "3:05 PM" in text and "ask_walter" in text
    (tmp_path / "realtime.md").write_text("Custom persona.")
    assert instructions(tmp_path, dt.datetime(2026, 11, 4, 9, 0)).startswith("Custom persona.")
    names = {t["name"] for t in TOOLS}
    assert names == {"ask_walter", "set_face", "stop_listening", "go_to_sleep", "mute"}
    face = next(t for t in TOOLS if t["name"] == "set_face")["parameters"]["properties"]
    assert "love" not in face["emotion"]["enum"] and "laugh:big" not in face["action"]["enum"]
    assert {"laugh", "grow", "shrink", "scoot:left", "peek"} <= set(face["action"]["enum"])


def test_mode_switch_persists(tmp_path):
    cfg = Config()
    sp = Speech(cfg, data_dir=tmp_path)
    assert sp.mode == "classic"
    seen = []
    sp.on_mode = seen.append
    sp.set("mode", "realtime")
    assert seen == ["realtime"]
    assert json.loads((tmp_path / "speech.json").read_text())["mode"] == "realtime"
    assert Speech(cfg, data_dir=tmp_path).mode == "realtime"
    with pytest.raises(ValueError):
        sp.set_mode("turbo")


# ----------------------------------------------------------------- sessions
def test_session_lifecycle_tool_round_trip_captions_and_usage(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    async def script(srv, n):
        if n == 1:   # the model hands the request to Walter's brain
            await srv.function_call(n, "ask_walter", {"request": SECRET_REQUEST})
        else:        # ...and speaks the answer
            await srv.audio_reply(n, SECRET_ANSWER)

    async def fn(srv):
        srv.script = script
        c, rt, sp, brain, sent = make(tmp_path, srv.url)
        c.on_heard("Hey Walter, " + SECRET_REQUEST)
        assert c.inbox.empty()                       # realtime took it, not the classic brain loop
        await until(lambda: rt.active)
        # handshake: model in the URL, bearer key, GA session shape
        assert srv.query["model"] == "gpt-realtime-2.1-mini"
        assert srv.headers["Authorization"] == "Bearer " + FAKE_KEY
        su = srv.of("session.update")[0]["session"]
        assert su["type"] == "realtime" and su["output_modalities"] == ["audio"]
        assert su["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
        # the server never decides on its own when to answer
        assert su["audio"]["input"]["turn_detection"] is None
        assert su["max_output_tokens"] == 600
        assert su["audio"]["output"]["voice"] == "ash"
        assert {t["name"] for t in su["tools"]} >= {"ask_walter", "set_face"}
        # the first utterance (heard locally, before the session) goes in as text
        item = srv.of("conversation.item.create")[0]["item"]
        assert item["role"] == "user" and SECRET_REQUEST in item["content"][0]["text"]
        # tool round trip: brain asked, answer returned, face cue shown, second response
        await until(lambda: len(srv.of("response.create")) >= 2)
        assert SECRET_REQUEST in brain.asked[0] and "realtime" in brain.asked[0]
        out = [e for e in srv.of("conversation.item.create")
               if e["item"]["type"] == "function_call_output"][0]["item"]
        assert out["call_id"] == "call_1"
        assert json.loads(out["output"])["answer"] == SECRET_ANSWER
        assert {"t": "emotion", "name": "happy", "hold": 30} in sent
        assert {"t": "action", "name": "bounce"} in sent
        # spoken: captions with word timings, speaking state, then back to listening
        await until(lambda: any(m.get("t") == "caption" and m.get("who") == "walter" for m in sent))
        cap = [m for m in sent if m.get("t") == "caption" and m.get("who") == "walter"][0]
        assert cap["words"] and cap["dur"] > 0
        assert {"t": "state", "state": "speaking"} in sent
        await until(lambda: not rt.player.playing and c.state == "listening")
        assert c.followup_until > time.time()
        # mic frames tapped from the ears stay in memory: nothing is sent by itself
        assert c.ears.audio_tap == rt.on_mic
        rt.on_mic(np.zeros(480, "<i2").tobytes())
        await asyncio.sleep(0.2)
        assert len(rt._ring) == 1 and not srv.of("input_audio_buffer.append")
        # usage: tokens and open seconds are counted; cost estimate is positive
        day = sp._day()
        assert day.get("rt_audio_out") == 70 and day.get("rt_text_in_cached") == 800
        assert sp.status()["realtime"]["approx_cost_today_usd"] > 0
        # the face shows the live cloud mark while streaming
        assert sp.indicator()["rt"] is True
        # what was said is waiting for the brain's next classic turn
        frame = c._frame("text", "anything else?")
        assert frame.startswith("[realtime exchange") and "you: " in frame
        await rt.close("test")
        assert not rt.active and c.ears.audio_tap is None and sp.indicator()["rt"] is False
        assert sp._day().get("rt_seconds", 0) > 0
    run_with_server(fn)
    logs = "\n".join(r.getMessage() for r in caplog.records)
    assert SECRET_REQUEST not in logs and "quokka" not in logs and FAKE_KEY not in logs


def test_interruption_truncates_to_what_was_heard(tmp_path):
    async def script(srv, n):
        # a long reply, streamed slowly so it is still playing when they talk
        await srv.audio_reply(n, "This is a rather long answer.", seconds=3.0, done=False, pause=0.01)

    async def fn(srv):
        srv.script = script
        c, rt, sp, brain, sent = make(tmp_path, srv.url)
        c.on_heard("Walter, tell me a story")
        await until(lambda: rt.player.playing)
        await asyncio.sleep(0.3)
        # they talk over him (classic barge-in rule, judged locally)
        c.on_hear_start()
        c.on_hear_partial("hang on, tell me about trains instead")
        await until(lambda: srv.of("conversation.item.truncate"))
        assert srv.of("response.cancel")
        tr = srv.of("conversation.item.truncate")[0]
        assert tr["item_id"] == "item_1" and tr["content_index"] == 0
        assert 100 <= tr["audio_end_ms"] <= 3000
        assert not rt.player.playing and rt.stats["interruptions"] == 1
        assert c.state == "listening"
        # the server cancels; nothing breaks
        await srv.send({"type": "response.done", "response": {"id": "resp_1", "status": "cancelled"}})
        await asyncio.sleep(0.1)
        assert rt.state == "open"
        await rt.close("test")
    run_with_server(fn)


def test_bare_name_acks_locally_then_streams(tmp_path):
    async def fn(srv):
        c, rt, sp, brain, sent = make(tmp_path, srv.url)
        c.on_heard("Hey Walter.")
        assert c.voice.said                     # instant local "Yes?"
        await until(lambda: rt.active)
        assert not srv.of("response.create")    # nothing asked yet: waits for a real utterance
        await rt.close("test")
    run_with_server(fn)


def test_handshake_error_falls_back_to_classic(tmp_path):
    async def fn(srv):
        srv.reject = 401
        c, rt, sp, brain, sent = make(tmp_path, srv.url)
        c.on_heard("Walter, what's the weather?")   # (not a fast-path command)
        item = await asyncio.wait_for(c.inbox.get(), 4)   # handled the classic way instead
        assert item[0] == "voice" and "weather" in item[1]
        assert rt.state == "closed" and rt.stats["fallbacks"] == 1
        assert "401" in rt.last_error and rt.problem()
        # while cooling down, utterances go straight to the classic brain
        c.on_heard("Walter, and the date?")
        item = await asyncio.wait_for(c.inbox.get(), 1)
        assert "date" in item[1] and srv.connections == 1
        # switching the mode on again clears the cool-down
        sp.set_mode("realtime")
        assert not rt.problem()
    run_with_server(fn)


def test_fatal_server_error_closes_and_falls_back(tmp_path):
    async def script(srv, n):
        await srv.send({"type": "error", "error": {"type": "invalid_request_error",
                                                   "code": "session_expired", "message": "x"}})

    async def fn(srv):
        srv.script = script
        c, rt, sp, brain, sent = make(tmp_path, srv.url)
        c.on_heard("Walter, hello there")
        await until(lambda: rt.stats["errors"] >= 1)
        await until(lambda: rt.state == "closed")
        assert "session_expired" in rt.last_error and rt.problem()
        assert sp.status()["realtime"]["problem"]
    run_with_server(fn)


def test_benign_error_keeps_session(tmp_path):
    async def fn(srv):
        c, rt, sp, brain, sent = make(tmp_path, srv.url)
        rt.prewarm()
        await until(lambda: rt.state == "open")
        await srv.send({"type": "error", "error": {"code": "response_cancel_not_active"}})
        await asyncio.sleep(0.1)
        assert rt.state == "open" and rt.stats["errors"] == 0
        await rt.close("test")
    run_with_server(fn)


def test_daily_cap_means_classic(tmp_path):
    async def fn(srv):
        c, rt, sp, brain, sent = make(tmp_path, srv.url, max_realtime_min_per_day=1.0)
        sp.add_rt(seconds=60)
        assert "daily cap" in rt.problem()
        c.on_heard("Walter, what's up?")
        item = c.inbox.get_nowait()
        assert item[0] == "voice" and srv.connections == 0
        assert sp.status()["realtime"]["room"] != "ok"
    run_with_server(fn)


def test_cap_reached_mid_session_closes(tmp_path, monkeypatch):
    import walter.realtime as rtmod
    monkeypatch.setattr(rtmod, "USAGE_EVERY_S", 0.2)

    async def fn(srv):
        c, rt, sp, brain, sent = make(tmp_path, srv.url, max_realtime_min_per_day=0.02)  # 1.2 s
        rt.prewarm(stream=True)
        await until(lambda: rt.active)
        await until(lambda: rt.state == "closed", timeout=5)
        assert "cap" in sp.rt_problem
    run_with_server(fn)


def test_idle_close_and_mode_switch_close(tmp_path):
    async def fn(srv):
        c, rt, sp, brain, sent = make(tmp_path, srv.url, realtime_idle_close_s=1.0)
        rt.prewarm(stream=True)
        await until(lambda: rt.active)
        await until(lambda: rt.state == "closed", timeout=4)   # nothing said: closes itself
        rt.prewarm(stream=True)
        await until(lambda: rt.active)
        sp.set("mode", "classic")                              # live switch back
        await until(lambda: rt.state == "closed")
        c.on_heard("Walter, what's the weather?")
        assert c.inbox.get_nowait()[0] == "voice"
    run_with_server(fn)


def test_mute_closes_session(tmp_path):
    async def fn(srv):
        c, rt, sp, brain, sent = make(tmp_path, srv.url)
        rt.prewarm(stream=True)
        await until(lambda: rt.active)
        c.set_user_mute(True)
        await until(lambda: rt.state == "closed")
        assert c.ears.audio_tap is None
    run_with_server(fn)


def test_set_face_and_stop_listening_tools(tmp_path):
    async def script(srv, n):
        if n == 1:
            await srv.function_call(n, "set_face", {"emotion": "excited", "action": "grow"}, "call_f")
        elif n == 2:
            await srv.audio_reply(n, "Bye for now!", seconds=0.2)
            await srv.function_call(n + 100, "stop_listening", {}, "call_s")

    async def fn(srv):
        srv.script = script
        c, rt, sp, brain, sent = make(tmp_path, srv.url)
        c.on_heard("Walter, that's all, thanks")
        await until(lambda: {"t": "action", "name": "grow"} in sent)
        assert {"t": "emotion", "name": "excited", "hold": 30} in sent
        # set_face alone: he carries on with a spoken reply
        await until(lambda: len(srv.of("response.create")) >= 2)
        await until(lambda: rt.state == "closed", timeout=5)
        assert not brain.asked
    run_with_server(fn)


async def _streaming(tmp_path, srv, **speech):
    c, rt, sp, brain, sent = make(tmp_path, srv.url, **speech)
    rt.prewarm(stream=True)
    await until(lambda: rt.active)
    return c, rt, sp, brain, sent


def _feed(rt, seconds: float, at: float | None = None):
    """Mic frames (30 ms, 16 kHz) as the audio thread would deliver them."""
    frame = (np.ones(480) * 500).astype("<i2").tobytes()
    for _ in range(int(seconds / 0.03)):
        rt.on_mic(frame)


def test_noise_and_unmeant_speech_get_no_reply(tmp_path):
    async def fn(srv):
        c, rt, sp, brain, sent = await _streaming(tmp_path, srv)
        c.followup_until = time.time() + 30
        # a train rumbles by: the ears start a segment, then reject it (Silero)
        c.on_hear_start()
        _feed(rt, 2.0)
        c.on_hear_dropped()
        # people talking to each other, after the follow-up window
        c.followup_until = time.time() - 1
        c.on_hear_start()
        _feed(rt, 1.0)
        c.on_heard("did you see the train go past just now")
        await asyncio.sleep(0.3)
        assert not srv.of("response.create")
        assert not srv.of("input_audio_buffer.append") and not srv.of("input_audio_buffer.commit")
        assert not srv.of("conversation.item.create")
        assert rt._sess.get("noise") == 1 and rt._sess.get("ignored") == 1
        assert not brain.asked
        await rt.close("test")
    run_with_server(fn)


def test_confirmed_utterance_sends_its_audio_then_asks_for_a_reply(tmp_path, caplog):
    caplog.set_level(logging.INFO)

    async def script(srv, n):
        await srv.audio_reply(n, "Ten past seven.")

    async def fn(srv):
        srv.script = script
        c, rt, sp, brain, sent = await _streaming(tmp_path, srv)
        _feed(rt, 3.0)                       # older room sound: not part of the turn
        await asyncio.sleep(0.6)
        c.followup_until = time.time() + 30
        c.on_hear_start()
        _feed(rt, 1.2)
        c.on_heard("what time is it")
        await until(lambda: srv.of("response.create"))
        kinds = [e["type"] for e in srv.events if e["type"] != "session.update"]
        assert kinds.index("input_audio_buffer.commit") < kinds.index("response.create")
        assert kinds.index("input_audio_buffer.append") < kinds.index("input_audio_buffer.commit")
        samples = sum(len(base64.b64decode(e["audio"])) // 2 for e in srv.of("input_audio_buffer.append"))
        # just this utterance (+ a little pre-roll), at 24 kHz: not the earlier 3 s
        assert 0.9 * 24000 <= samples <= 1.9 * 24000
        assert not rt._ring
        assert not srv.of("conversation.item.create")    # the audio, not a transcript
        await until(lambda: c.state == "listening")
        await rt.close("test")
    run_with_server(fn)
    logs = "\n".join(r.getMessage() for r in caplog.records)
    line = [l for l in logs.splitlines() if l.startswith("realtime session summary")][0]
    assert "1 turns sent" in line and "1 responses" in line and "$" in line
    assert "what time" not in logs and "Ten past" not in logs


def test_text_input_mode_sends_the_transcript(tmp_path):
    async def fn(srv):
        c, rt, sp, brain, sent = await _streaming(tmp_path, srv, realtime_input="text")
        c.followup_until = time.time() + 30
        c.on_hear_start()
        _feed(rt, 1.0)
        c.on_heard("what time is it")
        await until(lambda: srv.of("response.create"))
        item = srv.of("conversation.item.create")[0]["item"]
        assert item["content"][0] == {"type": "input_text", "text": "what time is it"}
        assert not srv.of("input_audio_buffer.append")
        await rt.close("test")
    run_with_server(fn)


def test_barge_in_only_for_real_speech(tmp_path):
    async def script(srv, n):
        await srv.audio_reply(n, "This is a rather long answer.", seconds=4.0, done=False, pause=0.01)

    async def fn(srv):
        srv.script = script
        c, rt, sp, brain, sent = make(tmp_path, srv.url)
        c.on_heard("Walter, tell me a story")
        await until(lambda: rt.player.playing)
        for noise in ("mm hmm", "yeah", "train"):      # backchannels, a single word
            c.on_hear_start()
            c.on_hear_partial(noise)
            c.on_hear_dropped()
        await asyncio.sleep(0.2)
        assert rt.player.playing and not srv.of("conversation.item.truncate")
        assert rt.stats["interruptions"] == 0
        c.on_hear_start()
        c.on_hear_partial("Walter wait")                # his name stops him
        await until(lambda: srv.of("conversation.item.truncate"))
        assert not rt.player.playing and rt.stats["interruptions"] == 1
        await rt.close("test")
    run_with_server(fn)


def test_stop_cancels_the_reply(tmp_path):
    async def script(srv, n):
        await srv.audio_reply(n, "This is a rather long answer.", seconds=4.0, done=False, pause=0.01)

    async def fn(srv):
        srv.script = script
        c, rt, sp, brain, sent = make(tmp_path, srv.url)
        c.on_heard("Walter, tell me a story")
        await until(lambda: rt.player.playing)
        c.on_heard("stop")
        await until(lambda: srv.of("response.cancel"))
        assert not rt.player.playing and len(srv.of("response.create")) == 1
        await rt.close("test")
    run_with_server(fn)


def test_he_is_told_not_to_repeat_his_opener(tmp_path):
    async def script(srv, n):
        await srv.audio_reply(n, "Ooh, nice one.")

    async def fn(srv):
        srv.script = script
        c, rt, sp, brain, sent = make(tmp_path, srv.url)
        c.on_heard("Walter, guess what")
        await until(lambda: rt._openers)
        await until(lambda: c.state == "listening")
        c.followup_until = time.time() + 30
        c.on_heard("I fixed the fence")
        await until(lambda: len(srv.of("response.create")) == 2)
        hint = [e for e in srv.of("session.update")
                if "Your last replies began with" in e["session"].get("instructions", "")]
        assert hint and '"Ooh nice"' in hint[-1]["session"]["instructions"]
        await rt.close("test")
    run_with_server(fn)


def test_default_persona_rules():
    from walter.realtime import DEFAULT_PERSONA
    p = DEFAULT_PERSONA
    assert "You ARE Walter" in p and "ask_walter" in p and "depends" in p
    assert '"Ooh"' in p and "what's next?" in p
