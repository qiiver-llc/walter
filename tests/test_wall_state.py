"""Wall state reported by the face (walter/wall_state.py, face/wallstate.js): local-only POST, strict
whitelist (no text fields), size limit, paired-device auth on the app GET, stale after 15 s."""
import asyncio
import json

import pytest

from walter import wall_state as W
from tests.test_app import PROXY, iso, make_app  # noqa: F401  (iso is a fixture)

LOCAL = {"Host": "127.0.0.1:8765", "Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"}


def snap(**over):
    d = {"t": 1794412761, "mode": "build",
         "build": {"on": True, "sticky": True, "showing": True, "phase": "plan", "zoom": "diff", "plan": True},
         "theme": "cream", "theme_auto": False, "layout": "large", "layout_pref": "auto",
         "orientation": "portrait", "conversation": {"on": True, "view": "build"},
         "focus": {"on": False, "status": ""}, "demo": False, "workspace": "acme",
         "asleep": False, "muted": False, "canvas": False, "browser": False}
    d.update(over)
    return d


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def post(store, d, peer="127.0.0.1", headers=None, raw=None):
    body = raw if raw is not None else json.dumps(d).encode()
    return W.handle_post(store, peer, headers or LOCAL, (headers or LOCAL).get("Host", ""), len(body), body)


def test_parse_accepts_the_face_shape_and_every_mode():
    for m in W.MODES:
        out = W.parse(snap(mode=m))
        assert out["mode"] == m and out["build"]["zoom"] == "diff" and out["workspace"] == "acme"
    # defaults for missing parts
    out = W.parse({"t": 5, "mode": "face"})
    assert out["build"] == {"on": False, "sticky": False, "showing": False, "phase": "build", "zoom": "", "plan": False}
    assert out["conversation"] == {"on": False, "view": "default"} and out["workspace"] is None
    # demo mode never carries the workspace
    assert W.parse(snap(demo=True, mode="presentation"))["workspace"] is None


@pytest.mark.parametrize("bad", [
    [], "x", {"mode": "face"}, snap(t="now"), snap(t=True), snap(mode="disco"),
    snap(title="Call the vet"), snap(text="hello"), snap(task="secret"),        # no text fields at all
    snap(focus={"on": True, "task": "Call the vet"}),                              # not even nested
    snap(build={"on": True, "plan": "my plan text"}), snap(build={"zoom": "kitchen"}),
    snap(theme="Cream Theme!"), snap(theme=3), snap(workspace="Acme Corp"), snap(workspace="../x"),
    snap(muted="yes"), snap(orientation="sideways"), snap(conversation={"on": True, "view": "x"}),
    snap(conversation="on"), snap(layout="huge"),
])
def test_parse_refuses_junk_and_text(bad):
    with pytest.raises(W.Invalid):
        W.parse(bad)


def test_post_is_local_only_json_same_origin_and_small():
    st = W.WallState()
    assert post(st, snap())[0] == 200 and st.get()["mode"] == "build"
    # through a proxy / the public tunnel, or from the internet: no
    assert post(st, snap(), headers={**LOCAL, "X-Forwarded-For": "8.8.8.8"})[0] == 403
    assert post(st, snap(), peer="8.8.8.8", headers={**LOCAL, "Host": "x.example", "Origin": "http://x.example"})[0] == 403
    # a plain form from some web page, or another origin
    assert post(st, snap(), headers={**LOCAL, "Content-Type": "text/plain"})[0] == 415
    assert post(st, snap(), headers={**LOCAL, "Origin": "http://evil.example"})[0] == 403
    # home network is fine
    lan = {**LOCAL, "Host": "192.168.1.20:8765", "Origin": "http://192.168.1.20:8765"}
    assert post(st, snap(mode="face"), peer="192.168.1.30", headers=lan)[0] == 200
    # too big, bad JSON, bad shape: refused and the last good snapshot stays
    assert post(st, None, raw=b"{" + b" " * 5000 + b"}")[0] == 413
    assert post(st, None, raw=b"nope")[0] == 400
    assert post(st, snap(mode="disco"))[0] == 400
    assert st.get()["mode"] == "face"


def test_stale_logic():
    clk = Clock()
    st = W.WallState(now=clk)
    g = st.get()
    assert g["stale"] is True and g["reported"] is False
    st.put(W.parse(snap()))
    clk.t += 3
    g = st.get()
    assert g["stale"] is False and g["age"] == 3.0 and g["mode"] == "build"
    clk.t += W.STALE_S
    assert st.get()["stale"] is True and st.get()["mode"] == "build"   # still says what it last saw


def test_routes_auth_on_get_and_local_post(iso):  # noqa: F811
    from aiohttp.test_utils import TestClient, TestServer

    async def go():
        app, apphub, cond, hub = make_app(iso)
        async with TestClient(TestServer(app)) as c:
            # the app GET needs pairing through the proxy
            assert (await c.get("/api/app/wall", headers=PROXY)).status == 401
            # nothing reported yet: stale
            j = await (await c.get("/api/app/wall")).json()   # this machine = the kiosk, no token needed
            assert j["stale"] is True and j["reported"] is False
            # the face (this machine) reports
            r = await c.post("/api/wall/state", json=snap(mode="chat"), headers={"Origin": f"http://{c.host}:{c.port}"})
            assert r.status == 200, await r.text()
            # the proxy (tunnel) may not post, nor read the local GET
            assert (await c.post("/api/wall/state", json=snap(), headers=PROXY)).status in (403, 404)
            assert (await c.get("/api/wall/state", headers=PROXY)).status in (403, 404)
            r = await c.post("/api/wall/state", data=b"x" * 5000, headers={"Content-Type": "application/json"})
            assert r.status == 413
            code = apphub.store.start_pairing()["code"]
            token = (await (await c.post("/api/app/pair", json={"code": code, "name": "Phone"},
                                         headers=PROXY)).json())["token"]
            j = await (await c.get("/api/app/wall", headers={**PROXY, "Authorization": f"Bearer {token}"})).json()
            assert j["mode"] == "chat" and j["stale"] is False and j["age"] >= 0
            j = await (await c.get("/api/wall/state")).json()
            assert j["mode"] == "chat"
            assert hub.wall.get()["mode"] == "chat"
    asyncio.run(go())
