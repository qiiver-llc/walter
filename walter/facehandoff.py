"""Handoff helpers for `walterctl face send NAME media` (docs/FACES.md).

"Move this to the kitchen" with a podcast or the radio playing on the wall: find
what's playing (stream URL, title, position), ask the brain to start it on the
target face's speaker agent, and only then stop it here. Loaders are injectable
so tests never touch mpv or ~/.walter.
"""
from __future__ import annotations

import re

URL_RE = re.compile(r"^https?://[^\s\"'<>]{3,2000}$")


def media_now(podcast_mod=None, radio_mod=None) -> dict | None:
    """{"url", "title", "start", "source", "stop": fn} of what plays on the wall, or None."""
    if podcast_mod is None:
        from . import podcast as podcast_mod
    if radio_mod is None:
        from . import radio as radio_mod
    try:
        st = podcast_mod.load()
        now = st.get("now")
        if now and now.get("url") and podcast_mod.player_running():
            pos = podcast_mod.get("time-pos")
            pos = float(pos) if isinstance(pos, (int, float)) else 0.0
            return {"url": now["url"], "title": f"{now.get('show', '')}: {now.get('title', '')}".strip(": "),
                    "start": max(0.0, pos - 2), "source": "podcast",
                    "stop": lambda: podcast_mod.stop(st, quiet=True)}
    except Exception:
        pass
    try:
        rst = radio_mod.load()
        now = rst.get("now")
        if now and now.get("url") and radio_mod.player_running():
            return {"url": now["url"], "title": now.get("name") or "Live radio", "start": 0.0,
                    "source": "radio", "stop": lambda: radio_mod.stop(rst, quiet=True)}
    except Exception:
        pass
    return None


def handoff_media(face: str, post, now: dict | None = None, **loaders) -> tuple[bool, str]:
    """Start what plays here on `face` (post(path, body) -> dict, raising on HTTP errors),
    then stop it here. Returns (ok, message). Nothing stops if the face can't take it."""
    now = now if now is not None else media_now(**loaders)
    if not now:
        return False, "nothing is playing on the wall (podcast or radio) to move"
    if not URL_RE.match(str(now.get("url") or "")):
        return False, "what's playing has no plain stream address I can hand over"
    body = {"face": face, "kind": "media", "url": now["url"], "title": now.get("title") or "",
            "start": now.get("start") or 0, "source": now.get("source") or ""}
    try:
        r = post("/api/faces/send", body)
    except Exception as e:   # the face has no speaker agent, unknown name, brain down...
        return False, f"couldn't move it: {e}"
    try:
        now["stop"]()
    except Exception:
        pass
    return True, f"moved {now.get('title') or 'it'} to {r.get('face', face)}"
