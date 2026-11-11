"""Live config: re-read ~/.walter/config.toml and apply what's safe without a restart.

  walterctl config reload            re-read the file, apply the hot keys
  walterctl config get [voice[.pitch]]  what's live right now
  walterctl config set speech.tts_pitch 0.9   edit the file (comments kept) + reload

GET /api/config, POST /api/config/reload, POST /api/config/set {"key", "value"}.

Hot sections: [voice] [speech] [ears] [life] [senses] [dashboard] [app], except
the keys in RESTART below (devices, models that are wired in at boot, what the
web server listens on), plus single HOT_KEYS in other sections ([brain] max_tasks:
tasks.py reads it at every spawn). The rest of [server] and [brain] needs a restart.
[speech] tts_provider / stt_provider / mode are only starting defaults: the live
switches are "walterctl voice|ears|mode" (remembered in speech.json, untouched here).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import tomllib
from dataclasses import fields, is_dataclass
from pathlib import Path

from . import config as config_mod

log = logging.getLogger(__name__)

HOT_SECTIONS = ("voice", "speech", "ears", "life", "senses", "dashboard", "app", "homeassistant")
# Single live keys in otherwise restart-only sections.
HOT_KEYS = {"brain": ("max_tasks",)}
# Integer limits checked by set_in_file (the CLI and the app both go through it).
LIMITS = {("brain", "max_tasks"): (1, 8)}

# Keys in hot sections that still need a restart, and why (shown to the person).
RESTART = {
    "voice": {"enabled": "audio output is opened at start", "device": "audio device is opened at start",
              "aec_device": "audio device is opened at start"},
    "ears": {"enabled": "the microphone is opened at start", "device": "the microphone is opened at start",
             "aec_device": "the microphone is opened at start", "barge_in": "echo canceller is set up at start"},
    "speech": {"key_file": "read at start", "realtime_model": "realtime sessions are set up at start",
               "realtime_url": "realtime sessions are set up at start"},
    "senses": {"recognise_faces": "enrolled people are loaded at start",
               "recognise_voices": "enrolled people are loaded at start",
               "camera_device": "camera is set up at start", "width": "camera is set up at start",
               "height": "camera is set up at start", "fps": "camera is set up at start"},
    "dashboard": {"phone_page": "the web server's LAN listening is set at start",
                  "list_title": "the list is opened at start"},
    "app": {"enabled": "the app routes are set up at start",
            "allow_lan": "the web server's LAN listening is set at start"},
}
# Live switches that win over the file: changing them here does nothing until a
# restart *without* speech.json; point at the right command instead.
SWITCHES = {("speech", "tts_provider"): "walterctl voice local|openai",
            ("speech", "stt_provider"): "walterctl ears local|openai",
            ("speech", "mode"): "walterctl mode classic|realtime"}


def as_dict(obj) -> dict:
    out = {}
    for f in fields(obj):
        v = getattr(obj, f.name)
        out[f.name] = as_dict(v) if is_dataclass(v) else v
    return out


def key_status(section: str, key: str) -> str:
    """'hot' | 'restart' | 'switch'"""
    if (section, key) in SWITCHES:
        return "switch"
    if section in HOT_SECTIONS and key not in RESTART.get(section, {}):
        return "hot"
    if key in HOT_KEYS.get(section, ()):
        return "hot"
    return "restart"


def describe() -> dict:
    """Which keys are hot-applied vs need a restart (for 'walterctl config' output)."""
    c = config_mod.Config()
    hot, restart = {}, {}
    for f in fields(c):
        for k in as_dict(getattr(c, f.name)):
            st = key_status(f.name, k)
            (hot if st == "hot" else restart).setdefault(f.name, []).append(k)
    return {"hot": hot, "restart": restart,
            "switches": {f"{s}.{k}": v for (s, k), v in SWITCHES.items()}}


# ------------------------------------------------------------ editing the file
def parse_value(raw):
    """A TOML value ("0.93", "true", "[1, 2]", '"echo"'), else a plain string."""
    if not isinstance(raw, str):
        return raw
    try:
        return tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        return raw


def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    raise ValueError(f"can't write {type(v).__name__} values")


def set_in_text(text: str, section: str, key: str, value) -> str:
    """Set section.key in TOML text, keeping comments and layout. Replaces the
    key's line (and its trailing comment) or adds it to the section (or a new
    section at the end). Single-line values only."""
    line = f"{key} = {toml_value(value)}"
    lines = text.splitlines()
    head = re.compile(r"^\s*\[([^\[\]]+)\]\s*(#.*)?$")
    keyre = re.compile(rf"^(\s*){re.escape(key)}\s*=\s*(.*)$")
    start = end = None
    for i, ln in enumerate(lines):
        m = head.match(ln)
        if m:
            if start is not None:
                end = i
                break
            if m.group(1).strip() == section:
                start = i
    if start is None:
        out = lines + ([""] if lines and lines[-1].strip() else []) + [f"[{section}]", line]
        return "\n".join(out) + "\n"
    end = len(lines) if end is None else end
    for i in range(start + 1, end):
        m = keyre.match(lines[i])
        if m:
            rest = m.group(2)
            comment = ""
            cm = re.search(r"\s+#.*$", rest)
            if cm and not rest.lstrip().startswith(("[", "\"\"\"", "'''")):
                comment = cm.group(0)
            lines[i] = m.group(1) + line + comment
            return "\n".join(lines) + "\n"
    ins = end
    while ins > start + 1 and not lines[ins - 1].strip():
        ins -= 1
    lines.insert(ins, line)
    return "\n".join(lines) + "\n"


def set_in_file(path: Path, dotted: str, raw) -> tuple[str, str, object]:
    """Edit section.key in the config file (validated by loading the result first)."""
    if "." not in dotted:
        raise ValueError("use section.key, e.g. speech.tts_pitch")
    section, key = dotted.split(".", 1)
    key = config_mod.ALIASES.get(key, key)
    cur = config_mod.Config()
    if not hasattr(cur, section) or not is_dataclass(getattr(cur, section)):
        raise ValueError(f"unknown section: {section}")
    if key not in as_dict(getattr(cur, section)):
        raise ValueError(f"unknown key: {section}.{key}")
    default = getattr(getattr(cur, section), key)
    value = parse_value(raw)
    if isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool):
        value = float(value)
    if type(default) is not type(value) and not (isinstance(default, list) and isinstance(value, list)):
        if isinstance(default, str):
            value = str(raw)
        else:
            raise ValueError(f"{dotted} wants a {type(default).__name__}, got {raw!r}")
    lim = LIMITS.get((section, key))
    if lim and not (lim[0] <= value <= lim[1]):
        raise ValueError(f"{section}.{key} must be {lim[0]} to {lim[1]}, got {value!r}")
    text = path.read_text() if path.exists() else ""
    new = set_in_text(text, section, key, value)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(new)
    try:
        config_mod.load(tmp)            # never leave a file Walter can't start with
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    return section, key, value


# ------------------------------------------------------------ applying it
class Live:
    """Holds the running Config and the file it came from; applies reloads."""

    def __init__(self, cfg, path=None, conductor=None, send=None):
        self.cfg = cfg
        self.path = Path(path) if path else config_mod.DATA_DIR / "config.toml"
        self.conductor = conductor     # reaches voice, ears, presence, speech
        self.send = send               # face hub (captions / live partials flags)
        # What the file said at start: compared against, not the live cfg (aec.py and
        # command-line flags change some live values, e.g. the audio devices).
        self.base = config_mod.load(self.path) if self.path.exists() else config_mod.Config()
        self.pending_restart: dict[str, str] = {}
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        conf = {s: as_dict(getattr(self.cfg, s)) for s in HOT_SECTIONS}
        for s, keys in HOT_KEYS.items():   # only the live keys of restart-only sections
            conf[s] = {k: getattr(getattr(self.cfg, s), k) for k in keys}
        return {"config": conf,
                "path": str(self.path), "pending_restart": dict(self.pending_restart), **describe()}

    def reload(self) -> dict:
        """Re-read the file; hot-apply what changed. Never raises for a bad file:
        returns {"ok": False, "error": ...} and keeps everything as it was."""
        with self._lock:
            try:
                new = config_mod.load(self.path)
            except Exception as e:
                log.warning("config reload failed: %s", e)
                return {"ok": False, "error": str(e)}
            applied, restart, switches = [], [], []
            changed: dict[str, set] = {}
            for f in fields(new):
                sec = f.name
                old_d, new_d = as_dict(getattr(self.base, sec)), as_dict(getattr(new, sec))
                for k, v in new_d.items():
                    if old_d.get(k) == v:
                        continue
                    dotted = f"{sec}.{k}"
                    st = key_status(sec, k)
                    if st == "hot":
                        setattr(getattr(self.cfg, sec), k, v)
                        setattr(getattr(self.base, sec), k, v)
                        changed.setdefault(sec, set()).add(k)
                        applied.append(dotted)
                    elif st == "switch":
                        switches.append(f"{dotted} (live switch: {SWITCHES[(sec, k)]})")
                    else:
                        why = RESTART.get(sec, {}).get(k, f"[{sec}] is read at start")
                        self.pending_restart[dotted] = why
                        restart.append(f"{dotted} ({why})")
            # a restart-only key changed back to its start value: nothing pending any more
            for dotted in list(self.pending_restart):
                sec, k = dotted.split(".", 1)
                if as_dict(getattr(new, sec)).get(k) == as_dict(getattr(self.base, sec)).get(k):
                    del self.pending_restart[dotted]
            notes = self._hooks(changed)
        if applied or restart:
            log.info("config reload: applied %s%s", ", ".join(applied) or "nothing",
                     f"; needs restart: {', '.join(restart)}" if restart else "")
        return {"ok": True, "applied": applied, "needs_restart": restart, "switches": switches,
                "notes": notes, "pending_restart": dict(self.pending_restart)}

    def set(self, dotted: str, raw) -> dict:
        section, key, value = set_in_file(self.path, dotted, raw)
        r = self.reload()
        r["set"] = {f"{section}.{key}": value}
        return r

    # Components that copied a setting at start get told here.
    def _hooks(self, changed: dict[str, set]) -> list[str]:
        notes = []
        c = self.conductor

        def note(fn, what):
            try:
                n = fn()
                if n:
                    notes.append(n)
            except Exception as e:
                log.warning("config reload: %s failed: %s", what, e)
                notes.append(f"{what} failed: {e}")

        ears_k = changed.get("ears", set())
        if ears_k and c is not None:
            if "wake_words" in ears_k and hasattr(c, "wake_re"):
                note(lambda: setattr(c, "wake_re", re.compile(
                    r"\b(" + "|".join(re.escape(w) for w in self.cfg.ears.wake_words) + r")\b", re.I)),
                    "wake words")
            ears = getattr(c, "ears", None)
            if ears is not None and hasattr(ears, "reconfigure"):
                note(lambda: ears.reconfigure(ears_k), "ears")
        voice = getattr(c, "voice", None) if c is not None else None
        local_keys = {"piper_model", "engine", "kokoro_dir", "kokoro_voice", "kokoro_speed",
                      "kokoro_threads"}
        if local_keys & changed.get("voice", set()) and voice is not None:
            note(lambda: self._reload_piper(voice), "piper voice")
        senses = changed.get("senses", set())
        pres = getattr(c, "presence", None) if c is not None else None
        if senses and pres is not None:
            note(lambda: self._presence(pres, senses), "camera")
        if self.send is not None and ({"captions"} & changed.get("life", set())
                                      or {"live_partials"} & ears_k):
            note(lambda: self.send({"t": "config", "captions": self.cfg.life.captions,
                                    "live": self.cfg.ears.live_partials}), "face")
        return notes

    def _reload_piper(self, voice) -> str:
        from .voice import Synth

        def run():
            s = Synth(self.cfg)
            voice.synth = s
            log.info("piper voice reloaded (%s)", s.kind)
        threading.Thread(target=run, name="piper-reload", daemon=True).start()
        return "loading the new local voice in the background"

    def _presence(self, pres, keys: set) -> str:
        s = self.cfg.senses
        t, g = getattr(pres, "tracker", None), getattr(pres, "gate", None)
        if t is not None:
            t.arrive_after_s = s.arrive_after_min * 60
            t.empty_after_s = s.empty_after_min * 60
            t.greet_cooldown_s = s.greet_cooldown_min * 60
        if g is not None:
            g.sleep_after_s = s.sleep_after_min * 60
            g.force_sleep_after_s = max(getattr(g, "force_sleep_after_s", 0), g.sleep_after_s)
            g.verify_s = s.verify_s
            g.wake_ratio = s.wake_ratio
            g.cooldown_s = s.wake_cooldown_s
            if hasattr(g, "sleep_enabled"):
                g.sleep_enabled = bool(s.camera_sleep)
        if "debug" in keys and hasattr(pres, "set_debug"):
            pres.set_debug(s.debug)
        if "camera" in keys and hasattr(pres, "set_enabled"):
            pres.set_enabled(bool(s.camera))
            return f"camera {'on' if s.camera else 'off'}"
        return ""


def add_routes(routes, conductor):
    """/api/config routes on the face server (kiosk/localhost only, like the rest of /api)."""
    from aiohttp import web

    def live():
        lv = getattr(conductor, "live", None)
        if lv is None:
            raise web.HTTPServiceUnavailable(text="live config not ready")
        return lv

    @routes.get("/api/config")
    async def api_config(_):
        return web.json_response(live().snapshot())

    @routes.post("/api/config/reload")
    async def api_config_reload(_):
        return web.json_response(live().reload())

    @routes.post("/api/config/set")
    async def api_config_set(request):
        try:
            d = await request.json()
        except Exception:
            d = {}
        if not d.get("key") or "value" not in d:
            raise web.HTTPBadRequest(text="key and value required")
        try:
            return web.json_response(live().set(str(d["key"]), d["value"]))
        except (ValueError, OSError, tomllib.TOMLDecodeError) as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
