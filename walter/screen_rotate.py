"""Turn the wall's screen live: portrait / landscape / flip (the owner approved 10 Nov 12:57).

Shared by the companion app (/api/app/rotate, app_rotate.py) and `walterctl display rotate`.

  current()                -> {"outputs": {name: transform}, "orientation": "portrait"|"landscape", ...}
  set(orientation, persist=True)
      orientation: "portrait" | "landscape" | "flip"
      (a) turns every enabled output live with wlr-randr inside the kiosk's Wayland session
          (portrait = WALTER_ROTATE from ~/.walter/kiosk.env when it's 90/270, else the remembered
          WALTER_PORTRAIT, else 270; landscape = normal; flip = the other one from what's on screen now)
      (b) persist=True saves it in ~/.walter/kiosk.env as WALTER_ROTATE=0 (landscape) or 90/270 (portrait),
          keeping every other line, so the next kiosk start (deploy/kiosk-rotate.sh) comes up the same way.
          WALTER_PORTRAIT=90|270 remembers which way portrait goes while the screen is in landscape.
      (c) never raises: a missing wlr-randr, no screen or a failed call come back as {"ok": False, "error": ...}

The face re-lays itself out by itself once the window is taller than wide (or wider than tall).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading

log = logging.getLogger("walter.rotate")

ORIENTATIONS = ("portrait", "landscape", "flip", "portrait-flipped", "landscape-flipped")
PORTRAIT_TRANSFORMS = ("90", "270")
DEFAULT_PORTRAIT = "270"
TIMEOUT_S = 10
_lock = threading.Lock()


def kiosk_env_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".walter", "kiosk.env")


# ---------------------------------------------------------------- kiosk.env
def _read_env(path: str | None = None) -> list[str]:
    try:
        with open(path or kiosk_env_path()) as f:
            return f.read().splitlines()
    except OSError:
        return []


def _env_value(lines: list[str], key: str) -> str | None:
    for ln in lines:
        k, sep, v = ln.strip().partition("=")
        if sep and k.strip() == key:
            return v.strip().strip('"').strip("'")
    return None


def saved_rotate(path: str | None = None) -> str:
    """WALTER_ROTATE as saved ("0" when unset)."""
    return _env_value(_read_env(path), "WALTER_ROTATE") or "0"


def portrait_transform(path: str | None = None) -> str:
    """Which way portrait goes on this wall: WALTER_ROTATE if it's 90/270, else WALTER_PORTRAIT, else 270."""
    lines = _read_env(path)
    for key in ("WALTER_ROTATE", "WALTER_PORTRAIT"):
        v = _env_value(lines, key)
        if v in PORTRAIT_TRANSFORMS:
            return v
    return DEFAULT_PORTRAIT


def persist(orientation: str, transform: str | None = None, path: str | None = None) -> str:
    """Save WALTER_ROTATE (0 for landscape, 90/270 for portrait) keeping all other lines. Returns the value."""
    if orientation not in ("portrait", "landscape"):
        raise ValueError("orientation must be portrait or landscape")
    path = path or kiosk_env_path()
    lines = _read_env(path)
    if orientation == "portrait":
        want = transform if transform in PORTRAIT_TRANSFORMS else portrait_transform(path)
    else:
        want = "180" if transform == "180" else "0"   # 180 = landscape upside down
    keep_portrait = want if want in PORTRAIT_TRANSFORMS else portrait_transform(path)
    out, seen_rot, seen_por = [], False, False
    for ln in lines:
        k = ln.strip().partition("=")[0].strip()
        if k == "WALTER_ROTATE":
            if not seen_rot:
                out.append(f"WALTER_ROTATE={want}")
            seen_rot = True
        elif k == "WALTER_PORTRAIT":
            if not seen_por:
                out.append(f"WALTER_PORTRAIT={keep_portrait}")
            seen_por = True
        else:
            out.append(ln)
    if not seen_rot:
        out.append(f"WALTER_ROTATE={want}")
    if not seen_por:
        out.append(f"WALTER_PORTRAIT={keep_portrait}")
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(prefix=".kiosk.env.", dir=d)
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(out) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return want


# ---------------------------------------------------------------- wlr-randr
class RandrError(RuntimeError):
    pass


def _randr(*args) -> str:
    exe = shutil.which("wlr-randr")
    if not exe:
        raise RandrError("wlr-randr isn't installed (sudo apt install wlr-randr), so the screen can't turn live")
    env = dict(os.environ, WAYLAND_DISPLAY=os.environ.get("WALTER_WAYLAND", "wayland-0"),
               XDG_RUNTIME_DIR=os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=TIMEOUT_S, env=env)
    except subprocess.TimeoutExpired:
        raise RandrError("wlr-randr didn't answer in time") from None
    except OSError as e:
        raise RandrError(f"couldn't run wlr-randr: {e}") from None
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        raise RandrError("wlr-randr failed: " + (msg[-1][:200] if msg else f"exit {r.returncode}"))
    return r.stdout


def parse_outputs(text: str) -> dict:
    """{output name: transform} for the ENABLED outputs in `wlr-randr` output."""
    outs, name, enabled, transform = {}, None, False, None

    def flush():
        if name and enabled:
            outs[name] = transform or "normal"

    for line in (text or "").splitlines():
        if line and not line[0].isspace():
            flush()
            name, enabled, transform = line.split()[0], False, None
        elif "Enabled:" in line:
            enabled = "yes" in line.split(":", 1)[1]
        elif "Transform:" in line:
            transform = line.split(":", 1)[1].strip()
    flush()
    return outs


def orientation_of(transform: str) -> str:
    t = str(transform or "normal")
    return "portrait" if t.replace("flipped-", "") in PORTRAIT_TRANSFORMS else "landscape"


def outputs() -> dict:
    return parse_outputs(_randr())


def current() -> dict:
    """What the screen is doing now. Never raises."""
    saved = saved_rotate()
    out = {"outputs": {}, "orientation": orientation_of(saved if saved != "0" else "normal"),
           "saved": saved, "portrait_transform": portrait_transform(), "live": False, "error": None}
    try:
        outs = outputs()
    except RandrError as e:
        out["error"] = str(e)
        return out
    except Exception as e:  # noqa: BLE001
        log.exception("rotate: couldn't read the screen")
        out["error"] = f"couldn't read the screen: {e}"
        return out
    out["outputs"] = outs
    if outs:
        out["live"] = True
        out["orientation"] = orientation_of(next(iter(outs.values())))
    else:
        out["error"] = "no screen found (is the kiosk running?)"
    return out


def set_orientation(orientation: str, persist_it: bool = True, **kw) -> dict:
    """Turn the screen (see the module doc). Never raises; {"ok": bool, "orientation", "outputs", "saved", "error"}."""
    persist_it = kw.pop("persist", persist_it)
    if orientation not in ORIENTATIONS:
        return {"ok": False, "error": "orientation must be portrait, landscape, flip, portrait-flipped or landscape-flipped", "orientation": None}
    with _lock:
        now = current()
        target = orientation
        if orientation == "flip":
            target = "landscape" if now["orientation"] == "portrait" else "portrait"
        ptx = portrait_transform()
        # turning to portrait while it's already portrait live: keep the way it's turned
        if target in ("portrait", "portrait-flipped") and now["orientation"] == "portrait":
            live_t = next(iter(now["outputs"].values()), "")
            if live_t in PORTRAIT_TRANSFORMS:
                ptx = live_t
        if target == "portrait-flipped":     # the other way up: 90 <-> 270
            target, ptx = "portrait", ("90" if ptx == "270" else "270")
        if target == "landscape-flipped":    # upside down landscape
            target, transform = "landscape", "180"
        else:
            transform = ptx if target == "portrait" else "normal"
        res = {"ok": False, "orientation": target, "transform": transform, "outputs": dict(now["outputs"]),
               "applied": [], "saved": None, "error": None}
        errors = []
        if now["outputs"]:
            for name in now["outputs"]:
                try:
                    _randr("--output", name, "--transform", transform)
                    res["applied"].append(name)
                    res["outputs"][name] = transform
                except RandrError as e:
                    errors.append(f"{name}: {e}")
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{name}: {e}")
        else:
            errors.append(now["error"] or "no screen found")
        if persist_it:
            try:
                res["saved"] = persist(target, transform if (target == "portrait" or transform == "180") else None)
            except Exception as e:  # noqa: BLE001
                log.warning("rotate: couldn't save kiosk.env: %s", e)
                errors.append(f"couldn't save the choice: {e}")
        res["ok"] = bool(res["applied"]) and not errors
        if errors:
            res["error"] = "; ".join(errors)
        log.info("rotate: %s -> %s (%s) applied=%s saved=%s err=%s", orientation, target, transform,
                 res["applied"], res["saved"], res["error"])
        return res


set = set_orientation   # noqa: A001  (the module's public name: screen_rotate.set("portrait"))
