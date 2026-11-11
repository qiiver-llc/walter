"""Optional Home Assistant bridge: `walterctl ha ...` and a few fast-path voice commands.

Talks to an EXISTING Home Assistant over its REST API with a long-lived access
token (docs/HOME-ASSISTANT.md). Nothing here runs unless [homeassistant]
enabled = true, a url is set and the token file exists.

  token     ~/.walter/secrets/homeassistant.token (mode 600; never printed or logged)
  aliases   ~/.walter/ha-aliases.json  {"spoken name": "light.entity_id", ...}
  cache     ~/.walter/ha-cache.json    last entity list (names only), for the fast path

SAFE-LIST policy: lights, switches, fans, climate, scenes, covers and input
booleans may be driven freely. Locks, alarm panels, valves, and anything that
looks like a garage door, gate, door or lock (by device class, entity id or
name) need the person's spoken yes: the module refuses them unless called with
confirmed=True (`--confirmed`). Every other domain (scripts, automations,
buttons, sirens, ...) is refused unless confirmed too. The fast path never
touches anything that needs a yes; the brain asks first.

Names and states come from Home Assistant: they are treated as data (cleaned,
and wrapped in <untrusted> for the brain in `list`/`areas` output).
"""
from __future__ import annotations

import difflib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import config as config_mod
from . import security

# ------------------------------------------------------------ policy
SAFE_DOMAINS = {"light", "switch", "fan", "climate", "scene", "cover", "input_boolean"}
SENSITIVE_DOMAINS = {"lock", "alarm_control_panel", "valve"}
SENSITIVE_CLASSES = {"garage", "garage_door", "gate", "door", "lock", "opening", "window", "safety"}
SENSITIVE_WORDS = re.compile(r"\b(garage|gates?|doors?|locks?|unlock|alarm|security|safe|deadbolt|"
                             r"garagedoor|frontdoor|backdoor|doorlock|opener)\b", re.I)
READ_ONLY = {"sensor", "binary_sensor", "weather", "sun", "person", "zone", "device_tracker"}

COLOURS = {
    "red": [255, 0, 0], "orange": [255, 140, 0], "yellow": [255, 220, 0], "green": [0, 200, 0],
    "blue": [0, 80, 255], "purple": [150, 0, 255], "pink": [255, 105, 180], "white": [255, 255, 255],
    "warm white": [255, 200, 140], "cool white": [210, 230, 255], "cyan": [0, 255, 255],
    "teal": [0, 160, 150], "magenta": [255, 0, 255],
}


class HAError(Exception):
    """A plain sentence safe to say out loud."""


class NeedsConfirmation(HAError):
    pass


def _data_dir() -> Path:
    return Path(os.environ.get("WALTER_DATA", config_mod.DATA_DIR))


def token_path(data_dir: Path | None = None) -> Path:
    return (data_dir or _data_dir()) / "secrets" / "homeassistant.token"


def aliases_path(data_dir: Path | None = None) -> Path:
    return (data_dir or _data_dir()) / "ha-aliases.json"


def cache_path(data_dir: Path | None = None) -> Path:
    return (data_dir or _data_dir()) / "ha-cache.json"


def clean(s, limit: int = 80) -> str:
    return security.clean_title(str(s or ""), limit)


def norm(s: str) -> str:
    s = re.sub(r"[_.]", " ", str(s or "").lower())
    s = re.sub(r"[^a-z0-9 ]+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def needs_confirmation(entity_id: str, attrs: dict | None = None, service: str = "") -> bool:
    """True if driving this entity needs the person's spoken yes."""
    domain = entity_id.split(".", 1)[0]
    attrs = attrs or {}
    if domain in SENSITIVE_DOMAINS:
        return True
    if service in ("unlock", "open", "alarm_disarm"):
        return True
    if domain not in SAFE_DOMAINS:
        return True                              # scripts, automations, buttons, sirens...
    if domain == "scene":
        return False
    if str(attrs.get("device_class") or "").lower() in SENSITIVE_CLASSES:
        return True
    name = norm(f"{entity_id} {attrs.get('friendly_name') or ''}")
    return bool(SENSITIVE_WORDS.search(name))


# ------------------------------------------------------------ settings
@dataclass
class Settings:
    enabled: bool = False
    url: str = ""
    timeout_s: float = 6.0
    fastpath: bool = True


def settings(cfg=None) -> Settings:
    try:
        cfg = cfg or config_mod.load()
        h = cfg.homeassistant
        return Settings(bool(h.enabled), str(h.url or "").rstrip("/"), float(h.timeout_s), bool(h.fastpath))
    except Exception:
        return Settings()


def read_token(data_dir: Path | None = None) -> str:
    p = token_path(data_dir)
    try:
        return p.read_text().strip()
    except OSError:
        return ""


def save_token(token: str, data_dir: Path | None = None) -> Path:
    token = token.strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+", token) or not 40 <= len(token) <= 1000:
        raise HAError("That doesn't look like a Home Assistant long-lived access token.")
    p = token_path(data_dir)
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)
    return p


# ------------------------------------------------------------ client
class Client:
    def __init__(self, url: str, token: str, timeout: float = 6.0):
        if not url:
            raise HAError("Home Assistant's address isn't set. Run: walterctl ha set-url http://homeassistant.local:8123")
        if not re.match(r"https?://", url):
            raise HAError("Home Assistant's address should start with http:// or https://.")
        if not token:
            raise HAError("I don't have a Home Assistant token yet. Run: walterctl ha connect")
        self.url = url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self._states: list[dict] | None = None

    def __repr__(self):   # never show the token
        return f"Client({self.url!r})"

    def _req(self, method: str, path: str, body=None, raw: bool = False):
        data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": "Bearer " + self._token, "Content-Type": "application/json",
            "User-Agent": "walter-ha/1"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out = r.read(4_000_000)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise HAError("Home Assistant didn't accept my token (it may have been revoked).") from None
            if e.code == 404:
                raise HAError("Home Assistant doesn't know that.") from None
            if e.code == 400:
                raise HAError("Home Assistant refused that request.") from None
            raise HAError(f"Home Assistant answered with an error ({e.code}).") from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise HAError("I can't reach Home Assistant right now.") from None
        if raw:
            return out.decode("utf8", "replace")
        try:
            return json.loads(out or b"null")
        except ValueError:
            raise HAError("Home Assistant sent something I couldn't read.") from None

    def ping(self) -> bool:
        r = self._req("GET", "/api/")
        return isinstance(r, dict) and "message" in r

    def states(self, refresh: bool = False) -> list[dict]:
        if self._states is None or refresh:
            r = self._req("GET", "/api/states")
            self._states = [s for s in (r or []) if isinstance(s, dict) and "entity_id" in s]
        return self._states

    def state(self, entity_id: str) -> dict:
        return self._req("GET", "/api/states/" + urllib.parse.quote(entity_id, safe="._"))

    def areas(self) -> list[dict]:
        """[{id, name, entities}] via the template API (REST has no area registry)."""
        tpl = ("{% set ns = namespace(o=[]) %}{% for a in areas() %}{% set ns.o = ns.o + "
               "[{'id': a, 'name': area_name(a), 'entities': area_entities(a)}] %}{% endfor %}"
               "{{ ns.o | tojson }}")
        out = self._req("POST", "/api/template", {"template": tpl}, raw=True)
        try:
            r = json.loads(out)
        except ValueError:
            return []
        return [a for a in r if isinstance(a, dict)] if isinstance(r, list) else []

    def call(self, domain: str, service: str, data: dict, confirmed: bool = False, attrs: dict | None = None):
        if not re.fullmatch(r"[a-z_]+", domain) or not re.fullmatch(r"[a-z_]+", service):
            raise HAError("That isn't a valid Home Assistant service.")
        ids = data.get("entity_id")
        ids = [ids] if isinstance(ids, str) else list(ids or [])
        if not ids and domain != "scene":
            raise HAError("Which device? I need an entity.")
        for eid in ids:
            a = attrs if attrs is not None else self._attrs(eid)
            if needs_confirmation(eid, a, service) and not confirmed:
                raise NeedsConfirmation(
                    f"{friendly(eid, a)} needs a spoken yes first (locks, doors, gates and alarms). "
                    "Ask, then run it again with --confirmed.")
        if not ids and needs_confirmation(f"{domain}.x", {}, service) and not confirmed:
            raise NeedsConfirmation("That needs a spoken yes first; run it again with --confirmed.")
        return self._req("POST", f"/api/services/{domain}/{service}", data)

    def _attrs(self, eid: str) -> dict:
        for s in (self._states or []):
            if s.get("entity_id") == eid:
                return s.get("attributes") or {}
        try:
            return (self.state(eid) or {}).get("attributes") or {}
        except HAError:
            return {}


def client(data_dir: Path | None = None, cfg=None) -> Client:
    st = settings(cfg)
    return Client(st.url, read_token(data_dir), st.timeout_s)


def friendly(eid: str, attrs: dict | None = None) -> str:
    n = clean((attrs or {}).get("friendly_name") or "", 60)
    return n or eid.split(".", 1)[-1].replace("_", " ")


# ------------------------------------------------------------ aliases, cache, matching
def load_aliases(data_dir: Path | None = None) -> dict[str, str]:
    try:
        d = json.loads(aliases_path(data_dir).read_text())
    except (OSError, ValueError):
        return {}
    return {norm(k): str(v) for k, v in d.items() if isinstance(v, str) and "." in v} if isinstance(d, dict) else {}


def save_aliases(d: dict, data_dir: Path | None = None):
    p = aliases_path(data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(d, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, p)


def write_cache(states: list[dict], data_dir: Path | None = None):
    rows = []
    for s in states:
        eid = s.get("entity_id", "")
        a = s.get("attributes") or {}
        rows.append({"id": eid, "name": clean(a.get("friendly_name") or "", 60),
                     "class": str(a.get("device_class") or "")[:30]})
    p = cache_path(data_dir)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"at": time.time(), "entities": rows}))
        os.replace(tmp, p)
    except OSError:
        pass


def read_cache(data_dir: Path | None = None) -> list[dict]:
    try:
        return json.loads(cache_path(data_dir).read_text()).get("entities") or []
    except (OSError, ValueError, AttributeError):
        return []


DOMAIN_WORDS = {"light": ("light", "lights", "lamp", "lamps"), "switch": ("switch", "plug", "outlet"),
                "fan": ("fan", "fans"), "climate": ("thermostat", "heating", "heat", "ac", "air conditioning"),
                "cover": ("blinds", "blind", "shades", "shade", "curtains", "curtain"), "scene": ("scene",)}
FILLER = re.compile(r"^(?:the|my|our|all the|all)\s+", re.I)


def resolve(spoken: str, entities: list[dict], aliases: dict[str, str] | None = None,
            domains: set[str] | None = None) -> str | None:
    """Entity id for a spoken name ("the kitchen lights"), or None if unclear.
    entities: [{"id", "name"}]. Aliases win. Ambiguous matches return None."""
    said = norm(FILLER.sub("", spoken.strip()))
    if not said:
        return None
    aliases = aliases or {}
    if said in aliases:
        return aliases[said]
    if said.startswith("the ") and said[4:] in aliases:
        return aliases[said[4:]]
    ents = [e for e in entities if e.get("id") and (not domains or e["id"].split(".", 1)[0] in domains)]
    if re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", spoken.strip()):
        return spoken.strip() if any(e["id"] == spoken.strip() for e in ents) else None
    # domain hint words ("lights") narrow the search and are dropped from the name
    words = said.split()
    hinted = {d for d, ws in DOMAIN_WORDS.items() if words and words[-1] in ws}
    core = " ".join(words[:-1]) if hinted and len(words) > 1 else said
    pool = [e for e in ents if e["id"].split(".", 1)[0] in hinted] if hinted else ents
    if hinted and not pool:
        pool = ents

    def names(e):
        obj = norm(e["id"].split(".", 1)[1])
        n = norm(e.get("name") or "")
        out = {obj, n}
        for x in list(out):   # "kitchen light" also answers to "kitchen"
            ws = x.split()
            if ws and any(ws[-1] in v for v in DOMAIN_WORDS.values()):
                out.add(" ".join(ws[:-1]))
        return {x for x in out if x}

    for target in (said, core):
        exact = [e for e in pool if target in names(e)]
        if len(exact) == 1:
            return exact[0]["id"]
        if len(exact) > 1:
            # prefer a light group like light.kitchen over light.kitchen_2... no: ambiguous
            return None
    scored = []
    for e in pool:
        best = max(difflib.SequenceMatcher(None, core, n).ratio() for n in names(e))
        scored.append((best, e["id"]))
    scored.sort(reverse=True)
    if scored and scored[0][0] >= 0.84 and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.08):
        return scored[0][1]
    return None


def resolve_live(c: Client, spoken: str, domains: set[str] | None = None, data_dir: Path | None = None) -> str:
    states = c.states()
    write_cache(states, data_dir)
    ents = [{"id": s["entity_id"], "name": (s.get("attributes") or {}).get("friendly_name") or ""} for s in states]
    eid = resolve(spoken, ents, load_aliases(data_dir), domains)
    if not eid:
        raise HAError(f"I couldn't tell which device \"{clean(spoken, 40)}\" is. "
                      "Try its exact name, or add an alias: walterctl ha alias add \"name\" light.entity_id")
    return eid


# ------------------------------------------------------------ actions (return a spoken sentence)
def _state_of(c: Client, eid: str) -> dict:
    for s in c.states():
        if s.get("entity_id") == eid:
            return s
    return c.state(eid) or {}


def turn(c: Client, onoff: str, spoken: str, confirmed: bool = False, data_dir=None) -> str:
    eid = resolve_live(c, spoken, data_dir=data_dir)
    domain = eid.split(".", 1)[0]
    st = _state_of(c, eid)
    a = st.get("attributes") or {}
    if domain in READ_ONLY:
        raise HAError(f"{friendly(eid, a)} is a sensor; I can only read it.")
    if domain == "scene":
        if onoff != "on":
            raise HAError("Scenes can only be turned on.")
        c.call("scene", "turn_on", {"entity_id": eid}, confirmed, a)
        return f"{friendly(eid, a)} scene on."
    if domain == "cover":
        svc = "open_cover" if onoff == "on" else "close_cover"
        c.call("cover", svc, {"entity_id": eid}, confirmed, a)
        return f"{'Opening' if onoff == 'on' else 'Closing'} {friendly(eid, a)}."
    if domain == "lock":
        c.call("lock", "unlock" if onoff == "off" else "lock", {"entity_id": eid}, confirmed, a)
        return f"{friendly(eid, a)} {'unlocked' if onoff == 'off' else 'locked'}."
    svc_domain = domain if domain in SAFE_DOMAINS | SENSITIVE_DOMAINS else "homeassistant"
    c.call(svc_domain, f"turn_{onoff}", {"entity_id": eid}, confirmed, a)
    return f"{friendly(eid, a)} {onoff}."


def brightness(c: Client, spoken: str, pct: int, data_dir=None) -> str:
    if not 0 <= pct <= 100:
        raise HAError("Brightness goes from 0 to 100 percent.")
    eid = resolve_live(c, spoken, {"light", "fan", "cover"}, data_dir)
    domain = eid.split(".", 1)[0]
    a = _state_of(c, eid).get("attributes") or {}
    if domain == "light":
        c.call("light", "turn_on", {"entity_id": eid, "brightness_pct": pct}, attrs=a)
    elif domain == "fan":
        c.call("fan", "set_percentage", {"entity_id": eid, "percentage": pct}, attrs=a)
    else:
        c.call("cover", "set_cover_position", {"entity_id": eid, "position": pct}, attrs=a)
    return f"{friendly(eid, a)} at {pct} percent."


def colour(c: Client, spoken: str, col: str, data_dir=None) -> str:
    col = col.strip().lower()
    if col in COLOURS:
        rgb = COLOURS[col]
    elif re.fullmatch(r"#?[0-9a-f]{6}", col):
        h = col.lstrip("#")
        rgb = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    else:
        raise HAError(f"I don't know the colour {clean(col, 20)}.")
    eid = resolve_live(c, spoken, {"light"}, data_dir)
    a = _state_of(c, eid).get("attributes") or {}
    c.call("light", "turn_on", {"entity_id": eid, "rgb_color": rgb}, attrs=a)
    return f"{friendly(eid, a)} set to {col}."


def set_temperature(c: Client, spoken: str, temp: float, data_dir=None) -> str:
    if not 40 <= temp <= 95 and not 5 <= temp <= 35:
        raise HAError("That temperature looks wrong.")
    eid = resolve_live(c, spoken or "thermostat", {"climate"}, data_dir)
    a = _state_of(c, eid).get("attributes") or {}
    lo, hi = a.get("min_temp"), a.get("max_temp")
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and not lo <= temp <= hi:
        raise HAError(f"{friendly(eid, a)} only goes from {lo:g} to {hi:g}.")
    c.call("climate", "set_temperature", {"entity_id": eid, "temperature": temp}, attrs=a)
    return f"{friendly(eid, a)} set to {temp:g} degrees."


def temperature(c: Client, spoken: str = "", data_dir=None) -> str:
    """Indoor temperature: a named thermostat/sensor, else the first thermostat."""
    states = c.states()
    write_cache(states, data_dir)
    if spoken:
        eid = resolve_live(c, spoken, {"climate", "sensor"}, data_dir)
        cand = [s for s in states if s["entity_id"] == eid]
    else:
        cand = [s for s in states if s["entity_id"].startswith("climate.")]
        if not cand:
            cand = [s for s in states if s["entity_id"].startswith("sensor.")
                    and (s.get("attributes") or {}).get("device_class") == "temperature"]
    for s in cand:
        a = s.get("attributes") or {}
        unit = "degrees"
        if s["entity_id"].startswith("climate."):
            cur, target = a.get("current_temperature"), a.get("temperature")
            if isinstance(cur, (int, float)):
                extra = f", set to {target:g}" if isinstance(target, (int, float)) else ""
                return f"It's {cur:g} {unit} by the {friendly(s['entity_id'], a)}{extra}."
        else:
            try:
                v = float(s.get("state"))
            except (TypeError, ValueError):
                continue
            return f"{friendly(s['entity_id'], a)} says {v:g} {unit}."
    raise HAError("I couldn't find a temperature in Home Assistant.")


def describe_state(c: Client, spoken: str, data_dir=None) -> str:
    eid = resolve_live(c, spoken, data_dir=data_dir)
    s = _state_of(c, eid)
    a = s.get("attributes") or {}
    st = clean(s.get("state"), 40)
    extra = ""
    if eid.startswith("light.") and st == "on" and isinstance(a.get("brightness"), (int, float)):
        extra = f" at {round(a['brightness'] / 2.55)} percent"
    unit = clean(a.get("unit_of_measurement") or "", 10)
    return f"{friendly(eid, a)} is {st}{(' ' + unit) if unit else ''}{extra}."


def scene(c: Client, spoken: str, data_dir=None) -> str:
    eid = resolve_live(c, spoken, {"scene"}, data_dir)
    a = _state_of(c, eid).get("attributes") or {}
    c.call("scene", "turn_on", {"entity_id": eid}, attrs=a)
    return f"{friendly(eid, a)} scene on."


def cover(c: Client, action: str, spoken: str, confirmed: bool = False, data_dir=None) -> str:
    svc = {"open": "open_cover", "close": "close_cover", "stop": "stop_cover"}.get(action)
    if not svc:
        raise HAError("Covers can open, close or stop.")
    eid = resolve_live(c, spoken, {"cover"}, data_dir)
    a = _state_of(c, eid).get("attributes") or {}
    c.call("cover", svc, {"entity_id": eid}, confirmed, a)
    return {"open": "Opening", "close": "Closing", "stop": "Stopping"}[action] + f" {friendly(eid, a)}."


# ------------------------------------------------------------ fast path (voice)
HINT = re.compile(r"\b(turn|switch|set|dim|brighten|what's|what is|whats|how warm|how cold)\b", re.I)


def fast_match(text: str, wake_re=None, enabled: bool | None = None, data_dir: Path | None = None):
    """A MediaCmd (walterctl args + spoken confirmation) for a clear whole-utterance
    home command whose device is known from the cache, else None (the brain decides).
    Never matches anything that needs a spoken yes."""
    from . import mediacmd
    if enabled is None:
        st = settings()
        enabled = st.enabled and st.fastpath and bool(st.url)
    if not enabled or "\n" in text or len(text) > 120:
        return None
    if not token_path(data_dir).exists():
        return None
    t = mediacmd._clean(text, wake_re)
    if not t or re.search(r"\b(and|then|but|unless|if|when|after|before|in \d+|at \d+|tomorrow|tonight)\b", t, re.I):
        return None
    low = t.lower()

    def known(spoken, domains=None):
        ents = read_cache(data_dir)
        eid = resolve(spoken, ents, load_aliases(data_dir), domains)
        if not eid:
            return None
        attrs = next(({"friendly_name": e.get("name"), "device_class": e.get("class")}
                      for e in ents if e.get("id") == eid), {})
        if eid.split(".", 1)[0] in READ_ONLY or needs_confirmation(eid, attrs):
            return None
        return eid

    def cmd(argv, note):
        return mediacmd.MediaCmd(["ha", *argv], "", wait=True, note=note)

    m = (re.fullmatch(r"(?:turn|switch)\s+(on|off)\s+(.{2,60})", low)
         or re.fullmatch(r"(?:turn|switch)\s+(.{2,60}?)\s+(on|off)", low))
    if m:
        a, b = m.group(1), m.group(2)
        onoff, name = (a, b) if a in ("on", "off") else (b, a)
        eid = known(name)
        if eid and not eid.startswith("scene.") and not eid.startswith("cover."):
            return cmd(["turn", onoff, eid], f"Home Assistant: turned {onoff} {eid}")
        return None
    m = re.fullmatch(r"(?:set|dim|brighten|turn)\s+(.{2,60}?)\s+(?:to|at|up to|down to)\s+(\d{1,3})\s*(?:percent|%)", low)
    if m:
        pct = int(m.group(2))
        eid = known(m.group(1), {"light", "fan", "cover"})
        if eid and pct <= 100:
            return cmd(["brightness", eid, str(pct)], f"Home Assistant: set {eid} to {pct}%")
        return None
    m = re.fullmatch(r"(?:set|turn)\s+(?:the\s+)?(?:(.{2,40}?)\s+)?(?:thermostat|heat|heating|ac|temperature)"
                     r"\s+(?:to|at)\s+(\d{2})(?:\s*degrees?)?", low)
    if m:
        name = (m.group(1) or "").strip()
        eid = known((name + " thermostat").strip(), {"climate"}) or (known(name, {"climate"}) if name else None)
        if not eid:
            climates = [e["id"] for e in read_cache(data_dir) if e.get("id", "").startswith("climate.")]
            eid = climates[0] if len(climates) == 1 and not name else None
        if eid:
            return cmd(["temp", eid, m.group(2)], f"Home Assistant: set {eid} to {m.group(2)} degrees")
        return None
    if re.fullmatch(r"(?:what's|what is|whats)\s+the\s+(?:temperature|temp)\s+(?:inside|in here|in the house|"
                    r"indoors|in the home)|(?:what's|what is|whats)\s+the\s+thermostat(?:\s+(?:set to|at|reading|say))?|"
                    r"how (?:warm|cold) is it (?:inside|in here|in the house)", low):
        if any(e.get("id", "").startswith("climate.") for e in read_cache(data_dir)):
            return cmd(["temperature"], "Home Assistant: told them the indoor temperature")
    return None


# ------------------------------------------------------------ CLI
HELP = """walterctl ha: Home Assistant bridge (docs/HOME-ASSISTANT.md)
  status                         enabled? address? token present? reachable?
  connect [URL]                  phone page (QR) to paste a long-lived token privately
  set-url URL                    e.g. http://homeassistant.local:8123
  token --stdin                  save a token piped on stdin (never printed)
  enable | disable
  areas                          areas and their devices
  list [DOMAIN|WORDS]            devices (light, switch, climate, ...)
  state NAME                     one device's state
  temperature [NAME]             indoor temperature
  on NAME | off NAME             (also: turn on|off NAME)
  brightness NAME PCT            lights (fans: speed, covers: position)
  colour NAME red|#ff8800
  temp NAME DEGREES              thermostat set temperature
  scene NAME
  cover open|close|stop NAME
  call DOMAIN SERVICE ENTITY [--data JSON]
  alias add "spoken name" ENTITY_ID | alias rm "spoken name" | aliases
  refresh                        refresh the device cache for the voice fast path
Locks, garage doors, gates, doors and alarm panels need the person's spoken yes
first: add --confirmed only after hearing it."""


def _set_cfg(key: str, value: str) -> str:
    from . import live
    data = _data_dir()
    live.set_in_file(data / "config.toml", f"homeassistant.{key}", value)
    try:   # tell a running Walter (fine if it isn't running)
        import subprocess
        exe = Path(__file__).resolve().parents[1] / "bin" / "walterctl"
        subprocess.run([sys.executable, str(exe), "config", "reload"], capture_output=True, timeout=10)
    except Exception:
        pass
    return f"homeassistant.{key} = {value}"


def cli(argv: list[str], out=print, data_dir: Path | None = None, cfg=None) -> int:
    args = list(argv)
    confirmed = "--confirmed" in args
    args = [a for a in args if a != "--confirmed"]
    if not args or args[0] in ("help", "-h", "--help"):
        out(HELP)
        return 0
    cmd, rest = args[0], args[1:]
    try:
        if cmd == "status":
            st = settings(cfg)
            tok = token_path(data_dir)
            out(f"enabled: {'yes' if st.enabled else 'no'}")
            out(f"url: {st.url or '(not set)'}")
            out(f"token: {'present' if tok.exists() else 'missing'} ({tok})")
            if st.url and tok.exists():
                try:
                    out("reachable: " + ("yes" if client(data_dir, cfg).ping() else "no"))
                except HAError as e:
                    out(f"reachable: no ({e})")
            out(f"aliases: {len(load_aliases(data_dir))}, cached devices: {len(read_cache(data_dir))}")
            return 0
        if cmd == "set-url":
            if not rest or not re.match(r"https?://[^\s/]+", rest[0]):
                raise HAError("usage: walterctl ha set-url http://homeassistant.local:8123")
            out(_set_cfg("url", rest[0].rstrip("/")))
            return 0
        if cmd in ("enable", "disable"):
            out(_set_cfg("enabled", "true" if cmd == "enable" else "false"))
            return 0
        if cmd == "token":
            if rest[:1] != ["--stdin"]:
                raise HAError("usage: walterctl ha token --stdin   (never type a token on the command line)")
            p = save_token(sys.stdin.read(), data_dir)
            out(f"token saved ({p}, mode 600)")
            return 0
        if cmd == "connect":
            if rest:
                out(_set_cfg("url", rest[0].rstrip("/")))
            drop = (_data_dir() / "home" / "skills" / "api-key-drop.py")
            if not drop.exists():
                raise HAError("The private key page isn't installed here; use: walterctl ha token --stdin")
            import subprocess
            subprocess.Popen([sys.executable, str(drop), "homeassistant"], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            out("QR code for the token page is on the wall (30 minutes).")
            return 0
        if cmd == "alias":
            al = {}
            try:
                al = json.loads(aliases_path(data_dir).read_text())
            except (OSError, ValueError):
                pass
            if rest[:1] == ["add"] and len(rest) == 3 and re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", rest[2]):
                al[norm(rest[1])] = rest[2]
                save_aliases(al, data_dir)
                out(f'alias "{norm(rest[1])}" -> {rest[2]}')
                return 0
            if rest[:1] in (["rm"], ["remove"]) and len(rest) == 2:
                al.pop(norm(rest[1]), None)
                save_aliases(al, data_dir)
                out(f'alias "{norm(rest[1])}" removed')
                return 0
            raise HAError('usage: walterctl ha alias add "spoken name" light.entity_id | alias rm "spoken name"')
        if cmd == "aliases":
            for k, v in sorted(load_aliases(data_dir).items()):
                out(f"{k} -> {v}")
            return 0

        st = settings(cfg)
        if not st.enabled:
            raise HAError("Home Assistant is switched off here. Turn it on with: walterctl ha enable")
        c = client(data_dir, cfg)
        name = " ".join(rest)
        if cmd == "refresh":
            write_cache(c.states(), data_dir)
            out(f"{len(read_cache(data_dir))} devices cached")
        elif cmd == "list":
            states = c.states()
            write_cache(states, data_dir)
            want = norm(name)
            lines = []
            for s in sorted(states, key=lambda s: s["entity_id"]):
                eid, a = s["entity_id"], s.get("attributes") or {}
                if want and eid.split(".", 1)[0] != want and want not in norm(eid + " " + str(a.get("friendly_name") or "")):
                    continue
                flag = "  [needs yes]" if needs_confirmation(eid, a) and eid.split(".", 1)[0] not in READ_ONLY else ""
                lines.append(f"{eid}  {friendly(eid, a)}: {clean(s.get('state'), 30)}{flag}")
            out(security.wrap("\n".join(lines) or "(nothing)", "home assistant", 20000))
        elif cmd == "areas":
            lines = [f"{clean(a.get('name'), 40)}: {', '.join(clean(e, 60) for e in (a.get('entities') or [])[:40])}"
                     for a in c.areas()]
            out(security.wrap("\n".join(lines) or "(no areas)", "home assistant", 20000))
        elif cmd == "state":
            out(describe_state(c, name, data_dir))
        elif cmd == "temperature":
            out(temperature(c, name, data_dir))
        elif cmd in ("on", "off"):
            out(turn(c, cmd, name, confirmed, data_dir))
        elif cmd == "turn" and rest and rest[0] in ("on", "off"):
            out(turn(c, rest[0], " ".join(rest[1:]), confirmed, data_dir))
        elif cmd == "brightness" and len(rest) >= 2:
            out(brightness(c, " ".join(rest[:-1]), int(rest[-1].rstrip("%")), data_dir))
        elif cmd in ("colour", "color") and len(rest) >= 2:
            out(colour(c, " ".join(rest[:-1]), rest[-1], data_dir))
        elif cmd == "temp" and len(rest) >= 1:
            out(set_temperature(c, " ".join(rest[:-1]), float(rest[-1]), data_dir))
        elif cmd == "scene":
            out(scene(c, name, data_dir))
        elif cmd == "cover" and len(rest) >= 2:
            out(cover(c, rest[0], " ".join(rest[1:]), confirmed, data_dir))
        elif cmd == "call" and len(rest) >= 3:
            data = {}
            if "--data" in rest:
                i = rest.index("--data")
                data = json.loads(rest[i + 1])
                rest = rest[:i] + rest[i + 2:]
            domain, service, eid = rest[0], rest[1], rest[2]
            c.states()
            c.call(domain, service, {**data, "entity_id": eid}, confirmed)
            out(f"Called {domain}.{service} on {eid}.")
        else:
            out(HELP)
            return 2
        return 0
    except NeedsConfirmation as e:
        out(str(e))
        return 3
    except HAError as e:
        out(str(e))
        return 1
    except (ValueError, IndexError):
        out("I didn't understand those numbers.")
        return 2
