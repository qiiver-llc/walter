"""Spotify Connect: Walter as a speaker you pick in the Spotify app (Spotify Premium required).

librespot (open source Spotify Connect receiver) runs as a systemd USER service,
``walter-spotify.service``, named after the device ("Walter", config [spotify] name).
Login is zeroconf only: the person picks "Walter" in their own Spotify app on the same
network and the app hands librespot a session. Walter never sees or stores Spotify
credentials (``--disable-credential-cache``, no username/password, no audio cache).

Install: ``sudo -n apt-get install librespot`` if Debian ships it, else the pinned
raspotify .deb (which bundles librespot) is downloaded, sha256-checked and only
*extracted* (``dpkg-deb -x``, nothing installed system-wide) into ~/.walter/tools/.

Ducking: the conductor calls :class:`Ducker` on every state change; it finds librespot's
PipeWire stream (pw-dump) and sets its volume to duck_volume percent while Walter
listens/thinks and mutes it while he talks (MPRIS Pause is used instead if librespot
exposes it). Work runs on a background thread so a state change never waits on it.

What Walter can NOT do here: search or start Spotify music by voice. That needs the
Spotify Web API and an OAuth login; later.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tomllib
import urllib.request
from pathlib import Path

DATA_DIR = Path(os.environ.get("WALTER_DATA", Path.home() / ".walter"))
TOOLS = DATA_DIR / "tools" / "librespot"
UNIT_NAME = "walter-spotify.service"
UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
UA = "Walter-spotify/1.0"

# Pinned: raspotify 0.48.4 = librespot v0.8.0 (amd64). Bump version, URL and sha256 together.
PIN = {
    "x86_64": {
        "version": "librespot-v0.8.0-raspotify-0.48.4",
        "url": "https://github.com/dtcooper/raspotify/releases/download/0.48.4/"
               "raspotify_0.48.4.librespot.v0.8.0-99b6088_amd64.deb",
        "sha256": "7b228feb0623bf0f9b65904961ad3ad9df8e1f4b07a002ab2388e1ee9385eeed",
    },
}


def config(path: Path | None = None) -> dict:
    try:
        with open(path or DATA_DIR / "config.toml", "rb") as f:
            c = tomllib.load(f).get("spotify", {})
    except (OSError, ValueError):
        c = {}
    name = str(c.get("name") or "Walter")
    name = "".join(ch for ch in name if ch.isalnum() or ch in " -_'")[:40].strip() or "Walter"
    br = int(c.get("bitrate", 320))
    return {"name": name, "bitrate": br if br in (96, 160, 320) else 320,
            "duck_volume": int(c.get("duck_volume", 25))}


# ------------------------------------------------------------------ install
def bundled_binary() -> Path | None:
    for p in sorted(TOOLS.glob("*/librespot"), reverse=True):
        if os.access(p, os.X_OK):
            return p
    return None


def binary() -> str | None:
    return shutil.which("librespot") or (str(bundled_binary()) if bundled_binary() else None)


def apt_candidate(run=subprocess.run) -> bool:
    try:
        r = run(["apt-cache", "policy", "librespot"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    for line in r.stdout.splitlines():
        if line.strip().startswith("Candidate:"):
            return line.split(":", 1)[1].strip() not in ("", "(none)")
    return False


def _download(url: str, dest: Path):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def install(run=subprocess.run, download=_download, arch: str | None = None, log=print) -> str:
    """librespot path after installing it (apt if Debian has it, else the pinned release).
    Raises RuntimeError with a plain reason."""
    have = binary()
    if have:
        return have
    if apt_candidate(run):
        r = run(["sudo", "-n", "apt-get", "install", "-y", "librespot"], capture_output=True, text=True)
        if r.returncode == 0 and shutil.which("librespot"):
            return shutil.which("librespot")
        log("apt-get install librespot failed; using the pinned release instead")
    pin = PIN.get(arch or os.uname().machine)
    if not pin:
        raise RuntimeError(f"no pinned librespot build for {arch or os.uname().machine}")
    TOOLS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="librespot-dl-", dir=TOOLS) as tmp:
        deb = Path(tmp) / "pkg.deb"
        log(f"downloading {pin['version']} ...")
        download(pin["url"], deb)
        got = sha256(deb)
        if got != pin["sha256"]:
            raise RuntimeError(f"sha256 mismatch for the librespot download ({got[:12]}...): not installed")
        out = Path(tmp) / "x"
        r = run(["dpkg-deb", "-x", str(deb), str(out)], capture_output=True, text=True)
        src = out / "usr" / "bin" / "librespot"
        if r.returncode != 0 or not src.exists():
            raise RuntimeError("couldn't unpack the librespot package")
        dest = TOOLS / pin["version"]
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / "librespot")
        os.chmod(dest / "librespot", 0o755)
        (dest / "SOURCE").write_text(f"{pin['url']}\nsha256 {pin['sha256']}\n")
    return str(dest / "librespot")


# ------------------------------------------------------------------ service
def exec_args(exe: str, cfg: dict) -> list[str]:
    return [exe, "--name", cfg["name"], "--device-type", "speaker", "--backend", "pulseaudio",
            "--bitrate", str(cfg["bitrate"]), "--disable-credential-cache",
            "--disable-audio-cache", "--initial-volume", "70", "--quiet"]


def unit_text(exe: str, cfg: dict) -> str:
    def q(a):   # systemd quoting for a word with spaces
        return '"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"' if (" " in a or '"' in a) else a
    return f"""[Unit]
Description=Walter Spotify Connect speaker ({cfg['name']}) - librespot, zeroconf login only
After=pipewire-pulse.service network-online.target
Wants=pipewire-pulse.service

[Service]
ExecStart={' '.join(q(a) for a in exec_args(exe, cfg))}
Restart=on-failure
RestartSec=5
Nice=5

[Install]
WantedBy=default.target
"""


def _ctl(*args, run=subprocess.run):
    return run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=30)


def enable(run=subprocess.run, download=_download, log=print) -> str:
    exe = install(run=run, download=download, log=log)
    cfg = config()
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    (UNIT_DIR / UNIT_NAME).write_text(unit_text(exe, cfg))
    _ctl("daemon-reload", run=run)
    r = _ctl("enable", "--now", UNIT_NAME, run=run)
    if r.returncode != 0:
        raise RuntimeError(f"couldn't start {UNIT_NAME}: {(r.stderr or '').strip()[:200]}")
    _ctl("restart", UNIT_NAME, run=run)   # pick up a changed unit
    return (f"Spotify Connect is on: pick \"{cfg['name']}\" as the speaker in the Spotify app "
            f"(same Wi-Fi; Spotify Premium required)")


def disable(run=subprocess.run) -> str:
    _ctl("disable", "--now", UNIT_NAME, run=run)
    return "Spotify Connect is off"


def is_active(run=subprocess.run) -> bool:
    try:
        return _ctl("is-active", UNIT_NAME, run=run).stdout.strip() == "active"
    except (OSError, subprocess.SubprocessError):
        return False


# ------------------------------------------------------------------ PipeWire stream
def stream_nodes(dump: list | None = None, run=subprocess.run) -> list[dict]:
    """librespot's playback streams: [{id, volume, muted, running}] from pw-dump."""
    if dump is None:
        try:
            dump = json.loads(run(["pw-dump"], capture_output=True, text=True, timeout=5).stdout or "[]")
        except (OSError, ValueError, subprocess.SubprocessError):
            return []
    out = []
    for o in dump if isinstance(dump, list) else []:
        info = (o or {}).get("info") or {}
        props = info.get("props") or {}
        if props.get("media.class") != "Stream/Output/Audio":
            continue
        names = " ".join(str(props.get(k, "")) for k in
                         ("application.name", "node.name", "application.process.binary")).lower()
        if "librespot" not in names:
            continue
        out.append({"id": o.get("id"), "running": info.get("state") == "running"})
    return out


def playing(run=subprocess.run) -> bool:
    return any(n["running"] for n in stream_nodes(run=run))


def mpris_name(run=subprocess.run) -> str | None:
    try:
        r = run(["busctl", "--user", "list", "--no-legend"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in r.stdout.splitlines():
        name = line.split()[0] if line.split() else ""
        if name.startswith("org.mpris.MediaPlayer2.") and "librespot" in name.lower():
            return name
    return None


def _mpris(name: str, method: str, run=subprocess.run):
    run(["busctl", "--user", "call", name, "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player",
         method], capture_output=True, timeout=3)


class Ducker:
    """listening/thinking/working -> stream volume duck_volume %; speaking -> pause via MPRIS
    if available, else mute the stream; anything else -> back to full (and unpause/unmute
    only what *we* did). Cheap no-op unless the service unit exists."""
    DUCK = ("listening", "thinking", "working")
    PAUSE = ("speaking",)

    def __init__(self, run=subprocess.run, unit: Path | None = None, threaded=True, cfg: dict | None = None):
        self.run, self.unit, self.threaded, self.cfg = run, unit, threaded, cfg
        self.mode = "full"
        self.how = None            # "mpris" | "mute" while we hold it quiet
        self.lock = threading.Lock()

    def on_state(self, state: str):
        if not (self.unit or UNIT_DIR / UNIT_NAME).exists():
            return
        want = "pause" if state in self.PAUSE else "duck" if state in self.DUCK else "full"
        if self.mode == "pause" and want == "duck":
            want = "pause"
        if want == self.mode:
            return
        self.mode = want
        if self.threaded:
            threading.Thread(target=self._apply, args=(want,), daemon=True, name="spotify-duck").start()
        else:
            self._apply(want)

    def _apply(self, want: str):
        with self.lock:
            if want != self.mode:          # superseded while queued
                return
            try:
                nodes = stream_nodes(run=self.run)
                if not nodes and self.how is None:
                    return
                duck = (self.cfg or config())["duck_volume"] / 100.0
                wp = lambda *a: self.run(["wpctl", *map(str, a)], capture_output=True, timeout=3)  # noqa: E731
                if want == "duck":
                    for n in nodes:
                        wp("set-volume", n["id"], f"{duck:.2f}")
                elif want == "pause":
                    for n in nodes:
                        wp("set-volume", n["id"], f"{duck:.2f}")
                    if any(n["running"] for n in nodes) and self.how is None:
                        name = mpris_name(self.run)
                        if name:
                            _mpris(name, "Pause", self.run)
                            self.how = "mpris"
                        else:
                            for n in nodes:
                                wp("set-mute", n["id"], 1)
                            self.how = "mute"
                else:
                    for n in nodes:
                        wp("set-volume", n["id"], "1.0")
                        if self.how == "mute":
                            wp("set-mute", n["id"], 0)
                    if self.how == "mpris":
                        name = mpris_name(self.run)
                        if name:
                            _mpris(name, "Play", self.run)
                    self.how = None
            except Exception:  # noqa: BLE001
                pass


# ------------------------------------------------------------------ CLI
USAGE = """walterctl spotify status|enable|disable
  enable    install librespot if needed and start the "Walter" Spotify Connect speaker
            (systemd user service walter-spotify; zeroconf login from the Spotify app,
            Spotify Premium required, no credentials stored)
  disable   stop it and don't start it at boot
  status    installed? running? playing right now?
Walter can't search or start Spotify music by voice (that needs the Spotify Web API)."""


def status_text(run=subprocess.run) -> str:
    exe, cfg = binary(), config()
    lines = [f"librespot: {exe or 'not installed'}",
             f"service: {UNIT_NAME} " + ("active" if is_active(run) else
                                          "installed, not running" if (UNIT_DIR / UNIT_NAME).exists() else "not set up"),
             f"speaker name: {cfg['name']}",
             "playing: " + ("yes" if playing(run) else "no")]
    return "\n".join(lines)


def cli(argv: list[str]) -> int:
    cmd = argv[0] if argv else "status"
    if cmd in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    try:
        if cmd == "status":
            print(status_text())
        elif cmd in ("enable", "on", "start"):
            print(enable())
        elif cmd in ("disable", "off", "stop"):
            print(disable())
        else:
            print(USAGE)
            return 2
    except RuntimeError as e:
        print(f"Spotify: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
