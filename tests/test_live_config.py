"""Live config reload (walter/live.py): settings apply without a restart."""
import asyncio
import json
import re
from types import SimpleNamespace


from walter import live
from walter.config import Config, load
from walter.speech import OPENAI_PCM_SR

from test_speech import fake, make_speech  # noqa: F401  (fixture)

CFG = """# my settings
[voice]
pitch = 0.95   # the owner liked this

[speech]
tts_model = "tts-1-hd"
tts_voice = "echo"
tts_speed = 1.12
tts_pitch = 0.93
"""


def write(p, text):
    p.write_text(text)
    return p


def test_reload_changes_the_next_cloud_sentence(tmp_path, fake):  # noqa: F811
    path = write(tmp_path / "config.toml", CFG)
    sp = make_speech(tmp_path, fake)
    cfg = sp.cfg
    for k, v in load(path).speech.__dict__.items():
        if k.startswith("tts_") and k != "tts_timeout_s":
            setattr(cfg.speech, k, v)
    lv = live.Live(cfg, path)
    assert sp.synth("Hello.", 24000) is not None
    first = json.loads(fake.requests[-1][2])
    assert first["voice"] == "echo" and abs(first["speed"] - 1.12 / 0.93) < 1e-6

    write(path, CFG.replace('"echo"', '"onyx"').replace("1.12", "1.3").replace("0.93", "1.0"))
    r = lv.reload()
    assert r["ok"] and set(r["applied"]) == {"speech.tts_voice", "speech.tts_speed", "speech.tts_pitch"}
    assert not r["needs_restart"]
    assert sp.synth("Hello.", 24000) is not None
    nxt = json.loads(fake.requests[-1][2])
    assert nxt["voice"] == "onyx" and abs(nxt["speed"] - 1.3) < 1e-6


def test_pitch_change_resamples_the_audio(tmp_path, fake):  # noqa: F811
    path = write(tmp_path / "config.toml", CFG)
    sp = make_speech(tmp_path, fake, tts_model="tts-1-hd", tts_pitch=1.0)
    lv = live.Live(sp.cfg, path)
    lv.base.speech.tts_pitch = 1.0           # pretend Walter started at pitch 1.0
    a = sp.synth("Hello.", OPENAI_PCM_SR)
    r = lv.reload()                          # file says 0.93
    assert "speech.tts_pitch" in r["applied"] and sp.cfg.speech.tts_pitch == 0.93
    b = sp.synth("Hello.", OPENAI_PCM_SR)
    assert len(b) > len(a)                   # deeper = played back slower = more samples


def test_restart_keys_switches_and_bad_files(tmp_path):
    path = write(tmp_path / "config.toml", CFG)
    cfg = load(path)
    cfg.voice.device = "walter_aec_speaker"   # aec.py changed it at start: not a file change
    lv = live.Live(cfg, path)
    assert lv.reload()["applied"] == []
    write(path, CFG + '\n[ears]\ndevice = "usb"\nend_silence_ms = 900\n[brain]\nmodel = "x"\n'
          '[life]\nquiet_hours = [22, 7]\n')
    path.write_text(path.read_text().replace('[speech]', '[speech]\ntts_provider = "openai"'))
    r = lv.reload()
    assert set(r["applied"]) == {"ears.end_silence_ms", "life.quiet_hours"}
    assert cfg.ears.end_silence_ms == 900 and cfg.life.quiet_hours == [22, 7]
    assert cfg.ears.device == "" and cfg.brain.model == ""          # not applied
    assert {x.split(" ")[0] for x in r["needs_restart"]} == {"ears.device", "brain.model"}
    assert r["switches"] and "walterctl voice" in r["switches"][0]
    assert set(r["pending_restart"]) == {"ears.device", "brain.model"}
    assert cfg.voice.device == "walter_aec_speaker"
    write(path, "[voice\nbroken")
    bad = lv.reload()
    assert bad["ok"] is False and cfg.ears.end_silence_ms == 900


def test_hooks_reach_the_components(tmp_path):
    path = write(tmp_path / "config.toml", "")
    cfg = Config()
    calls = []
    ears = SimpleNamespace(reconfigure=lambda k: calls.append(("ears", set(k))) or "")
    pres = SimpleNamespace(tracker=SimpleNamespace(), gate=SimpleNamespace(force_sleep_after_s=0),
                           set_enabled=lambda on: calls.append(("camera", on)),
                           set_debug=lambda on: None)
    c = SimpleNamespace(ears=ears, presence=pres, wake_re=re.compile("walter"), voice=None)
    sent = []
    lv = live.Live(cfg, path, c, sent.append)
    write(path, '[ears]\nwake_words = ["buddy"]\nwhisper_model = "tiny.en"\n'
          '[senses]\ncamera = true\nempty_after_min = 5\n[life]\ncaptions = true\n')
    r = lv.reload()
    assert r["ok"]
    assert c.wake_re.search("hey buddy") and not c.wake_re.search("walter")
    assert ("ears", {"wake_words", "whisper_model"}) in calls and ("camera", True) in calls
    assert pres.tracker.empty_after_s == 300
    assert sent and sent[-1]["captions"] is True


def test_set_in_file_keeps_comments_and_validates(tmp_path):
    path = write(tmp_path / "config.toml", CFG)
    live.set_in_file(path, "voice.pitch", "0.9")
    live.set_in_file(path, "speech.tts_voice", "ash")
    live.set_in_file(path, "life.quiet_hours", "[22, 7]")
    live.set_in_file(path, "ears.beam_size", "2")
    t = path.read_text()
    assert "pitch = 0.9   # the owner liked this" in t and "# my settings" in t
    assert 'tts_voice = "ash"' in t and "[ears]\nbeam_size = 2" in t
    c = load(path)
    assert c.voice.pitch == 0.9 and c.life.quiet_hours == [22, 7] and c.ears.beam_size == 2
    for bad in (("voice.pitch", "loud"), ("voice.nope", "1"), ("moon.x", "1"), ("pitch", "1")):
        try:
            live.set_in_file(path, *bad)
        except ValueError:
            pass
        else:
            raise AssertionError(bad)
    assert load(path).voice.pitch == 0.9


def test_describe_lists_hot_and_restart():
    d = live.describe()
    assert "tts_pitch" in d["hot"]["speech"] and "device" in d["restart"]["ears"]
    assert "brain" in d["restart"] and d["hot"]["brain"] == ["max_tasks"]
    assert "max_tasks" not in d["restart"]["brain"] and "model" in d["restart"]["brain"]


def test_api_config_routes(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from walter.server import Hub, build_app
    path = write(tmp_path / "config.toml", CFG)
    cfg = load(path)
    conductor = SimpleNamespace(wake_up=lambda: None)
    conductor.live = live.Live(cfg, path, conductor)
    app = build_app(cfg, Hub(), conductor, None, None, None, None, None)

    async def go():
        async with TestClient(TestServer(app)) as cl:
            d = await (await cl.get("/api/config")).json()
            assert d["config"]["speech"]["tts_voice"] == "echo" and "voice" in d["hot"]
            r = await (await cl.post("/api/config/set", json={"key": "voice.volume", "value": "0.8"})).json()
            assert r["applied"] == ["voice.volume"] and cfg.voice.volume == 0.8
            r = await cl.post("/api/config/set", json={"key": "voice.volume", "value": "loud"})
            assert r.status == 400
            r = await (await cl.post("/api/config/reload")).json()
            assert r["ok"] and r["applied"] == []
    asyncio.run(go())


def test_ears_reconfigure_picks_up_timings():
    from walter.ears import Ears
    e = Ears(Config(), on_text=lambda t: None, on_hear=lambda v: None, on_start=lambda: None)
    assert e.reconfigure({"end_silence_ms"}) == "" and e.cfg_gen == 1
    e.model = object()
    import threading
    started = []
    orig = threading.Thread.start
    try:
        threading.Thread.start = lambda self: started.append(self.name)
        assert "background" in e.reconfigure({"whisper_model"})
    finally:
        threading.Thread.start = orig
    assert started == ["ears-reload"]
