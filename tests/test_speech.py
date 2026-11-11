"""Local/cloud speech switches (speech.py) against a fake OpenAI server.

No real API calls, no real key, nothing under ~/.walter."""
import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
import wave
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from walter.config import Config
from walter.speech import OPENAI_PCM_SR, Speech

FAKE_KEY = "sk-test-FAKEFAKEFAKEFAKEFAKE1234"
REPO = Path(__file__).resolve().parent.parent


class FakeOpenAI:
    """Tiny threaded HTTP server standing in for api.openai.com/v1."""

    def __init__(self):
        self.requests = []      # (path, headers, body)
        self.mode = "ok"        # ok | error | slow | drip (slow trickle; notices a cut-off client)
        self.aborted = threading.Event()   # drip: the client closed the connection on us
        self.text = "Hey Walter, what's the weather like?"
        self.pcm_seconds = 1.0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(n)
                outer.requests.append((self.path, dict(self.headers), body))
                if outer.mode == "error":
                    self.send_response(500); self.end_headers(); self.wfile.write(b"{}")
                    return
                if outer.mode == "drip":
                    self._drip()
                    return
                if self.path.endswith("/audio/speech"):
                    t = np.arange(int(OPENAI_PCM_SR * outer.pcm_seconds)) / OPENAI_PCM_SR
                    pcm = (np.sin(2 * np.pi * 220 * t) * 8000).astype("<i2").tobytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/pcm")
                    self.end_headers()
                    if outer.mode == "slow":   # first bytes, then a stall past the deadline
                        self.wfile.write(pcm[:1000]); self.wfile.flush()
                        time.sleep(1.5)
                    self.wfile.write(pcm)
                elif self.path.endswith("/audio/transcriptions"):
                    if outer.mode == "slow":
                        time.sleep(1.5)
                    out = json.dumps({"text": outer.text}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                else:
                    self.send_response(404); self.end_headers()

            def _gone(self) -> bool:
                import select
                import socket as so
                r, _, _ = select.select([self.connection], [], [], 0)
                if not r:
                    return False
                try:
                    return self.connection.recv(1, so.MSG_PEEK) == b""
                except OSError:
                    return True

            def _drip(self):
                """Answer very slowly (~4 s), watching for the client to hang up."""
                tts = self.path.endswith("/audio/speech")
                if tts:
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/pcm")
                    self.end_headers()
                piece = (np.ones(2400) * 3000).astype("<i2").tobytes()   # 0.1 s of audio
                for _ in range(80):
                    if self._gone():
                        outer.aborted.set()
                        return
                    try:
                        if tts:
                            self.wfile.write(piece); self.wfile.flush()
                    except OSError:
                        outer.aborted.set()
                        return
                    time.sleep(0.05)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}/v1"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def fake():
    f = FakeOpenAI()
    yield f
    f.close()


def make_speech(tmp_path, fake=None, key=True, **speech):
    cfg = Config()
    if fake is not None:
        cfg.speech.openai_base_url = fake.url
    cfg.speech.tts_timeout_s = speech.pop("tts_timeout_s", 3.0)
    cfg.speech.stt_timeout_s = speech.pop("stt_timeout_s", 3.0)
    for k, v in speech.items():
        setattr(cfg.speech, k, v)
    if key:
        d = tmp_path / "secrets"
        d.mkdir(exist_ok=True)
        (d / "openai.key").write_text(FAKE_KEY + "\n")
    return Speech(cfg, data_dir=tmp_path)


# ------------------------------------------------------------ switches & key
def test_defaults_local_and_switches_persist(tmp_path):
    sp = make_speech(tmp_path, key=False)
    assert (sp.tts, sp.stt) == ("local", "local")
    changes = []
    sp.on_change = lambda: changes.append(1)
    sp.set("voice", "openai")
    sp.set("stt", "openai")
    assert changes == [1, 1]
    assert json.loads((tmp_path / "speech.json").read_text()) == {"tts": "openai", "stt": "openai",
                                                                 "mode": "classic"}
    again = make_speech(tmp_path, key=False)
    assert (again.tts, again.stt) == ("openai", "openai")
    with pytest.raises(ValueError):
        sp.set("tts", "elevenlabs")
    with pytest.raises(ValueError):
        sp.set("nose", "local")


def test_key_from_file_or_env_and_never_in_status(tmp_path, monkeypatch):
    sp = make_speech(tmp_path, key=False)
    assert not sp.has_key()
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    assert sp.has_key()
    monkeypatch.delenv("OPENAI_API_KEY")
    sp2 = make_speech(tmp_path, key=True)
    assert sp2.has_key()
    st = json.dumps(sp2.status())
    assert FAKE_KEY not in st and st.count("sk-") == 0


def test_indicator_only_when_cloud_really_used(tmp_path):
    sp = make_speech(tmp_path, key=False)
    assert sp.indicator() == {"t": "cloud", "tts": False, "stt": False, "rt": False,
                              "idle_tts": False, "idle_stt": False, "idle_rt": False}
    sp.set("tts", "openai")
    assert sp.indicator()["tts"] is False and sp.indicator()["idle_tts"] is True  # no key yet
    sp2 = make_speech(tmp_path, key=True)
    assert sp2.indicator()["tts"] is True and sp2.indicator()["stt"] is False


# ------------------------------------------------------------ TTS
def test_tts_request_resample_usage_and_quiet_logs(tmp_path, fake, caplog):
    caplog.set_level(logging.DEBUG)
    sp = make_speech(tmp_path, fake, tts_voice="onyx")
    text = "Ooh, the build passed."
    audio = sp.synth(text, 22050)
    assert audio is not None and audio.dtype == np.int16
    assert abs(len(audio) - 22050) < 30            # 1 s of 24 kHz audio -> 22.05 kHz
    path, headers, body = fake.requests[0]
    assert path == "/v1/audio/speech"
    assert headers["Authorization"] == "Bearer " + FAKE_KEY
    d = json.loads(body)
    assert d["model"] == "gpt-4o-mini-tts" and d["voice"] == "onyx" and d["input"] == text
    assert d["response_format"] == "pcm" and d["instructions"] and "speed" not in d
    day = sp.status()["today"]
    assert day["tts_chars"] == len(text) and day["tts_calls"] == 1
    assert abs(day["tts_audio_s"] - 1.0) < 0.01
    assert sp.stats["tts_ok"] == 1
    logs = caplog.text
    assert FAKE_KEY not in logs and "build passed" not in logs
    assert "22 chars" in logs
    usage = (tmp_path / "speech-usage.json").read_text()
    assert "build" not in usage and FAKE_KEY not in usage


def test_tts1_sends_speed_not_instructions(tmp_path, fake):
    sp = make_speech(tmp_path, fake, tts_model="tts-1", tts_speed=1.15)
    assert sp.synth("Hello.", 24000) is not None
    d = json.loads(fake.requests[0][2])
    assert d["speed"] == 1.15 and "instructions" not in d


def test_tts_error_and_timeout_fall_back(tmp_path, fake, caplog):
    sp = make_speech(tmp_path, fake, tts_timeout_s=0.5)
    fake.mode = "error"
    assert sp.synth("Hello there.") is None
    assert sp.stats["tts_fallback"] == 1 and "HTTP 500" in sp.last_error["tts"]
    fake.mode = "slow"
    t0 = time.monotonic()
    assert sp.synth("Hello there.") is None
    assert time.monotonic() - t0 < 1.4
    assert sp.stats["tts_fallback"] == 2
    assert "tts_chars" not in sp.status()["today"]   # failures aren't counted as usage
    assert "Hello there" not in caplog.text


def test_tts_timeout_override_for_the_quick_retry(tmp_path, fake):
    sp = make_speech(tmp_path, fake, tts_timeout_s=5.0)
    fake.mode = "slow"
    t0 = time.monotonic()
    assert sp.synth("Hello there.", timeout=0.5) is None
    assert time.monotonic() - t0 < 1.4
    assert "0.5 s" in sp.last_error["tts"] or "Timeout" in sp.last_error["tts"]


def test_tts_without_key_or_server_falls_back(tmp_path):
    sp = make_speech(tmp_path, key=False)
    assert sp.synth("Hi.") is None and sp.last_error["tts"].endswith("no API key")
    sp2 = make_speech(tmp_path, key=True)
    sp2.cfg.speech.openai_base_url = "http://127.0.0.1:9/v1"   # nothing listens there
    assert sp2.synth("Hi.") is None and "network" in sp2.last_error["tts"]


def test_tts_daily_cap_skips_the_call(tmp_path, fake):
    sp = make_speech(tmp_path, fake, max_tts_chars_per_day=30)
    changes = []
    sp.on_change = lambda: changes.append(1)
    assert sp.synth("Twenty characters!!") is not None
    assert sp.synth("Another twenty chars") is None      # would exceed 30
    assert len(fake.requests) == 1 and "daily cap" in sp.last_error["tts"]
    assert changes                                        # face indicator refreshed
    assert sp.status()["today"]["tts_chars"] == 19
    sp.cfg.speech.max_tts_chars_per_day = 19              # full: indicator shows "local for now"
    assert sp.indicator()["tts"] is False or sp.tts == "local"


def test_monthly_cap(tmp_path, fake):
    sp = make_speech(tmp_path, fake, max_stt_seconds_per_month=5)
    sp.usage = {"2000-01-01": {"stt_seconds": 999}}       # other months don't count
    month = time.strftime("%Y-%m")
    sp.usage[f"{month}-01"] = {"stt_seconds": 4.5}
    assert sp.transcribe(b"\0" * 32000) is None           # 1 s more would make 5.5
    assert "monthly cap" in sp.last_error["stt"] and not fake.requests


# ------------------------------------------------------------ STT
def test_stt_sends_wav_and_prompt(tmp_path, fake, caplog):
    caplog.set_level(logging.DEBUG)
    sp = make_speech(tmp_path, fake)
    pcm = (np.ones(16000 * 2) * 100).astype("<i2").tobytes()      # 2 s
    text = sp.transcribe(pcm, "Walter is a friendly wall buddy. Names: Claude, GitHub.")
    assert text == fake.text
    path, headers, body = fake.requests[0]
    assert path == "/v1/audio/transcriptions"
    assert headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'name="model"\r\n\r\ngpt-4o-mini-transcribe' in body
    assert b"Names: Claude, GitHub." in body
    wav_start = body.index(b"RIFF")
    with wave.open(io.BytesIO(body[wav_start:])) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        assert w.getnframes() == 32000
    assert sp.status()["today"]["stt_seconds"] == 2.0
    assert "weather" not in caplog.text and FAKE_KEY not in caplog.text


def test_stt_error_and_timeout(tmp_path, fake):
    sp = make_speech(tmp_path, fake, stt_timeout_s=0.5)
    fake.mode = "error"
    assert sp.transcribe(b"\0" * 32000) is None
    fake.mode = "slow"
    assert sp.transcribe(b"\0" * 32000) is None
    assert sp.stats["stt_fallback"] == 2 and "stt_seconds" not in sp.status()["today"]


def test_cost_estimate(tmp_path):
    sp = make_speech(tmp_path, key=False)
    day = {"tts_chars": 1000, "tts_audio_s": 60, "stt_seconds": 120}
    assert sp.cost(day) == pytest.approx(0.015 + 2 * 0.003)
    sp.cfg.speech.tts_model, sp.cfg.speech.stt_model = "tts-1", "whisper-1"
    assert sp.cost(day) == pytest.approx(1000 * 15e-6 + 2 * 0.006)


# ------------------------------------------------------------ voice integration
class FakeSpeech:
    def __init__(self, audio=None, tts="openai", stt="openai", text="Hey Walter, cloud words"):
        self.tts, self.stt = tts, stt
        self.audio, self.text = audio, text
        self.synth_calls, self.stt_calls = [], []

    def synth(self, text, sr=22050):
        self.synth_calls.append((text, sr))
        return self.audio

    def transcribe(self, pcm, prompt=""):
        self.stt_calls.append((len(pcm), prompt))
        return self.text

    def has_key(self):
        return True


def make_voice(tmp_path):
    from walter.voice import Voice
    cfg = Config()
    cfg.voice.enabled = False
    cfg.voice.piper_model = str(tmp_path / "none.onnx")
    v = Voice(cfg, on_level=lambda x: None, on_cue=lambda k, val: None, on_speaking=lambda on: None)
    v.synth.kind = "mime"
    v.synth.synth_timed = lambda text: (np.zeros(5, np.int16), 16000, [["local", 0, 0]])
    return v


def test_voice_uses_cloud_with_estimated_word_timings(tmp_path):
    v = make_voice(tmp_path)
    tone = (np.sin(np.arange(22050) / 5) * 9000).astype(np.int16)
    v.speech = FakeSpeech(audio=tone)
    audio, sr, words, secs, cloud = v._timed_synth("Well, hello there friend.")
    assert cloud and sr == 22050 and len(audio) == 22050
    assert [w[0] for w in words] == ["Well,", "hello", "there", "friend."]
    assert all(0 <= w[1] <= w[2] <= 1.0 for w in words)
    assert words[0][1] < words[-1][1]                    # spread across the clip
    assert v.speech.synth_calls == [("Well, hello there friend.", 22050)]


def test_voice_falls_back_to_piper_and_respects_switch(tmp_path):
    v = make_voice(tmp_path)
    v.speech = FakeSpeech(audio=None)                     # cloud failed
    *_, cloud = v._timed_synth("Hello.")
    assert cloud is False and v.speech.synth_calls
    v.speech = FakeSpeech(audio=np.ones(10, np.int16), tts="local")
    audio, sr, words, _, cloud = v._timed_synth("Hello.")
    assert cloud is False and words == [["local", 0, 0]] and not v.speech.synth_calls
    # the demo forces one provider per sentence
    *_, cloud = v._timed_synth("Hello.", "openai")
    assert cloud is True


def test_voice_demo_queue_plays_cloud_audibly(tmp_path):
    """A cloud sentence is played with sound even if piper is missing (mime)."""
    v = make_voice(tmp_path)
    v.speech = FakeSpeech(audio=np.ones(2205, np.int16) * 3000, tts="local")
    played = []
    v.player.play = lambda audio, sr, lvl, silent, pause=0.12: played.append((len(audio), silent))

    async def go():
        v.start()
        v.say("Same sentence.", "local")
        v.say("Same sentence.", "openai")
        await asyncio.sleep(0.05)
        await asyncio.wait_for(v.wait_idle(), 3)
    asyncio.run(go())
    assert played == [(5, True), (2205, False)]


# ------------------------------------------------------------ ears integration
def test_ears_cloud_final_gated_and_fallback():
    from test_live import make_ears, wait_idle
    e, got = make_ears()
    e.speech = FakeSpeech()
    gate = []
    e.cloud_gate = lambda text: (gate.append(text), True)[1]
    pcm = b"\0" * 2 * 16000 * 2
    assert e._process(pcm, 60, 30.0)
    assert got["text"] == ["Hey Walter, cloud words"]
    assert gate == ["Hey Walter, what's the time?"]      # judged on the local text
    assert e.speech.stt_calls[0][0] == len(pcm) and "Walter" in e.speech.stt_calls[0][1]
    # room chatter: never sent
    e.cloud_gate = lambda text: False
    assert e._process(pcm, 60, 30.0)
    assert got["text"][-1] == "Hey Walter, what's the time?" and len(e.speech.stt_calls) == 1
    # cloud failure or empty result: the local transcript stands
    e.cloud_gate = lambda text: True
    e.speech.text = None
    assert e._process(pcm, 60, 30.0) and got["text"][-1] == "Hey Walter, what's the time?"
    e.speech.text = "thank you"                           # a hallucination
    assert e._process(pcm, 60, 30.0) and got["text"][-1] == "Hey Walter, what's the time?"
    # switched to local: no cloud at all
    e.speech = FakeSpeech(stt="local")
    assert e._process(pcm, 60, 30.0) and not e.speech.stt_calls
    wait_idle(e)
    e.stop()


def test_ears_cloud_gets_whole_long_turn():
    from test_live import make_ears
    from walter.ears import _Job
    e, got = make_ears()
    e.speech = FakeSpeech()
    piece = _Job(e._uid, b"\1" * 64000, 60, final=True, chunk=True)
    piece.text = "Hey Walter, first part"
    piece.done.set()
    assert e._transcribe(b"\0" * 16000, chunks=[piece])
    assert e.speech.stt_calls[0][0] == 64000 + 16000
    e.stop()


def test_conductor_cloud_gate():
    from test_conductor import make
    c, _ = make()
    assert not c.cloud_gate("pass me the salt please")
    assert c.cloud_gate("Walter, what's the time?")
    c.followup_until = time.time() + 10
    assert c.cloud_gate("and tomorrow?")
    c.enrolling = "voice"
    assert not c.cloud_gate("Walter, the quick brown fox")
    c.enrolling = None
    c.state = "speaking"                                   # probably his own echo
    assert not c.cloud_gate("and tomorrow?")
    assert c.cloud_gate("Walter, stop")


# ------------------------------------------------------------ walterctl
def test_walterctl_offline_saves_choice(tmp_path):
    env = dict(os.environ, WALTER_DATA=str(tmp_path), WALTER_URL="http://127.0.0.1:9")
    r = subprocess.run([sys.executable, str(REPO / "bin" / "walterctl"), "voice", "openai"],
                       capture_output=True, text=True, env=env, timeout=20)
    assert r.returncode == 0, r.stderr
    assert json.loads((tmp_path / "speech.json").read_text()) == {"tts": "openai"}
    r = subprocess.run([sys.executable, str(REPO / "bin" / "walterctl"), "ears", "bogus"],
                       capture_output=True, text=True, env=env, timeout=20)
    assert r.returncode != 0 and "usage" in r.stderr


def test_server_speech_api(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from walter.server import Hub, build_app
    cfg = Config()
    sp = Speech(cfg, data_dir=tmp_path)
    said = []
    hub = Hub()
    sent = []
    hub.send = sent.append
    conductor = SimpleNamespace(speech=sp, wake_up=lambda: None,
                                voice=SimpleNamespace(say=lambda t, p=None: said.append(p)))
    app = build_app(cfg, hub, conductor, None, None, None, None, None)

    async def go():
        async with TestClient(TestServer(app)) as cl:
            r = await cl.get("/api/speech")
            d = await r.json()
            assert d["tts"] == "local" and d["key"] is False and d["available"]
            r = await cl.post("/api/speech", json={"which": "ears", "provider": "openai"})
            assert (await r.json())["stt"] == "openai"
            r = await cl.post("/api/speech", json={"which": "ears", "provider": "moon"})
            assert r.status == 400
            r = await cl.post("/api/speech/demo", json={})
            assert (await r.json())["openai"] is False and said == ["local"]
    asyncio.run(go())
    assert any(m.get("t") == "cloud" for m in sent)
    assert json.loads((tmp_path / "speech.json").read_text())["stt"] == "openai"


def test_prompt_echo_is_detected():
    from walter.speech import echoes_prompt
    prompt = "### Walter is a friendly wall buddy. Hey Walter. Names: Alex, Maria, Sam, Springfield. ###"
    assert echoes_prompt("context: ### Walter is a friendly wall buddy. Hey Walter. Names: Alex, Maria ###", prompt)
    assert echoes_prompt("Walter is a friendly wall buddy. Hey Walter.", prompt)
    assert not echoes_prompt("Hey Walter, what's the weather in Springfield tomorrow?", prompt)
    assert not echoes_prompt("", prompt)
