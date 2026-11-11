"""Podcast tool: feed parsing, show/episode choice, playback via a fake mpv IPC
socket, and ducking on Walter's state changes. No network, no real player."""
import json
import socket
import threading
from pathlib import Path

import pytest

from walter import podcast

FEED = (Path(__file__).parent / "fixtures" / "podcast" / "feed.xml").read_bytes()
SHOW = {"title": "Sample Comedy Hour", "feed": "https://example.invalid/feed", "author": "A Comic"}


class FakeMpv:
    """Minimal mpv JSON IPC server on a unix socket."""
    def __init__(self, path):
        self.path = Path(path)
        self.props = {"pause": False, "volume": 100.0, "speed": 1.0, "time-pos": 0.0,
                      "duration": 3600.0, "pid": 1234}
        self.log = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(str(self.path))
        self.srv.listen(8)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            with c:
                data = b""
                while b"\n" not in data:
                    chunk = c.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                if not data:
                    continue
                msg = json.loads(data.split(b"\n")[0])
                cmd = msg["command"]
                self.log.append(cmd)
                out = {"request_id": msg["request_id"], "error": "success", "data": None}
                if cmd[0] == "get_property":
                    if cmd[1] in self.props:
                        out["data"] = self.props[cmd[1]]
                    else:
                        out["error"] = "property unavailable"
                elif cmd[0] == "set_property":
                    self.props[cmd[1]] = cmd[2]
                elif cmd[0] == "seek":
                    self.props["time-pos"] += cmd[1]
                elif cmd[0] == "quit":
                    self.close()
                c.sendall(json.dumps({"event": "noise"}).encode() + b"\n"
                          + json.dumps(out).encode() + b"\n")

    def close(self):
        self.srv.close()
        self.path.unlink(missing_ok=True)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(podcast, "STATE", tmp_path / "podcasts.json")
    monkeypatch.setattr(podcast, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(podcast, "SOCK", tmp_path / "mpv.sock")
    monkeypatch.setattr(podcast, "NOW_FILE", tmp_path / "dashboard-podcast.json")
    monkeypatch.setattr(podcast, "UPNEXT_FILE", tmp_path / "dashboard-podcast-upnext.json")
    return tmp_path


def test_parse_feed_newest_first_skips_items_without_audio():
    eps = podcast.parse_feed(FEED)
    assert [e["guid"] for e in eps] == ["ep-43", "ep-42", "ep-41"]
    assert eps[2]["dur"] == 3723 and eps[1]["dur"] == 2700


def test_search_and_pick_result():
    def fetch(url):
        assert "media=podcast" in url
        return json.dumps({"results": [
            {"collectionName": "Something Else", "feedUrl": "f1", "artistName": "x"},
            {"collectionName": "Sample Comedy Hour", "feedUrl": "f2", "artistName": "y"},
            {"collectionName": "No Feed"}]}).encode()
    res = podcast.search("sample comedy hour", fetch=fetch)
    assert len(res) == 2
    assert podcast.pick_result("Sample Comedy Hour", res)["feed"] == "f2"


def test_episodes_cached(env):
    calls = []
    def fetch(url):
        calls.append(url)
        return FEED
    podcast.episodes(SHOW, fetch=fetch)
    podcast.episodes(SHOW, fetch=fetch)
    assert len(calls) == 1


def test_find_show_and_split():
    st = podcast.load(Path("/nonexistent"))
    st["shows"] = [SHOW, {"title": "Other Talk Show", "feed": "x"}]
    assert podcast.find_show(st, "sample comedy")["feed"] == SHOW["feed"]
    show, what = podcast._split_show(st, ["sample", "comedy", "latest"])
    assert show is SHOW and what == "latest"
    st["shows"] = [SHOW]
    show, what = podcast._split_show(st, ["gravy", "trains"])
    assert show is SHOW and what == "gravy trains"


def test_choose_episode():
    eps = podcast.parse_feed(FEED)
    st = podcast.load(Path("/nonexistent"))
    assert podcast.choose_episode(eps, "latest", st)["guid"] == "ep-43"
    assert podcast.choose_episode(eps, "2", st)["guid"] == "ep-42"
    assert podcast.choose_episode(eps, "gravy", st)["guid"] == "ep-42"
    st["episodes"]["ep-43"] = {"played": True}
    assert podcast.choose_episode(eps, None, st)["guid"] == "ep-42"
    st["episodes"]["ep-41"] = {"pos": 600}
    assert podcast.choose_episode(eps, None, st)["guid"] == "ep-41"  # half-heard first


def test_play_resumes_and_stop_saves_position(env):
    st = podcast.load()
    ep = podcast.parse_feed(FEED)[0]
    st["episodes"][ep["guid"]] = {"pos": 125.0}
    argv = []
    mpv = {}
    def spawn(a):
        argv.extend(a)
        mpv["p"] = FakeMpv(podcast.SOCK)
    msg = podcast.play(ep, SHOW, st, spawn=spawn)
    assert "from 2:05" in msg
    assert "--start=122" in argv and argv[-1] == ep["url"]
    assert any(a.startswith("--input-ipc-server=") for a in argv)
    mpv["p"].props["time-pos"] = 3550.0
    assert "Playing" in podcast.status(podcast.load())
    out = podcast.stop(podcast.load())
    assert out.startswith("Stopped")
    rec = podcast.load()["episodes"][ep["guid"]]
    assert rec["pos"] == 3550.0 and rec["played"] is True
    assert podcast.load()["now"] is None


def test_ducker_ducks_pauses_and_restores(env):
    fake = FakeMpv(podcast.SOCK)
    d = podcast.Ducker()
    d.on_state("listening")
    assert fake.props["volume"] == 25 and fake.props["pause"] is False
    d.on_state("thinking")
    d.on_state("speaking")
    assert fake.props["pause"] is True
    d.on_state("listening")          # follow-up window: stay paused
    assert fake.props["pause"] is True
    d.on_state("idle")
    assert fake.props["pause"] is False and fake.props["volume"] == 100
    fake.close()


def test_ducker_keeps_a_user_pause(env):
    fake = FakeMpv(podcast.SOCK)
    d = podcast.Ducker()
    d.on_state("thinking")
    fake.props["pause"] = True       # "pause the podcast"
    d.on_state("speaking")
    d.on_state("idle")
    assert fake.props["pause"] is True
    fake.close()


def test_ducker_without_player_is_a_noop(env):
    d = podcast.Ducker()
    for s in ("listening", "speaking", "idle"):
        d.on_state(s)
    assert d.mode == "full"


def test_cli_skip_speed(env, capsys):
    fake = FakeMpv(podcast.SOCK)
    assert podcast.cli(["skip", "30"]) == 0
    assert fake.props["time-pos"] == 30
    assert podcast.cli(["back"]) == 0
    assert fake.props["time-pos"] == 15
    assert podcast.cli(["speed", "1.25"]) == 0
    assert fake.props["speed"] == 1.25 and podcast.load()["speed"] == 1.25
    fake.close()


def test_now_playing_publish_and_resume_chip(env):
    st = podcast.load()
    st["now"] = {"guid": "g1", "show": "Morning News", "title": "Ep 1", "url": "u", "started": 0}
    st["episodes"]["g1"] = {"show": "Morning News", "title": "Ep 1"}
    fake = FakeMpv(podcast.SOCK)
    fake.props.update({"time-pos": 61.0, "volume": 25.0, "speed": 1.25, "pause": True})
    podcast.publish(st)
    d = json.loads(podcast.NOW_FILE.read_text())
    assert d["playing"] and d["title"] == "Ep 1" and d["show"] == "Morning News"
    assert d["pos"] == 61.0 and d["dur"] == 3600.0 and d["paused"] and d["ducked"] and d["speed"] == 1.25
    fake.props["volume"] = 100.0
    assert podcast.now_playing(st)["ducked"] is False
    podcast.save(st)
    podcast.stop(podcast.load())
    d = json.loads(podcast.NOW_FILE.read_text())
    assert d["playing"] is False and d["resume"]["title"] == "Ep 1" and d["resume"]["pos"] == 61.0


def test_resume_candidate_skips_played_and_short(env):
    st = podcast.load()
    st["episodes"] = {"a": {"title": "A", "pos": 5, "at": 9}, "b": {"title": "B", "pos": 100, "played": True, "at": 9},
                      "c": {"title": "C", "pos": 100, "at": 1}, "d": {"title": "D", "pos": 200, "at": 5}}
    assert podcast.resume_candidate(st)["title"] == "D"
    assert podcast.now_playing({"episodes": {}, "now": None})["playing"] is False


# ------------------------------------------------------------------ up next
NOW_TS = 1793865600.0   # Thu 5 Nov 2026 08:00 UTC: ep-43 (4 Nov) is new, ep-42 (31 Oct) too, ep-41 not


def _upnext_state():
    return {"shows": [SHOW], "episodes": {}, "now": None}


def test_upnext_newest_unplayed_first_with_new_dots(env):
    st = _upnext_state()
    st["episodes"]["ep-43"] = {"played": True}
    items = podcast.upnext(st, fetch=lambda u: FEED, now=NOW_TS)
    assert [i["guid"] for i in items] == ["ep-42", "ep-41"]
    assert items[0]["new"] and not items[1]["new"]
    assert items[0]["show"] == "Sample Comedy Hour" and items[0]["dur"] == 2700


def test_upnext_resume_first_and_skips_now_playing(env):
    st = _upnext_state()
    st["episodes"]["ep-41"] = {"pos": 600, "at": 5}
    items = podcast.upnext(st, fetch=lambda u: FEED, now=NOW_TS)
    assert items[0]["guid"] == "ep-41" and items[0]["resume"] and items[0]["pos"] == 600
    assert [i["guid"] for i in items[1:]] == ["ep-43", "ep-42"]
    st["now"] = {"guid": "ep-43", "show": SHOW["title"], "title": "Episode 43: Newest Thing"}
    assert "ep-43" not in [i["guid"] for i in podcast.upnext(st, fetch=lambda u: FEED, now=NOW_TS)]


def test_upnext_cache_only_never_fetches(env):
    def boom(u):
        raise AssertionError("network")
    assert podcast.upnext(_upnext_state(), fetch=boom, cache_only=True) == []
    podcast.episodes(SHOW, fetch=lambda u: FEED)            # fills the cache
    items = podcast.upnext(_upnext_state(), fetch=boom, cache_only=True, now=NOW_TS + 86400 * 30)
    assert len(items) == 3 and not any(i["new"] for i in items)


def test_publish_upnext_writes_sibling_file(env):
    out = env / "upnext.json"
    podcast.publish_upnext(_upnext_state(), fetch=lambda u: FEED, path=out)
    d = json.loads(out.read_text())
    assert [i["guid"] for i in d["items"]] == ["ep-43", "ep-42", "ep-41"] and d["updated"]
    assert "Episode 43" in podcast.describe_upnext(d["items"])
    assert podcast.describe_upnext([]).startswith("Nothing new")


# ------------------------------------------------------------------ cover art (Up next card)
ART_FEED = (Path(__file__).parent / "fixtures" / "podcast" / "feed-art.xml").read_bytes()
ART_SHOW = {"title": "Sample Art Show", "feed": "https://example.invalid/art-feed"}


def _fake_convert(src, dst):
    dst.write_bytes(b"JPEG:" + src.read_bytes())
    return True


def test_parse_feed_episode_art_falls_back_to_show_art():
    eps = podcast.parse_feed(ART_FEED)
    assert [e["img"] for e in eps] == ["https://example.invalid/ep2.png", "https://example.invalid/show.jpg"]
    assert podcast.parse_feed(FEED)[0]["img"] == ""


def test_cache_art_fetches_once_and_serves_a_local_copy(tmp_path):
    calls = []
    def fetch(u):
        calls.append(u)
        return b"img"
    kw = dict(fetch=fetch, art_dir=tmp_path / "art", face_dir=tmp_path / "face" / "podcast-art", convert=_fake_convert)
    rel = podcast.cache_art("https://example.invalid/x.png", **kw)
    assert rel.startswith("podcast-art/") and rel.endswith(".jpg")
    assert (tmp_path / "face" / rel).read_bytes() == b"JPEG:img"
    assert podcast.cache_art("https://example.invalid/x.png", **kw) == rel and len(calls) == 1
    assert podcast.cache_art("", **kw) == "" and podcast.cache_art("file:///etc/passwd", **kw) == ""


def test_cache_art_failure_is_not_retried_for_a_day(tmp_path):
    calls = []
    def fetch(u):
        calls.append(u)
        raise OSError("offline")
    kw = dict(fetch=fetch, art_dir=tmp_path / "art", face_dir=tmp_path / "pa", convert=_fake_convert)
    assert podcast.cache_art("https://example.invalid/y.png", now=1000, **kw) == ""
    assert podcast.cache_art("https://example.invalid/y.png", **kw) == "" and len(calls) == 1
    assert not list((tmp_path / "art").glob("*.jpg"))


def test_publish_upnext_items_carry_source_art_and_now(env, monkeypatch):
    monkeypatch.setattr(podcast, "_ffmpeg_square", _fake_convert)
    st = {"shows": [ART_SHOW], "episodes": {},
          "now": {"guid": "a-2", "show": "Sample Art Show", "title": "Ep 2: Own Art"}}
    out = env / "face" / "dashboard-podcast-upnext.json"
    out.parent.mkdir()
    podcast.publish_upnext(st, fetch=lambda u: ART_FEED if "feed" in u else b"img", path=out)
    d = json.loads(out.read_text())
    assert [i["guid"] for i in d["items"]] == ["a-1"]
    it = d["items"][0]
    assert it["source"] == "podcast" and "img" not in it and (out.parent / it["art"]).exists()
    assert d["now"]["guid"] == "a-2" and d["now"]["art"] and d["now"]["art"] != it["art"]


@pytest.mark.skipif(not podcast.shutil.which("ffmpeg"), reason="no ffmpeg")
def test_ffmpeg_square_makes_a_small_jpeg(tmp_path):
    src = tmp_path / "in.ppm"
    src.write_bytes(b"P6 4 2 255\n" + bytes([200, 30, 30]) * 8)
    dst = tmp_path / "out.jpg"
    assert podcast._ffmpeg_square(src, dst, px=16)
    assert dst.read_bytes()[:2] == b"\xff\xd8"


def test_cli_seekto_and_play_guid(env, monkeypatch, capsys):
    fake = FakeMpv(podcast.SOCK)
    assert podcast.cli(["seekto", "125"]) == 0
    assert fake.props["time-pos"] == 125
    fake.close()
    st = podcast.load()
    st["shows"] = [SHOW]
    podcast.save(st)
    played = []
    monkeypatch.setattr(podcast, "episodes", lambda show, **kw: podcast.parse_feed(FEED))
    monkeypatch.setattr(podcast, "find_player", lambda: "mpv")
    monkeypatch.setattr(podcast, "play", lambda ep, show, st: played.append(ep["guid"]) or "Playing")
    assert podcast.cli(["play-guid", "ep-42"]) == 0 and played == ["ep-42"]
    assert podcast.cli(["play-guid", "nope"]) == 1 and played == ["ep-42"]
