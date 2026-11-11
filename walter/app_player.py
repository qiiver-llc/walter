"""Companion app player: podcast (mpv, walter/podcast.py) and YouTube (wall browser,
walter/media.py) controls for paired devices.

GET  /api/app/player              state: {kind: podcast|youtube|none, title, show, art, pos,
                                  dur, paused, speed, volume, ducked, upnext: [...], resume}
POST /api/app/player/{action}     {value?, guid?, kind?}; action from a fixed whitelist
GET  /api/app/player/art/{name}   cover art (face/podcast-art/<hex>.jpg), token required

Actions never go through a shell: each one maps to a fixed argv list for
`python -m walter.podcast ...` / `python -m walter.media ...` (the same code `walterctl`
runs), and the only user values in it are validated numbers or an episode guid that must
be one of the up-next items we just listed.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sys
from pathlib import Path

from aiohttp import web

from . import media as M
from . import podcast as P
from .config import REPO_DIR

log = logging.getLogger("walter.app.player")

ACTIONS = ("toggle", "pause", "resume", "back", "forward", "seek", "speed", "volume", "stop", "next", "play")
SPEED_MIN, SPEED_MAX = 0.5, 2.0
ART_RE = re.compile(r"^[0-9a-f]{16}\.jpg$")
BACK_S, FORWARD_S = 15, 30


def _num(v, lo: float, hi: float) -> float:
    """A real number in [lo, hi], or ValueError (bools, strings and NaN refused)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        raise ValueError(f"must be a number from {lo:g} to {hi:g}")
    return float(v)


def _art_url(rel: str) -> str:
    name = Path(rel or "").name
    return f"/api/app/player/art/{name}" if ART_RE.match(name) else ""


def _upnext_file() -> dict:
    try:
        return json.loads(P.UPNEXT_FILE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def _upnext(skip_guid: str = "") -> list[dict]:
    out = []
    for i in (_upnext_file().get("items") or []):
        if not isinstance(i, dict) or not i.get("guid") or i.get("guid") == skip_guid:
            continue
        out.append({"guid": str(i["guid"]), "show": i.get("show", ""), "title": i.get("title", ""),
                    "dur": i.get("dur", 0), "pos": i.get("pos", 0), "resume": bool(i.get("resume")),
                    "new": bool(i.get("new")), "art": _art_url(i.get("art", ""))})
    return out[:4]


def _youtube_thumb(url: str) -> str:
    vid = M.video_id(url or "")
    if not vid:
        return ""
    try:   # cached once beside the face (podcast-art/), like podcast covers
        return _art_url(P.cache_art(f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"))
    except Exception:  # noqa: BLE001
        return ""


def player_state() -> dict:
    """Blocking (mpv IPC / browser socket, short timeouts): run in an executor."""
    st = P.load()
    np = P.now_playing(st) if st.get("now") else {"playing": False}
    if np.get("playing"):
        up = _upnext_file()
        now_art = _art_url((up.get("now") or {}).get("art", ""))
        guid = (st.get("now") or {}).get("guid", "")
        return {"kind": "podcast", "title": np.get("title", ""), "show": np.get("show", ""),
                "art": now_art, "pos": np.get("pos", 0), "dur": np.get("dur", 0),
                "paused": np.get("paused", False), "speed": np.get("speed", 1.0),
                "volume": int(st.get("volume", 100)), "ducked": np.get("ducked", False),
                "can_next": False, "upnext": _upnext(guid)}
    ms = M.load()
    if ms.get("active") and M.SOCK.exists():
        try:
            info = (M.send({"action": "info"}, timeout=2) or {}).get("info") or {}
        except (OSError, ValueError):
            info = {}
        if info.get("found"):
            return {"kind": "youtube", "title": info.get("title") or ms.get("title") or "YouTube",
                    "show": info.get("channel") or "YouTube", "art": _youtube_thumb(ms.get("url", "")),
                    "pos": round(float(info.get("time") or 0), 1), "dur": round(float(info.get("duration") or 0), 1),
                    "paused": bool(info.get("paused")), "speed": info.get("rate") or 1.0,
                    "volume": int(info.get("volume") if isinstance(info.get("volume"), (int, float)) else ms.get("volume", 60)),
                    "ducked": False, "ad": bool(info.get("ad")), "can_next": True, "upnext": _upnext()}
    return {"kind": "none", "upnext": _upnext(), "resume": np.get("resume") or P.resume_candidate(st)}


def argv_for(kind: str, action: str, d: dict, upnext_guids=()) -> list[str]:
    """The fixed argv (after `python -m walter.<module>`) for one validated action.
    Raises ValueError with a short message for anything not allowed."""
    if action not in ACTIONS:
        raise ValueError("unknown action")
    if kind not in ("podcast", "youtube"):
        raise ValueError("nothing is playing")
    v = d.get("value")
    if kind == "podcast":
        mod = ["walter.podcast"]
        simple = {"toggle": "toggle", "pause": "pause", "resume": "resume", "stop": "stop"}
        if action in simple:
            return mod + [simple[action]]
        if action == "back":
            return mod + ["back", str(BACK_S)]
        if action == "forward":
            return mod + ["skip", str(FORWARD_S)]
        if action == "seek":
            return mod + ["seekto", f"{_num(v, 0, 86400):.0f}"]
        if action == "speed":
            return mod + ["speed", f"{_num(v, SPEED_MIN, SPEED_MAX):g}"]
        if action == "volume":
            return mod + ["volume", str(int(_num(v, 0, 100)))]
        if action == "play":
            g = d.get("guid")
            if g is None:
                return mod + ["play"]
            if not isinstance(g, str) or g not in upnext_guids:
                raise ValueError("that episode isn't in Up next")
            return mod + ["play-guid", g]
        raise ValueError("not for podcasts")
    mod = ["walter.media"]
    simple = {"toggle": "toggle", "pause": "pause", "resume": "resume", "stop": "stop", "next": "next"}
    if action in simple:
        return mod + [simple[action]]
    if action == "back":
        return mod + ["back", str(BACK_S)]
    if action == "forward":
        return mod + ["skip", str(FORWARD_S)]
    if action == "seek":
        return mod + ["seek", f"{_num(v, 0, 86400):.0f}"]
    if action == "speed":
        return mod + ["speed", f"{_num(v, SPEED_MIN, SPEED_MAX):g}"]
    if action == "volume":
        return mod + ["volume", str(int(_num(v, 0, 100)))]
    raise ValueError("not for YouTube")


async def run_argv(argv: list[str], timeout: float = 30) -> tuple[int, str]:
    """python -m walter.<module> ARGS, no shell; (returncode, first line of output)."""
    try:
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", *argv, cwd=str(REPO_DIR),
                                                    stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return proc.returncode or 0, out.decode(errors="replace").strip()[:300]
    except Exception as e:  # noqa: BLE001
        log.warning("player %s failed: %s", argv[:2], e.__class__.__name__)
        return 1, "That didn't work."


def add_routes(routes, apphub, json_body, state_fn=None, runner=None, art_dir: Path | None = None):
    """state_fn / runner / art_dir are for tests."""
    loop_state = state_fn or (lambda: globals()["player_state"]())
    art_dir = art_dir or P.UPNEXT_FILE.with_name(P.FACE_ART)

    async def state() -> dict:
        try:
            return await asyncio.get_running_loop().run_in_executor(None, loop_state)
        except Exception as e:  # noqa: BLE001
            log.warning("player state failed: %s", e)
            return {"kind": "none", "upnext": [], "error": "couldn't read the player"}

    @routes.get("/api/app/player")
    async def app_player(request):
        apphub.auth(request)
        return web.json_response(await state())

    @routes.get("/api/app/player/art/{name}")
    async def app_player_art(request):
        apphub.auth(request)
        name = request.match_info["name"]
        f = art_dir / name
        if not ART_RE.match(name) or not f.is_file():
            raise web.HTTPNotFound()
        return web.Response(body=f.read_bytes(), content_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=86400"})

    @routes.post("/api/app/player/{action}")
    async def app_player_do(request):
        dev = apphub.auth(request)
        action = request.match_info["action"]
        d = await json_body(request, 1024)
        if action not in ACTIONS:
            return web.json_response({"error": "unknown action"}, status=400)
        cur = await state()
        kind = d.get("kind") if d.get("kind") in ("podcast", "youtube") else cur.get("kind")
        if action == "play" and kind not in ("podcast", "youtube"):
            kind = "podcast"   # nothing playing: Play starts the resume episode / an Up next item
        guids = {i.get("guid") for i in cur.get("upnext") or []}
        try:
            argv = argv_for(kind, action, d, guids)
        except ValueError as e:
            return web.json_response({"error": str(e), **cur}, status=400)
        rc, out = await (runner or globals()["run_argv"])(argv)
        log.info("app: %s player %s %s -> %s", dev.get("name"), kind, action, rc)
        new = await state()
        if rc != 0:
            return web.json_response({"error": out or "That didn't work.", **new}, status=502)
        return web.json_response({"ok": True, "said": out, **new})

    return True
