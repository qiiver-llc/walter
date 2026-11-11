"""YouTube channel follow: resolving channels from public pages, the RSS feed, up next
(merged into the podcast up-next file for the face) and play. Fake data, no network."""
import json
from datetime import datetime
from pathlib import Path

import pytest

from walter import podcast, youtube

FEED = (Path(__file__).parent / "fixtures" / "youtube" / "feed.xml").read_bytes()
CID = "UCaaaaaaaaaaaaaaaaaaaaaa"
CH = {"id": CID, "title": "Sample Builders", "handle": "@samplebuilders"}
NOW = datetime.fromisoformat("2026-11-09T12:00:00+00:00").timestamp()
SEARCH = ('{"contents":[{"channelRenderer":{"channelId":"UCbbbbbbbbbbbbbbbbbbbbbb","title":{"simpleText":"Builders Reacts"},'
          '"navigationEndpoint":{"browseEndpoint":{"canonicalBaseUrl":"/@buildersreacts"}}}},'
          '{"channelRenderer":{"channelId":"' + CID + '","title":{"simpleText":"Sample Builders"},'
          '"navigationEndpoint":{"browseEndpoint":{"canonicalBaseUrl":"/@samplebuilders"}}}}]}')


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube, "STATE", tmp_path / "youtube.json")
    monkeypatch.setattr(youtube, "CACHE_DIR", tmp_path / "ytcache")
    monkeypatch.setattr(youtube, "_refresh_face", lambda: None)
    return tmp_path


def fake_fetch(calls=None):
    def f(url):
        if calls is not None:
            calls.append(url)
        if url.startswith(youtube.FEED):
            return FEED
        if "/results?" in url:
            return SEARCH.encode()
        if "/@samplebuilders" in url:
            return b'<html><meta itemprop="identifier" content="' + CID.encode() + b'">"browseId":"UCcccccccccccccccccccccc"'
        raise OSError("unexpected " + url)
    return f


def test_parse_feed_newest_first_clean_titles_and_safe_thumbs():
    v = youtube.parse_feed(FEED)
    assert [x["vid"] for x in v] == ["vidBBBBBBB2", "vidAAAAAAA1"]
    assert v[1]["title"] == "Older build bvideo/b" and "<" not in v[1]["title"]
    assert v[1]["img"].startswith("https://i1.ytimg.com/")
    assert v[0]["img"] == "https://i.ytimg.com/vi/vidBBBBBBB2/hqdefault.jpg"   # off-site thumb replaced
    assert v[0]["url"] == "https://www.youtube.com/watch?v=vidBBBBBBB2" and v[0]["channel"] == "Sample Builders"


def test_resolve_by_name_prefers_exact_title_over_first_result():
    ch = youtube.resolve("sample builders", fetch=fake_fetch())
    assert ch == {"id": CID, "title": "Sample Builders", "handle": "@samplebuilders"}


def test_resolve_by_handle_and_by_id():
    assert youtube.resolve("@samplebuilders", fetch=fake_fetch())["id"] == CID
    calls = []
    assert youtube.resolve("https://www.youtube.com/channel/" + CID, fetch=fake_fetch(calls))["title"] == "Sample Builders"
    assert all(c.startswith(youtube.FEED) for c in calls)
    assert youtube.page_url("https://evil.invalid/@x") is None


def test_videos_cached_30_min_then_refetched(env):
    calls = []
    youtube.videos(CH, fetch=fake_fetch(calls), now=NOW)
    youtube.videos(CH, fetch=fake_fetch(calls))
    assert len(calls) == 1
    assert youtube.videos({"id": "UCdddddddddddddddddddddd"}, fetch=fake_fetch(), cache_only=True) == []


def test_upnext_skips_watched_and_marks_new(env):
    st = {"channels": [CH], "watched": {"vidBBBBBBB2": 1}}
    items = youtube.upnext(st, fetch=fake_fetch(), now=NOW)
    assert [i["guid"] for i in items] == ["yt:vidAAAAAAA1"]
    assert items[0]["source"] == "youtube" and items[0]["show"] == "Sample Builders" and not items[0]["new"]
    st["watched"] = {}
    first = youtube.upnext(st, fetch=fake_fetch(), now=NOW)[0]
    assert first["guid"] == "yt:vidBBBBBBB2" and first["new"]   # a day old: new dot


def test_cli_follow_play_marks_watched(env, capsys):
    played = []
    assert youtube.cli(["follow", "Sample", "Builders"], fetch=fake_fetch()) == 0
    assert "Following Sample Builders" in capsys.readouterr().out
    assert youtube.load()["channels"][0]["id"] == CID
    assert youtube.cli(["play"], fetch=fake_fetch(), player=lambda a: played.append(a) or 0) == 0
    assert played[-1] == ["youtube", "url", "https://www.youtube.com/watch?v=vidBBBBBBB2"]
    assert "vidBBBBBBB2" in youtube.load()["watched"]
    assert youtube.cli(["play", "sample", "builders", "older"], fetch=fake_fetch(),
                       player=lambda a: played.append(a) or 0) == 0
    assert played[-1][-1].endswith("vidAAAAAAA1")
    assert youtube.cli(["unfollow", "builders"], fetch=fake_fetch()) == 0 and not youtube.load()["channels"]


def test_cli_follow_not_found(env, capsys):
    assert youtube.cli(["follow", "nobody"], fetch=lambda u: b"{}") == 1


def test_merge_into_podcast_upnext_with_thumbnail_art(env, tmp_path, monkeypatch):
    monkeypatch.setattr(podcast, "_ffmpeg_square", lambda s, d: d.write_bytes(b"J") > 0)
    monkeypatch.setattr(podcast, "CACHE_DIR", tmp_path / "pcache")
    youtube.videos(CH, fetch=fake_fetch())               # warm the cache: publish reads cache_only
    out = tmp_path / "face" / "dashboard-podcast-upnext.json"
    out.parent.mkdir()
    podcast.publish_upnext({"shows": [], "episodes": {}, "now": None}, cache_only=True, path=out,
                           art_fetch=lambda u: b"img", yt_state={"channels": [CH], "watched": {}})
    d = json.loads(out.read_text())
    assert [i["source"] for i in d["items"]] == ["youtube", "youtube"]
    assert all((out.parent / i["art"]).exists() for i in d["items"]) and "img" not in d["items"][0]


def test_merge_keeps_resume_first():
    pods = [{"guid": "p1", "resume": True, "date": 1}, {"guid": "p2", "resume": False, "date": 5}]
    st = {"channels": [], "watched": {}}
    assert podcast.merge_youtube(pods, st) == pods
    assert podcast.merge_youtube(pods, None, real=False) == pods


PAGE = ('"lockupViewModel":{"contentImage":{},"overlays":[{"text":"12:03"}],"contentId":"vidCCCCCCC3",'
        '"metadata":{"lockupMetadataViewModel":{"title":{"content":"Page \\u0026 fallback"},'
        '"metadata":{"x":[{"text":{"content":"1.2M"}},{"text":{"content":"3 days ago"}}]}}}}'
        '"lockupViewModel":{"contentImage":{},"contentId":"vidDDDDDDD4",'
        '"metadata":{"lockupMetadataViewModel":{"title":{"content":"Two weeks"},'
        '"metadata":{"x":[{"text":{"content":"2w ago"}}]}}}}')


def test_videos_fall_back_to_public_page_when_feed_is_down(env):
    def fetch(url):
        if url.startswith(youtube.FEED):
            raise OSError("404")
        assert url == f"https://www.youtube.com/channel/{CID}/videos"
        return PAGE.encode()
    v = youtube.videos(CH, fetch=fetch, now=NOW)
    assert [x["vid"] for x in v] == ["vidCCCCCCC3", "vidDDDDDDD4"]
    assert v[0]["title"] == "Page & fallback" and v[0]["date"] == NOW - 3 * 86400
    assert v[1]["date"] == NOW - 14 * 86400 and v[0]["img"].startswith("https://i.ytimg.com/vi/vidCCCCCCC3/")
    assert [x["vid"] for x in youtube.videos(CH, fetch=None, cache_only=True)] == ["vidCCCCCCC3", "vidDDDDDDD4"]


def test_age_parsing():
    assert youtube._age("2w ago") == 14 * 86400 and youtube._age("Streamed 5 hours ago") == 5 * 3600
    assert youtube._age("1 month ago") == 2592000 and youtube._age("22M") is None
