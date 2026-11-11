"""Face agent (walter/face_agent.py): pairing from the CLI, token file, hello, heard turns with a
mic score, SSE parsing and event handling (audio, media, stop) with fakes. No mic, no speaker,
no mpv: everything that would make a sound is faked. Never touches ~/.walter."""
import asyncio
import json
import os
import re
import threading
from unittest.mock import MagicMock

import pytest

from walter import face_agent as A
from walter import faces as F
from walter.config import Config


def test_parse_sse():
    lines = [b": hello\n", b"\n", b"id: 5\n", b'data: {"t": "say", "text": "hi"}\n', b"\n",
             b": ping\n", b"id: 6\n", b"data: not json\n", b"\n", "id: 7\n", 'data: {"t": "stop"}\n', "\n"]
    assert list(A.parse_sse(lines)) == [("5", {"t": "say", "text": "hi"}), ("7", {"t": "stop"})]


def test_clean_brain():
    assert A.clean_brain("http://192.168.1.20:8765/") == "http://192.168.1.20:8765"
    for bad in ("192.168.1.20", "file:///etc", "http://x/y?z", "javascript:alert(1)"):
        with pytest.raises(ValueError):
            A.clean_brain(bad)


def test_state_file_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("WALTER_FACE_HOME", str(tmp_path / "fh"))
    A.save_state({"brain": "http://x:1", "token": "secret"})
    assert oct(os.stat(A.state_path()).st_mode & 0o777) == "0o600"
    assert oct(os.stat(tmp_path / "fh").st_mode & 0o777) == "0o700"
    assert A.load_state()["token"] == "secret"


class FakeSpeaker:
    def __init__(self):
        self.player = ["true"]
        self.gen = 0
        self.played, self.media, self.said, self.stopped = [], [], [], 0

    def play_wav(self, wav, gen=None):
        self.played.append(wav)

    def say_text(self, text):
        self.said.append(text)

    def play_media(self, url, start=0.0):
        self.media.append((url, start))
        return True

    def stop(self):
        self.stopped += 1


def test_handle_events_with_fakes():
    http = MagicMock()
    http.raw.return_value = b"RIFFwav"
    spk = FakeSpeaker()
    ag = A.Agent("http://x:1", "t", ears=False, http=http, spk=spk)
    ag.handle({"t": "audio", "url": "/api/face/audio/abc"})
    assert spk.played == [b"RIFFwav"] and http.raw.call_args.args == ("/api/face/audio/abc",)
    ag.handle({"t": "noaudio", "text": "hello"})
    assert spk.said == ["hello"]
    ag.handle({"t": "media", "url": "https://x.example/a.mp3", "start": 61})
    assert spk.media == [("https://x.example/a.mp3", 61.0)]
    ag.handle({"t": "stop"})
    assert spk.stopped == 1
    ag.handle({"t": "forgotten"})
    assert ag.running is False
    quiet = A.Agent("http://x:1", "t", ears=False, speaker=False, http=http, spk=FakeSpeaker())
    quiet.handle({"t": "audio", "url": "/x"})
    assert quiet.spk.played == []


def test_speaker_media_refuses_non_http_and_stale_audio(tmp_path):
    spk = A.Speaker(player=["true"], run=MagicMock())
    spawned = []
    assert spk.play_media("file:///etc/passwd", spawn=lambda *a, **k: spawned.append(a)) is False
    assert spk.play_media("https://x.example/a.mp3 --script=evil", spawn=lambda *a, **k: spawned.append(a)) is False
    assert spawned == []
    g = spk.gen
    spk.stop()                       # "stop" makes queued sentences stale
    assert spk.play_wav(b"x", g) is False
    busy = []
    spk2 = A.Speaker(on_busy=busy.append, player=["true"], run=MagicMock())
    assert spk2.play_wav(b"RIFF", spk2.gen) is True and busy == [True, False]


def test_media_args_end_options_before_the_url(monkeypatch):
    monkeypatch.setattr(A.shutil, "which", lambda n: "/usr/bin/mpv" if n == "mpv" else None)
    spk = A.Speaker(player=["true"], run=MagicMock())
    got = []
    assert spk.play_media("https://x.example/a.mp3", 90, spawn=lambda argv, **k: got.append(argv) or MagicMock())
    assert got[0][-2:] == ["--", "https://x.example/a.mp3"] and "--start=90" in got[0]


def test_caps_and_mic_quality():
    ag = A.Agent("http://x:1", "t", ears=False, http=MagicMock(), spk=FakeSpeaker())
    assert ag.caps() == {"ears": False, "speaker": True, "version": A.VERSION}
    ag.scores = [0.5, 0.7]
    assert ag.caps()["mic_quality"] == 0.6


# ---------------------------------------------------------------- against a real brain server
@pytest.fixture
def iso(tmp_path, monkeypatch):
    monkeypatch.setenv("WALTER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("WALTER_FACE_HOME", str(tmp_path / "fh"))
    import walter.dashboard as dash
    (tmp_path / "face").mkdir()
    monkeypatch.setattr(dash, "FACE_DIR", tmp_path / "face")
    return tmp_path


def test_pair_hello_and_heard_end_to_end(iso):
    from aiohttp.test_utils import TestServer
    from walter.face_server import FaceHub
    from walter.server import Hub, build_app

    async def go():
        cfg = Config()
        cfg.brain.home = str(iso)
        cfg.faces.window_s = 0.05
        cfg.faces.mdns = False
        cond = MagicMock(state="idle", user_muted=False, speech=None, voice=None, ears=None,
                         wake_re=re.compile(r"\b(walter)\b", re.I))
        hub = Hub()
        fh = FaceHub(cfg, cond, hub, F.FaceStore(iso / "data" / "faces.json"))
        app = build_app(cfg, hub, cond, MagicMock(items=[]), MagicMock(), MagicMock(items=[]), MagicMock(),
                        MagicMock(), None, fh)
        server = TestServer(app)
        await server.start_server()
        url = str(server.make_url("")).rstrip("/")
        loop = asyncio.get_running_loop()
        try:
            code = fh.store.start_pairing()["code"]
            rc = await loop.run_in_executor(None, A.main, ["pair", url, code, "--name", "Laptop", "--room", "office"])
            assert rc == 0
            st = A.load_state()
            assert st["face"]["id"] == "laptop" and st["brain"] == url and fh.store.check(st["token"])
            ag = A.Agent(url, st["token"], ears=False, spk=FakeSpeaker())
            r = await loop.run_in_executor(None, ag.hello)
            assert r["face"]["room"] == "office" and fh.has_speaker("laptop")
            ag.levels = [0.5, 0.6, 0.55]
            await loop.run_in_executor(None, ag._post_heard, "Walter, what's on today?", 0.8)
            await asyncio.sleep(0.2)
            src, text, meta = cond.submit.call_args.args
            assert src == "face" and meta["face"] == "Laptop" and meta["audio"] is True and meta["room"] == "office"
            # the agent's event stream gets the reply audio
            fh.emit("laptop", {"t": "say", "text": "hi"})
            got = []

            def read():
                with ag.http.stream(f"/api/face/events?after=1") as resp:
                    for _eid, msg in A.parse_sse(resp):
                        got.append(msg)
                        if msg["t"] == "say":
                            return
            await loop.run_in_executor(None, read)
            assert [m["t"] for m in got] == ["heard", "say"]   # what it heard, then the reply
            # a wrong token is refused
            bad = A.Agent(url, "nope", ears=False, spk=FakeSpeaker())
            with pytest.raises(RuntimeError):
                await loop.run_in_executor(None, bad.hello)
        finally:
            await server.close()
    asyncio.run(go())
