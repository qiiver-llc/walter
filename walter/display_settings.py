"""Wall display settings in face/display.json (rotation, portrait layout, text scale).

One code path for `walterctl display text-scale` and the companion app's
"Wall text size" slider (/api/app/display). The face polls display.json every
~2 s (face/textscale.js, face/rotate.html), so writing the file IS the live
update; nothing else needs to be told.
"""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path

TEXT_SCALE_DEFAULT = 0.8
TEXT_SCALE_MIN = 0.5
TEXT_SCALE_MAX = 1.5
_OLD_MTIME = 946684800   # 2000-01-01: the face's edit watcher never mistakes it for a code edit


def display_path() -> Path:
    from . import dashboard   # looked up at call time so tests can point FACE_DIR elsewhere
    return Path(dashboard.FACE_DIR) / "display.json"


def read_display(path: Path | str | None = None) -> dict:
    try:
        with open(path or display_path()) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def write_display(path: Path | str | None = None, **changes) -> dict:
    """Merge changes into display.json atomically (rotation, layout and scale live together)."""
    path = str(path or display_path())
    d = read_display(path)
    d.update(changes)
    d["set"] = int(time.time())
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.utime(tmp, (_OLD_MTIME, _OLD_MTIME))
    os.replace(tmp, path)
    return d


def parse_text_scale(value) -> float:
    """A number from 0.5 to 1.5 (rounded to 0.01), else ValueError. Bools and NaN are refused."""
    if isinstance(value, bool):
        raise ValueError("not a number")
    try:
        v = round(float(value), 2)
    except (TypeError, ValueError):
        raise ValueError("not a number") from None
    if not math.isfinite(v) or not TEXT_SCALE_MIN <= v <= TEXT_SCALE_MAX:
        raise ValueError(f"must be from {TEXT_SCALE_MIN} to {TEXT_SCALE_MAX}")
    return v


def get_text_scale(path: Path | str | None = None) -> float:
    try:
        return parse_text_scale(read_display(path).get("text_scale", TEXT_SCALE_DEFAULT))
    except ValueError:
        return TEXT_SCALE_DEFAULT


def set_text_scale(value, path: Path | str | None = None) -> tuple[float, float]:
    """Validate and store; returns (new, old). The face applies it within ~2 s."""
    v = parse_text_scale(value)
    old = get_text_scale(path)
    write_display(path, text_scale=v)
    return v, old


# ---- themes (face/themes.js; the owner, 10 Nov): display.json "theme" = a name or "auto" ----------------
# Keep in the same order as THEMES in face/themes.js (tests/test_themes.py checks it).
THEMES = ("midnight", "light", "cream", "fall", "spring", "summer", "winter", "sunset", "ocean", "forest")
THEME_DEFAULT = "midnight"


def parse_theme(value) -> str:
    """A theme name or "auto" (case-insensitive), else ValueError."""
    if not isinstance(value, str):
        raise ValueError("not a theme name")
    v = value.strip().lower()
    if v == "autumn":
        v = "fall"
    if v != "auto" and v not in THEMES:
        raise ValueError("must be one of " + ", ".join(THEMES) + " or auto")
    return v


def get_theme(path: Path | str | None = None) -> str:
    try:
        return parse_theme(read_display(path).get("theme", THEME_DEFAULT))
    except ValueError:
        return THEME_DEFAULT


def auto_theme(now: time.struct_time | None = None) -> str:
    """Same rule as autoTheme() in face/themes.js: midnight 10 PM-6 AM, else the season."""
    t = now or time.localtime()
    if t.tm_hour >= 22 or t.tm_hour < 6:
        return "midnight"
    m = t.tm_mon
    return "winter" if m in (12, 1, 2) else "spring" if m <= 5 else "summer" if m <= 8 else "fall"


def showing_theme(path: Path | str | None = None) -> str:
    """The theme on the wall right now (auto resolved)."""
    v = get_theme(path)
    return auto_theme() if v == "auto" else v


def set_theme(value, path: Path | str | None = None) -> tuple[str, str]:
    """Validate and store; returns (new, old). The face applies it within ~2 s."""
    v = parse_theme(value)
    old = get_theme(path)
    write_display(path, theme=v)
    return v, old


def step_theme(step: str, path: Path | str | None = None, rnd=None) -> tuple[str, str]:
    """"next" (after the one showing) or "random" (any other one); manual, so it pauses auto."""
    import random
    cur = showing_theme(path)
    if step == "next":
        new = THEMES[(THEMES.index(cur) + 1) % len(THEMES)]
    else:
        new = (rnd or random).choice([n for n in THEMES if n != cur])
    return set_theme(new, path)


# ---- calendar style (face/calstyle.js; 10 Nov): display.json "calendar_style" -------------------
# "chips" = every event as a chip (the original look, default); "counts" = one tile per calendar with a
# count in the weeks that aren't expanded. Keep in step with STYLES in face/calstyle.js.
CAL_STYLES = ("chips", "counts")
CAL_STYLE_DEFAULT = "chips"


def parse_calendar_style(value) -> str:
    if not isinstance(value, str):
        raise ValueError("not a calendar style")
    v = value.strip().lower()
    v = {"chip": "chips", "count": "counts", "tiles": "counts"}.get(v, v)
    if v not in CAL_STYLES:
        raise ValueError("must be one of " + ", ".join(CAL_STYLES))
    return v


def get_calendar_style(path: Path | str | None = None) -> str:
    try:
        return parse_calendar_style(read_display(path).get("calendar_style", CAL_STYLE_DEFAULT))
    except ValueError:
        return CAL_STYLE_DEFAULT


def set_calendar_style(value, path: Path | str | None = None) -> tuple[str, str]:
    """"chips", "counts" or "toggle"; returns (new, old). The face applies it within ~2 s."""
    old = get_calendar_style(path)
    if isinstance(value, str) and value.strip().lower() == "toggle":
        v = "chips" if old == "counts" else "counts"
    else:
        v = parse_calendar_style(value)
    write_display(path, calendar_style=v)
    return v, old


# ---- per-face settings (walter/faces.py; one brain, many faces) ----------------------------------
# display.json stays the wall's. Every other face has face/display-<id>.json, written by the same
# helpers above (pass the path). The face page loaded with ?face=<id> reads its own file
# (face/faceid.js), and the brain's /face/ route serves it as display.json.
FACE_COPY_KEYS = ("theme", "text_scale", "calendar_style")   # a new face starts with the wall's look


def face_display_path(face_id: str | None) -> Path:
    """display.json for the wall (None / "wall"), face/display-<id>.json for any other face."""
    if not face_id or face_id == "wall":
        return display_path()
    from .faces import valid_id
    if not valid_id(face_id):
        raise ValueError("bad face id")
    return display_path().with_name(f"display-{face_id}.json")


def seed_face_display(face_id: str) -> dict:
    """A new face's display file, starting from the wall's theme, text size and calendar style."""
    path = face_display_path(face_id)
    if path.exists():
        return read_display(path)
    wall = read_display()
    return write_display(path, **{k: wall[k] for k in FACE_COPY_KEYS if k in wall})
