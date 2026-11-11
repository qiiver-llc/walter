"""Chat images from the companion app: type/size validation, auth, storage,
cleanup, no path traversal, and the brain/conductor hand-off.
Never touches ~/.walter (WALTER_DATA points at tmp_path)."""
import asyncio
import datetime as dt
import json
import os
import time

import pytest

from walter.app_uploads import UploadError, UploadStore, sniff
from walter.brain import user_content
from tests.test_app import PROXY, iso, make_app, run  # noqa: F401  (fixture)

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 50
GIF = b"GIF89a" + b"\x00" * 50


def test_sniff_only_allowed_magic():
    assert [sniff(x) for x in (JPEG, PNG, WEBP, GIF)] == ["jpg", "png", "webp", "gif"]
    for bad in (b"", b"<svg xmlns=", b"#!/bin/sh\n", b"%PDF-1.4", b"RIFF\x00\x00\x00\x00WAVE", b"MZ\x90"):
        assert sniff(bad) is None


def test_save_validates_and_stores_privately(tmp_path):
    st = UploadStore(tmp_path / "up", max_bytes=1000, per_day=3, bytes_per_day=10_000)
    r = st.save(JPEG)
    assert r["type"] == "image/jpeg" and r["id"].endswith(".jpg")
    p = st.path(r["id"])
    assert p and p.read_bytes() == JPEG and p.parent.name == dt.date.today().isoformat()
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"   # never executable
    assert oct(os.stat(p.parent).st_mode & 0o777) == "0o700"
    with pytest.raises(UploadError) as e:
        st.save(b"<svg onload=alert(1)>")
    assert e.value.status == 415
    with pytest.raises(UploadError) as e:
        st.save(JPEG + b"\x00" * 1000)
    assert e.value.status == 413
    with pytest.raises(UploadError):
        st.save(b"")
    st.save(PNG), st.save(GIF)
    with pytest.raises(UploadError) as e:   # 4th today: daily cap
        st.save(WEBP)
    assert e.value.status == 429
    # random names: two saves of the same bytes don't collide
    st2 = UploadStore(tmp_path / "up2")
    assert st2.save(JPEG)["id"] != st2.save(JPEG)["id"]


def test_daily_byte_cap(tmp_path):
    st = UploadStore(tmp_path / "up", bytes_per_day=250)
    st.save(JPEG)
    with pytest.raises(UploadError) as e:
        st.save(PNG + b"\x00" * 100)
    assert e.value.status == 429


def test_path_rejects_traversal_and_junk(tmp_path):
    st = UploadStore(tmp_path / "up")
    good = st.save(PNG)["id"]
    (tmp_path / "secret.png").write_bytes(PNG)
    day = good.split("_")[0]
    os.symlink(tmp_path / "secret.png", tmp_path / "up" / day / ("L" * 24 + ".png"))
    for bad in ("../secret.png", f"{day}_../../secret.png", f"{day}/x.png", "", None, 5,
                f"{day}_{'L' * 24}.png",            # a symlink
                f"{day}_{'a' * 24}.png",            # doesn't exist
                f"{day}_{'a' * 24}.svg", good.replace(".png", ".exe"), "..%2f..%2fsecret.png"):
        assert st.path(bad) is None, bad
    assert st.path(good) is not None


def test_cleanup_deletes_after_7_days(tmp_path):
    st = UploadStore(tmp_path / "up")
    st.save(JPEG)
    today = dt.date.today()
    for ago in (6, 7, 30):
        d = tmp_path / "up" / (today - dt.timedelta(days=ago)).isoformat()
        d.mkdir()
        (d / "x.jpg").write_bytes(JPEG)
    (tmp_path / "up" / "notaday").mkdir()
    assert st.cleanup() == 2
    left = sorted(p.name for p in (tmp_path / "up").iterdir())
    assert left == sorted([today.isoformat(), (today - dt.timedelta(days=6)).isoformat(), "notaday"])
    assert UploadStore(tmp_path / "nothing").cleanup() == 0


def test_user_content_blocks(tmp_path):
    assert user_content("hi") == "hi" and user_content("hi", []) == "hi"
    p = tmp_path / "a.png"
    p.write_bytes(PNG)
    c = user_content("what is this?", [str(p), str(tmp_path / "gone.jpg"), str(tmp_path / "x.txt")])
    assert [b["type"] for b in c] == ["image", "text"]
    assert c[0]["source"]["media_type"] == "image/png" and c[0]["source"]["type"] == "base64"
    assert c[1]["text"] == "what is this?"
    assert user_content("x", [str(tmp_path / "gone.jpg")]) == "x"   # nothing readable: plain text


def test_frame_marks_images_untrusted():
    from unittest.mock import MagicMock
    from walter.conductor import Conductor
    fake = MagicMock(realtime=None)
    meta = {"route": "app", "device": "Phone", "owner": "Alex", "images": ["/x/a.jpg"]}
    f = Conductor._frame(fake, "app", "", meta)
    assert "1 image(s)" in f and "/x/a.jpg" in f and "untrusted" in f and "(no text, just the image)" in f
    assert "image" not in Conductor._frame(fake, "app", "hi", {"route": "app"})


def _form(*parts):
    from aiohttp import FormData
    fd = FormData()
    for name, data in parts:
        fd.add_field(name, data, filename="p.jpg", content_type="image/jpeg")
    return fd


def test_upload_route_auth_limits_and_chat(iso):  # noqa: F811
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        app, apphub, cond, _ = make_app(iso)
        assert str(apphub.uploads.root).startswith(str(iso))
        async with TestClient(TestServer(app)) as c:
            # auth required, from the network and through a proxy
            assert (await c.post("/api/app/upload", data=_form(("image", JPEG)), headers=PROXY)).status == 401
            dev, token = apphub.store.pair(apphub.store.start_pairing()["code"], "Phone", "Alex")
            auth = {**PROXY, "Authorization": f"Bearer {token}"}
            r = await c.post("/api/app/upload", data=_form(("image", JPEG), ("image", PNG)), headers=auth)
            j = await r.json()
            assert r.status == 200 and len(j["images"]) == 2 and j["errors"] == []
            iid = j["images"][0]["id"]
            # fetching needs auth too, and only by a valid id
            assert (await c.get(f"/api/app/upload/{iid}", headers=PROXY)).status == 401
            r = await c.get(f"/api/app/upload/{iid}", headers=auth)
            assert r.status == 200 and await r.read() == JPEG and r.headers["Content-Type"] == "image/jpeg"
            assert "no-store" in r.headers["Cache-Control"]
            for bad in ("..%2F..%2Fdevices.json", "nope.jpg"):
                assert (await c.get(f"/api/app/upload/{bad}", headers=auth)).status in (403, 404)   # gate refuses dot segments first
            # not served from the static /app/ folder either
            assert (await c.get(f"/app/{iid}", headers=auth)).status in (403, 404)
            # wrong type, too many, not multipart
            r = await c.post("/api/app/upload", data=_form(("image", b"<html>")), headers=auth)
            assert r.status == 400 and "JPEG" in (await r.json())["error"]
            r = await c.post("/api/app/upload", data=_form(*[("image", JPEG)] * 5), headers=auth)
            j2 = await r.json()
            assert len(j2["images"]) == 4 and "4 images" in j2["errors"][0]
            r = await c.post("/api/app/upload", json={"image": "x"}, headers=auth)
            assert r.status == 415
            # too big (streamed check)
            apphub.uploads.max_bytes = 500
            r = await c.post("/api/app/upload", data=_form(("image", JPEG + b"\x00" * 1000)), headers=auth)
            assert r.status == 400 and "too big" in (await r.json())["error"]
            # the status tells the app images work
            r = await c.get("/api/app/status", headers=auth)
            assert "images" in (await r.json()).get("features", [])

            # chat over the websocket with the image ids -> conductor gets the paths
            ws = await c.ws_connect("/app/ws", headers=PROXY)
            await ws.send_str(json.dumps({"t": "auth", "token": token}))
            assert (await ws.receive_json())["ok"]
            await ws.send_str(json.dumps({"t": "chat", "text": "", "id": "m1",
                                          "images": [iid, "../../etc/passwd", iid, 7]}))
            for _ in range(10):
                m = await ws.receive_json(timeout=2)
                if m.get("t") == "queued":
                    break
            assert m["t"] == "queued"
            src, text, meta = cond.submit.call_args.args
            assert src == "app" and text == "" and meta["images"] == [str(apphub.uploads.path(iid))]
            assert meta["route"] == "app"   # replies still go to the app only
            await ws.close()
    run(go())


def test_brain_sends_image_blocks(tmp_path):
    """Brain.ask writes the content array into the stream-json user message."""
    from walter.brain import Brain

    written = []

    class FakeStdin:
        def write(self, b): written.append(b)
        async def drain(self): pass

    class FakeProc:
        returncode = None
        stdin = FakeStdin()

    async def go():
        from walter.config import Config
        b = Brain.__new__(Brain)
        b.lock = asyncio.Lock()
        b.proc = FakeProc()
        b.turn_open = False

        async def turn():
            b.turn_open = False
            yield ("done", "ok")
        b._turn = turn
        p = tmp_path / "a.jpg"
        p.write_bytes(JPEG)
        out = [x async for x in b.ask("look", images=[str(p)])]
        assert out == [("done", "ok")]
    run(go())
    msg = json.loads(written[0])
    content = msg["message"]["content"]
    assert content[0]["type"] == "image" and content[-1] == {"type": "text", "text": "look"}
