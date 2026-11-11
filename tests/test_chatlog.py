"""walter/chatlog.py (the app's full chat history) and /api/app/history (walter/app_history.py)."""
import asyncio
import json
import os

import pytest

from walter import app_history as H
from walter import chatlog as C
from tests.test_app import PROXY, iso, make_app  # noqa: F401  (iso is a fixture)


def recs(path):
    return [json.loads(x) for x in open(path)]


def test_write_ids_and_mode(tmp_path):
    log = C.ChatLog(tmp_path / "chat-history.jsonl")
    a = log.append("person", "what's the weather", "wall", "acme", ts=100)
    b = log.append("walter", "[happy] Sunny and mild. [action:grow]", "wall", "acme", ts=101)
    assert (a["id"], b["id"]) == (1, 2)
    assert b["text"] == "Sunny and mild."
    assert oct(os.stat(log.path).st_mode & 0o777) == "0o600"
    assert [r["role"] for r in recs(log.path)] == ["person", "walter"]
    with pytest.raises(ValueError):
        log.append("system", "x")


def test_dedupe_consecutive(tmp_path):
    log = C.ChatLog(tmp_path / "c.jsonl")
    assert log.append("person", "hello", ts=1)
    assert log.append("person", "  Hello ", ts=2) is None
    assert log.append("walter", "hi", ts=3)
    assert log.append("person", "hello", ts=4)          # not consecutive any more
    assert log.append("person", "hello", ts=4 + C.DEDUPE_S + 1)   # an hour later: kept
    assert len(recs(log.path)) == 4


def test_skips_silent_tags_and_credentials(tmp_path):
    log = C.ChatLog(tmp_path / "c.jsonl")
    assert log.append("walter", "[silent]") is None
    assert log.append("walter", "[think:hard]") is None
    assert log.append("person", "sk-ant-" "api03-abcdefghijklmnop") is None   # split: keep scanners quiet
    assert log.append("person", "my password is hunter2") is None
    assert log.append("person", "x7Kq29LmPz0aQ4rT") is None
    r = log.append("person", "use token=abc123 and ghp_abcdefghijklmnopqrst please")
    assert "abc123" not in r["text"] and "ghp_" not in r["text"] and "[redacted]" in r["text"]
    assert log.append("person", "the key is under the mat")["text"] == "the key is under the mat"


def test_strip_tags_and_headers():
    assert C.strip_tags("[typed in the companion app on Phone (the owner) at 1:00; away.]\nadd milk") == "add milk"
    assert C.strip_tags("[heard through the microphone at 9; errors]\nhello there") == "hello there"
    assert C.strip_tags("[wink] Ooh, nice. [action:scoot:left] Right then.") == "Ooh, nice. Right then."
    assert C.strip_tags("see [1] in the list") == "see [1] in the list"


def test_rotation_keeps_two_files_and_ids_continue(tmp_path):
    log = C.ChatLog(tmp_path / "c.jsonl", max_bytes=300)
    for i in range(30):
        log.append("person" if i % 2 else "walter", f"message number {i}", ts=i)
    assert log.old_path.exists() and log.path.exists()
    assert not (tmp_path / "c.2.jsonl").exists()
    ids = [r["id"] for r in log.iter_newest()]
    assert ids == sorted(ids, reverse=True) and ids[0] == 30
    assert len(set(ids)) == len(ids)


def test_page_and_search(tmp_path):
    log = C.ChatLog(tmp_path / "c.jsonl", max_bytes=1500)   # rotates once: pages span both files
    for i in range(1, 21):
        log.append("person", f"note {i} " + ("banana" if i % 5 == 0 else "apple"), ts=i)
    p = log.page(limit=5)
    assert [r["id"] for r in p["items"]] == [20, 19, 18, 17, 16] and p["has_more"]
    p = log.page(before=3, limit=5)
    assert [r["id"] for r in p["items"]] == [2, 1] and not p["has_more"]
    p = log.page(q="BANANA", limit=3)
    assert [r["id"] for r in p["items"]] == [20, 15, 10] and p["has_more"]
    p = log.page(q="banana", before=10, limit=3)
    assert [r["id"] for r in p["items"]] == [5] and not p["has_more"]


@pytest.mark.parametrize("q", [{"before": "0"}, {"before": "-1"}, {"before": "x"}, {"before": "1" * 13},
                               {"limit": "0"}, {"limit": "101"}, {"limit": "5.5"}, {"q": ""}, {"q": "a" * 101},
                               {"q": "a\x00b"}, {"evil": "1"}])
def test_parse_refuses_junk(q):
    with pytest.raises(ValueError):
        H.parse(q)


def test_parse_ok():
    assert H.parse({}) == {"before": None, "limit": 50, "q": None}
    assert H.parse({"before": "12", "limit": "100", "q": "  tea  time "}) == {"before": 12, "limit": 100,
                                                                           "q": "tea time"}


def test_api_auth_paging_search(iso, monkeypatch):  # noqa: F811
    from aiohttp.test_utils import TestClient, TestServer
    log = C.ChatLog()   # conftest points the default log into this test's temp dir
    assert "chatlog" in str(log.path) and not str(log.path).startswith(str(C.Path.home() / ".walter"))
    for i in range(1, 8):
        log.append("person" if i % 2 else "walter", f"line {i}" + (" tea" if i == 4 else ""), ts=i)

    async def go():
        app, apphub, _, _ = make_app(iso)
        async with TestClient(TestServer(app)) as c:
            r = await c.get("/api/app/history", headers=PROXY)
            assert r.status == 401
            code = apphub.store.start_pairing()["code"]
            _, token = apphub.store.pair(code, "Phone", "the owner")
            h = {**PROXY, "Authorization": "Bearer " + token}
            r = await c.get("/api/app/history?limit=3", headers=h)
            d = await r.json()
            assert [x["id"] for x in d["items"]] == [7, 6, 5] and d["has_more"]
            r = await c.get("/api/app/history?limit=3&before=5", headers=h)
            d = await r.json()
            assert [x["id"] for x in d["items"]] == [4, 3, 2] and d["has_more"]
            r = await c.get("/api/app/history?q=tea", headers=h)
            d = await r.json()
            assert [x["id"] for x in d["items"]] == [4] and not d["has_more"]
            r = await c.get("/api/app/history?limit=1000", headers=h)
            assert r.status == 400
    asyncio.run(go())
