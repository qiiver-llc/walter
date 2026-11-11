"""walter/media.py: YouTube in the wall browser (pure helpers + ducking with a fake browser)."""
import json

from walter import media


def test_video_id_from_links():
    assert media.video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10") == "dQw4w9WgXcQ"
    assert media.video_id("youtu.be/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert media.video_id("https://youtube.com/shorts/dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert media.video_id("https://example.com/watch?v=dQw4w9WgXcQ") == ""
    assert media.video_id("packers highlights") == ""


def test_search_url_filters_to_videos_unless_live():
    assert media.search_url("packers highlights").endswith("search_query=packers+highlights&sp=EgIQAQ%253D%253D")
    assert "sp=" not in media.search_url("packers game", live=True)


def r(vid, title, channel="Someone", **kw):
    return {"href": f"/watch?v={vid}", "title": title, "channel": channel, **kw}


def test_pick_skips_ads_shorts_live_and_prefers_official():
    res = [r("aaaaaaaaaaa", "Song X", ad=True),
           {"href": "/shorts/bbbbbbbbbbb", "title": "Song X", "channel": "c"},
           r("ccccccccccc", "Song X live now", live=True),
           r("ddddddddddd", "Song X reaction"),
           r("eeeeeeeeeee", "Song X (Official Video)", "ArtistVEVO", verified=True),
           r("fffffffffff", "Song X lyrics")]
    assert media.pick(res, "song x")["href"].endswith("eeeeeeeeeee")
    assert media.pick(res[:4], "song x")["href"].endswith("ddddddddddd")   # last resort
    assert media.pick(res[:3], "song x") is None
    assert media.pick(res[:4], "song x", allow_live=True)["href"].endswith("ccccccccccc")
    assert media.pick(res[:4], "song x reaction", allow_live=False)["href"].endswith("ddddddddddd")


def test_times():
    assert media.parse_time("1:30") == 90 and media.parse_time("1:02:03") == 3723
    assert media.fmt_time(95) == "1:35" and media.fmt_time(3723) == "1:02:03"
    s = media.describe({"found": True, "title": "T", "channel": "C", "time": 5, "duration": 65,
                        "paused": False, "volume": 40, "rate": 1})
    assert s == "T (C): playing, 0:05 of 1:05, volume 40%"
    assert media.describe({}) == "Nothing playing in the wall browser."


class FakeBrowser:
    def __init__(self, paused=False):
        self.paused, self.calls = paused, []

    def __call__(self, args):
        self.calls.append((args["action"], args.get("value")))
        if args["action"] == "pause":
            self.paused = True
        if args["action"] == "play":
            self.paused = False
        return {"ok": True, "info": {"found": True, "paused": self.paused}}


def ducker(tmp_path, fake, active=True):
    sock = tmp_path / "browser-live.sock"
    sock.touch()
    st = tmp_path / "media.json"
    st.write_text(json.dumps({"active": active, "volume": 80, "duck_volume": 25}))
    return media.Ducker(sock=sock, state_path=st, sender=fake, threaded=False)


def test_ducks_pauses_and_resumes(tmp_path):
    fake = FakeBrowser()
    d = ducker(tmp_path, fake)
    d.on_state("listening")
    assert fake.calls == [("volume", 20)]
    d.on_state("speaking")
    assert ("pause", None) in fake.calls and fake.paused
    d.on_state("thinking")                     # mid-turn: stays paused
    d.on_state("idle")
    assert fake.calls[-2:] == [("volume", 80), ("play", None)] and not fake.paused


def test_does_not_resume_what_the_person_paused(tmp_path):
    fake = FakeBrowser(paused=True)
    d = ducker(tmp_path, fake)
    d.on_state("speaking")
    d.on_state("idle")
    assert ("play", None) not in fake.calls and ("pause", None) not in fake.calls


def test_idle_when_nothing_active(tmp_path):
    fake = FakeBrowser()
    d = ducker(tmp_path, fake, active=False)
    for s in ("listening", "speaking", "idle"):
        d.on_state(s)
    assert fake.calls == []
