"""walter-desktop doctor: is this computer ready to be a Walter face? Each check gives
a plain-language fix. Every probe is injectable so the checks are tested with fakes."""
from __future__ import annotations

import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import browser as B

MIN_PY = (3, 10)


@dataclass
class Check:
    name: str
    ok: bool | None          # None = optional thing that's missing (a warning, not a failure)
    detail: str
    fix: str = ""

    def line(self) -> str:
        mark = {True: "ok  ", False: "FAIL", None: "warn"}[self.ok]
        s = f"[{mark}] {self.name}: {self.detail}"
        return s + (f"\n       fix: {self.fix}" if self.fix and self.ok is not True else "")


def check_python(version=sys.version_info) -> Check:
    v = f"{version[0]}.{version[1]}"
    if tuple(version[:2]) >= MIN_PY:
        return Check("Python", True, v)
    return Check("Python", False, f"{v} is too old", "install Python 3.10 or newer from python.org "
                 "(or your package manager), then run the installer again")


def _sounddevice_devices():
    import sounddevice as sd   # noqa: optional
    return sd.query_devices()


def check_audio(osn: str, devices=_sounddevice_devices, which=shutil.which) -> list[Check]:
    """Microphone and speaker. sounddevice when installed; else the OS tools."""
    try:
        devs = list(devices())
        ins = [d for d in devs if d.get("max_input_channels", 0) > 0]
        outs = [d for d in devs if d.get("max_output_channels", 0) > 0]
        mic = Check("Microphone", bool(ins), f"{len(ins)} input device(s)" if ins else "no input device found",
                    "plug in a USB mic or headset, and allow microphone access for Python in the OS privacy settings")
        spk = Check("Speaker", bool(outs), f"{len(outs)} output device(s)" if outs else "no output device found",
                    "connect speakers or headphones and check the system sound settings")
        return [mic, spk]
    except Exception as e:
        mic = Check("Microphone", None, f"can't list devices ({type(e).__name__}): the face runs without ears",
                    "install the voice extras: pip install numpy sounddevice (the installer offers it)")
    from walter.face_agent import find_player
    if osn == "linux":
        player = find_player()
        spk = Check("Speaker", bool(player), f"plays replies with {player[0]}" if player else "no audio player",
                    "install pipewire (pw-play), pulseaudio-utils (paplay) or alsa-utils (aplay)")
    else:
        spk = Check("Speaker", None, "not checked without sounddevice", "pip install sounddevice")
    return [mic, spk]


def check_browser(osn: str, which=shutil.which, exists=None) -> Check:
    import os
    found = B.find(osn, which, exists or os.path.exists)
    if found:
        name = found[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        mode = {"chrome": "app/kiosk window", "firefox": "kiosk window", "safari": "normal window"}[found[1]]
        return Check("Browser", True, f"{name} ({mode})")
    return Check("Browser", None, "no Chromium, Chrome, Edge or Firefox found: the default browser opens instead",
                 "install Chromium/Chrome/Edge for a clean full-screen face")


def check_paired(state: dict) -> Check:
    if state.get("token") and state.get("brain"):
        f = state.get("face") or {}
        return Check("Paired", True, f"face {f.get('name', '?')} on {state['brain']}")
    return Check("Paired", False, "not paired yet",
                 "on the wall say 'add a face called Office' (or walterctl face pair Office), then run: "
                 "walter-desktop pair http://<wall-ip>:8765 <code> --name Office")


def check_brain(state: dict, opener=urllib.request.urlopen) -> Check:
    if not state.get("brain"):
        return Check("Brain", None, "no brain address yet", "pair first")
    req = urllib.request.Request(state["brain"] + "/api/face/settings",
                                 headers={"Authorization": f"Bearer {state.get('token', '')}"})
    try:
        with opener(req, timeout=5) as r:
            r.read()
        return Check("Brain", True, f"{state['brain']} answers and knows this face")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return Check("Brain", False, "the brain doesn't know this face any more",
                         "pair again: walterctl face pair NAME on the wall, then walter-desktop pair ...")
        if e.code == 403:
            return Check("Brain", False, "the brain refuses this address",
                         "faces only work on the home network or tailnet; check [app] allow_lan on the wall")
        return Check("Brain", False, f"HTTP {e.code}", "check the wall is running (walterctl status there)")
    except Exception as e:
        return Check("Brain", False, f"can't reach {state['brain']} ({getattr(e, 'reason', e)})",
                     "is this computer on the same Wi-Fi as the wall? is the wall awake? try its IP address")


def check_mdns(importer=__import__) -> Check:
    try:
        importer("zeroconf")
        return Check("mDNS", True, "zeroconf installed: finds the wall by itself")
    except Exception:
        return Check("mDNS", None, "zeroconf not installed: pairing needs the wall's address",
                     "optional: pip install zeroconf")


def check_hands(osn: str, which=shutil.which, env=None) -> Check:
    from walter import hands as H
    tools = H.detect_backend(osn, which=which, env=env)
    missing = [k for k in ("type", "click", "screenshot") if not tools.get(k)]
    if not missing:
        return Check("Hands tools", True, ", ".join(f"{k}: {v}" for k, v in sorted(tools.items()) if v))
    return Check("Hands tools", None, "missing for " + ", ".join(missing) + " (only matters if you turn hands on)",
                 H.install_hint(osn, tools))


def run_all(osn: str, state: dict, **probes) -> list[Check]:
    out = [check_python(probes.get("version", sys.version_info))]
    out += check_audio(osn, probes.get("devices", _sounddevice_devices), probes.get("which", shutil.which))
    out.append(check_browser(osn, probes.get("which", shutil.which), probes.get("exists")))
    out.append(check_paired(state))
    out.append(check_brain(state, probes.get("opener", urllib.request.urlopen)))
    out.append(check_mdns(probes.get("importer", __import__)))
    out.append(check_hands(osn, probes.get("which", shutil.which), probes.get("env")))
    return out


def report(checks: list[Check]) -> tuple[str, int]:
    text = "\n".join(c.line() for c in checks)
    bad = sum(1 for c in checks if c.ok is False)
    text += "\n\n" + ("All good: this computer is ready to be a Walter face." if not bad else
                      f"{bad} thing(s) to fix (see 'fix:' above).")
    return text, (1 if bad else 0)
