"""An add-on's look: the colour, shape and icon of its cloud-worker satellites, and its favicon.

addon.toml (docs/ADDONS.md):

    favicon = "assets/favicon.png"   # optional, any add-on: PNG/ICO <= 32 KB or SVG <= 8 KB

    [workers.look]                   # optional, inside [workers]
    color = "#c8923a"                # #rrggbb
    shape = "hex"                    # diamond | ring | square | triangle | hex | star | drop
    icon = "assets/icon.svg"         # small SVG inside the add-on (<= 8 KB)
    favicon = "assets/favicon.png"   # same as the top-level favicon (either place)

Everything an add-on gives is untrusted. SVGs are parsed and REBUILT from an allowlist of elements and
attributes (no script, no event handlers, no style, no external or data references, no DOCTYPE/entities);
validation rejects a file that needed anything stripped. The face and the app only ever get the rebuilt
file, inlined as a data: URI (no file route needed).

Standard library only. Kept identical in both repos: walter/addonlook.py (Walter) and tools/look.py (catalog).
"""
from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

SHAPES = ("diamond", "ring", "square", "triangle", "hex", "star", "drop")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
SVG_CAP = 8 * 1024
RASTER_CAP = 32 * 1024
LOOK_KEYS = ("color", "shape", "icon", "favicon")
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"

ELEMENTS = {"svg", "g", "path", "circle", "rect", "ellipse", "line", "polyline", "polygon", "defs",
            "linearGradient", "radialGradient", "stop", "title"}
ATTRS = {"id", "d", "cx", "cy", "r", "rx", "ry", "x", "y", "x1", "y1", "x2", "y2", "fx", "fy", "width",
         "height", "points", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin",
         "stroke-miterlimit", "stroke-dasharray", "stroke-dashoffset", "opacity", "fill-opacity",
         "stroke-opacity", "fill-rule", "clip-rule", "transform", "viewBox", "preserveAspectRatio", "offset",
         "stop-color", "stop-opacity", "gradientUnits", "gradientTransform", "spreadMethod", "version",
         "href"}
GRADIENTS = {"linearGradient", "radialGradient"}
LOCAL_URL = re.compile(r"url\(\s*#[A-Za-z][\w.-]{0,60}\s*\)")
BAD_VALUE = re.compile(r"javascript:|vbscript:|data:|@import|expression\s*\(|[<>&\\]|url\(", re.I)
_EXT = {".svg": "svg", ".png": "png", ".ico": "ico"}
_MIME = {"svg": "image/svg+xml", "png": "image/png", "ico": "image/x-icon"}


def _local(tag: str) -> tuple[str, str]:
    """'{ns}name' -> (ns, name)."""
    if tag.startswith("{"):
        ns, _, name = tag[1:].partition("}")
        return ns, name
    return "", tag


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def sanitize_svg(data: bytes) -> tuple[str | None, list[str]]:
    """(rebuilt SVG text or None if unusable, what was wrong/stripped). An empty list = it was clean."""
    if not isinstance(data, (bytes, bytearray)):
        return None, ["not a file"]
    if len(data) > SVG_CAP:
        return None, [f"SVG is larger than {SVG_CAP // 1024} KB"]
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        return None, ["SVG isn't UTF-8 text"]
    low = text.lower()
    if "<!doctype" in low or "<!entity" in low:
        return None, ["SVG has a DOCTYPE or entities"]
    try:
        root = ET.fromstring(text)   # comments and processing instructions are dropped here
    except ET.ParseError as e:
        return None, [f"SVG doesn't parse: {e}"]
    ns, name = _local(root.tag)
    if name != "svg" or ns not in ("", SVG_NS):
        return None, ["the file isn't an <svg>"]
    probs: list[str] = []
    ids: set[str] = set()

    def note(p: str):
        if p not in probs and len(probs) < 20:
            probs.append(p)

    def attrs(el_name: str, el) -> str:
        out = []
        for k, v in el.attrib.items():
            ans, an = _local(k)
            if an.lower().startswith("on"):
                note(f"<{el_name}> has an event handler ({an})")
                continue
            if ans not in ("", XLINK_NS) or (ans == XLINK_NS and an != "href") or an not in ATTRS:
                note(f"<{el_name}> attribute '{an}' isn't allowed")
                continue
            v = str(v)
            if an == "href":
                if el_name not in GRADIENTS or not re.fullmatch(r"#[A-Za-z][\w.-]{0,60}", v):
                    note(f"<{el_name}> links to '{v[:40]}' (only #id on gradients)")
                    continue
            elif BAD_VALUE.search(LOCAL_URL.sub("", v)):
                note(f"<{el_name}> {an} has an unsafe value")
                continue
            if an == "id":
                ids.add(v)
            out.append(f' {an}="{_esc(v[:4000])}"')
        return "".join(out)

    def walk(el, depth: int) -> str:
        ens, en = _local(el.tag)
        if ens not in ("", SVG_NS) or en not in ELEMENTS:
            note(f"<{en}> isn't allowed")
            return ""
        if depth > 12:
            note("SVG is nested too deeply")
            return ""
        a = attrs(en, el)
        if depth == 0:
            a = f' xmlns="{SVG_NS}"' + a
        if en == "title":
            inner = _esc((el.text or "")[:120])
        else:
            if (el.text or "").strip():
                note(f"<{en}> has text in it")
            inner = "".join(walk(c, depth + 1) for c in el)
        for c in (el if en == "title" else ()):
            note("<title> has elements in it")
            break
        return f"<{en}{a}>{inner}</{en}>"

    out = walk(root, 0)
    return out, probs


def sniff(data: bytes) -> str | None:
    """'png' / 'ico' / 'svg' by the file's first bytes, or None."""
    b = bytes(data[:512])
    if b.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if b.startswith(b"\x00\x00\x01\x00"):
        return "ico"
    if b.lstrip().lower().startswith((b"<svg", b"<?xml")):
        return "svg"
    return None


def image_problems(data: bytes, kind: str, allow: tuple = ("svg", "png", "ico")) -> tuple[str | None, list[str]]:
    """(data: URI or None, problems) for icon/favicon bytes. kind = the file extension's type."""
    if kind not in allow:
        return None, [f"must be {'/'.join(a.upper() for a in allow)}"]
    got = sniff(data)
    if got != kind:
        return None, [f"isn't really a{'n' if kind in ('svg', 'ico') else ''} {kind.upper()} file"]
    if kind == "svg":
        clean, probs = sanitize_svg(data)
        if clean is None:
            return None, probs
        return "data:image/svg+xml;base64," + base64.b64encode(clean.encode("utf-8")).decode("ascii"), probs
    if len(data) > RASTER_CAP:
        return None, [f"is larger than {RASTER_CAP // 1024} KB"]
    return f"data:{_MIME[kind]};base64," + base64.b64encode(bytes(data)).decode("ascii"), []


def clean_data_uri(uri) -> str | None:
    """Re-check a data: URI that came from outside (the catalog index): rebuilt SVG / real PNG or ICO, or None."""
    if not isinstance(uri, str) or len(uri) > 64 * 1024:
        return None
    m = re.fullmatch(r"data:(image/svg\+xml|image/png|image/x-icon|image/vnd\.microsoft\.icon);base64,"
                     r"([A-Za-z0-9+/=]+)", uri)
    if not m:
        return None
    try:
        data = base64.b64decode(m.group(2), validate=True)
    except ValueError:
        return None
    kind = {"image/svg+xml": "svg", "image/png": "png"}.get(m.group(1), "ico")
    out, probs = image_problems(data, kind)
    return out if out and not probs else None


def _rel_ok(p) -> bool:
    if not isinstance(p, str) or not p or len(p) > 200 or "\\" in p or "\x00" in p:
        return False
    pp = PurePosixPath(p)
    return not pp.is_absolute() and ".." not in pp.parts and str(pp) not in ("", ".")


def _file(folder: Path, rel: str, what: str, allow: tuple, errs: list[str]) -> str | None:
    """Check a file the look points at; returns rel if fine (with a folder) or if there's no folder yet."""
    if not _rel_ok(rel):
        errs.append(f"{what} must be a relative path inside the add-on")
        return None
    kind = _EXT.get(PurePosixPath(rel).suffix.lower())
    if kind not in allow:
        errs.append(f"{what} must be a {' / '.join('.' + a for a in allow)} file")
        return None
    if folder is None:
        return rel
    p = folder / rel
    try:
        if p.is_symlink() or not p.is_file() or folder.resolve() not in p.resolve().parents:
            errs.append(f"{what}: {rel} is not in the add-on")
            return None
        cap = SVG_CAP if kind == "svg" else RASTER_CAP
        if p.stat().st_size > cap:
            errs.append(f"{what}: {rel} is larger than {cap // 1024} KB")
            return None
        data = p.read_bytes()
    except OSError:
        errs.append(f"{what}: {rel} can't be read")
        return None
    _, probs = image_problems(data, kind, allow)
    if probs:
        errs.append(f"{what}: {rel}: " + "; ".join(probs[:3]))
        return None
    return rel


def check_look(look, folder: Path | None, errs: list[str], where: str = "[workers.look]") -> dict | None:
    """Validate a [workers.look] table -> {color?, shape?, icon?, favicon?} (paths kept relative)."""
    if not isinstance(look, dict):
        errs.append(f"{where} must be a table")
        return None
    out: dict = {}
    for k in sorted(set(look) - set(LOOK_KEYS)):
        errs.append(f"{where} {k}: unknown key (only {', '.join(LOOK_KEYS)})")
    c = look.get("color")
    if c is not None:
        if isinstance(c, str) and COLOR_RE.match(c):
            out["color"] = c.lower()
        else:
            errs.append(f"{where} color must look like #c8923a")
    s = look.get("shape")
    if s is not None:
        if s in SHAPES:
            out["shape"] = s
        else:
            errs.append(f"{where} shape must be one of {', '.join(SHAPES)}")
    if look.get("icon") is not None:
        r = _file(folder, look["icon"], f"{where} icon", ("svg",), errs)
        if r:
            out["icon"] = r
    if look.get("favicon") is not None:
        r = _file(folder, look["favicon"], f"{where} favicon", ("png", "ico", "svg"), errs)
        if r:
            out["favicon"] = r
    return out


def check_favicon(v, folder: Path | None, errs: list[str]) -> str | None:
    return _file(folder, v, "favicon", ("png", "ico", "svg"), errs)


# ------------------------------------------------------------------ resolved (cached by file mtimes)
_memo: dict = {}


def data_uri(folder: Path, rel: str | None) -> str | None:
    """The sanitized data: URI of a file the look points at (memoized on its mtime/size), or None."""
    if not rel or not _rel_ok(rel):
        return None
    p = Path(folder) / rel
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
        if key in _memo:
            return _memo[key]
        kind = _EXT.get(PurePosixPath(rel).suffix.lower())
        if p.is_symlink() or kind is None or st.st_size > (SVG_CAP if kind == "svg" else RASTER_CAP):
            return None
        uri, probs = image_problems(p.read_bytes(), kind)
    except OSError:
        return None
    if len(_memo) > 64:
        _memo.clear()
    _memo[key] = uri    # a stripped-but-usable SVG still comes out clean (validation rejects it earlier)
    return uri


def resolve(m: dict, folder: Path) -> dict | None:
    """What the face and app get for an add-on's cloud jobs: {color, shape, icon_url?}, or None (default look)."""
    lk = (m or {}).get("look") or {}
    if not (lk.get("color") or lk.get("shape") or lk.get("icon")):
        return None
    out = {"color": lk.get("color") or "#78beff", "shape": lk.get("shape") or "diamond"}
    icon = data_uri(folder, lk.get("icon"))
    if icon:
        out["icon_url"] = icon
    return out


def favicon_url(m: dict, folder: Path) -> str | None:
    """The add-on's favicon (top-level or [workers.look]), else its worker icon, as a data: URI."""
    lk = (m or {}).get("look") or {}
    return data_uri(folder, (m or {}).get("favicon") or lk.get("favicon")) or data_uri(folder, lk.get("icon"))
