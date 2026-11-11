"""Where the desktop face keeps its files, per operating system (stdlib only).

  Linux    $XDG_CONFIG_HOME/walter-desktop      (default ~/.config/walter-desktop)
  macOS    ~/Library/Application Support/walter-desktop
  Windows  %APPDATA%\\walter-desktop            (default ~\\AppData\\Roaming\\walter-desktop)

WALTER_DESKTOP_HOME overrides all of them (tests, portable installs).
Everything here is a pure function of (os name, environment, home) so it can
be tested for every OS on any OS.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

APP = "walter-desktop"


def os_name(platform: str | None = None) -> str:
    """'linux', 'mac' or 'windows' (anything unknown counts as linux: a Unix-like desktop)."""
    p = (platform or sys.platform).lower()
    if p.startswith("darwin"):
        return "mac"
    if p.startswith(("win", "cygwin", "msys")):
        return "windows"
    return "linux"


def config_dir(osn: str | None = None, env: dict | None = None, home: str | None = None):
    """The per-user config folder (a Path for this OS; a Pure*Path when asked about another OS)."""
    osn = osn or os_name()
    env = os.environ if env is None else env
    if env.get("WALTER_DESKTOP_HOME"):
        return _path(osn, env["WALTER_DESKTOP_HOME"])
    home = home if home is not None else str(Path.home())
    if osn == "windows":
        base = env.get("APPDATA") or str(PureWindowsPath(home, "AppData", "Roaming"))
        return _path(osn, base, APP)
    if osn == "mac":
        return _path(osn, home, "Library", "Application Support", APP)
    base = env.get("XDG_CONFIG_HOME") or str(PurePosixPath(home, ".config"))
    if not PurePosixPath(base).is_absolute():   # the XDG spec says ignore relative values
        base = str(PurePosixPath(home, ".config"))
    return _path(osn, base, APP)


def _path(osn: str, *parts):
    here = os_name()
    if osn == here:
        return Path(*parts)
    return PureWindowsPath(*parts) if osn == "windows" else PurePosixPath(*parts)


def ensure_dir(path: Path | None = None) -> Path:
    """Create the config folder private (0700 on Unix) and return it."""
    p = Path(path or config_dir())
    p.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(p, 0o700)
    return p


def config_file(path: Path | None = None) -> Path:
    return Path(path or config_dir()) / "desktop.json"


def hands_file(path: Path | None = None) -> Path:
    return Path(path or config_dir()) / "hands.json"


def log_dir(path: Path | None = None) -> Path:
    return Path(path or config_dir()) / "logs"


def repo_root() -> Path:
    """The folder holding walter/ and desktop/ (the tarball root, or the repo)."""
    if getattr(sys, "frozen", False):   # PyInstaller one-folder build
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent
