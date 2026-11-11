"""Open the face page full screen (or as an app window) in a browser that's already installed.

Chromium-family browsers (Chromium, Chrome, Edge, Brave) get --app / --kiosk and their OWN
profile folder under the desktop config dir, so the face's login cookie lives there and
nowhere else. Safari and Firefox have no reliable app mode: they open a normal window.
The system default browser is the last fallback.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

LINUX = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "microsoft-edge",
         "microsoft-edge-stable", "brave-browser", "firefox"]
MAC_APPS = [("Google Chrome", "chrome"), ("Chromium", "chrome"), ("Microsoft Edge", "chrome"),
            ("Brave Browser", "chrome"), ("Safari", "safari"), ("Firefox", "firefox")]
WIN_PATHS = [
    (r"Microsoft\Edge\Application\msedge.exe", "chrome"),
    (r"Google\Chrome\Application\chrome.exe", "chrome"),
    (r"BraveSoftware\Brave-Browser\Application\brave.exe", "chrome"),
]


def find(osn: str, which=shutil.which, exists=os.path.exists, env: dict | None = None) -> tuple[str, str] | None:
    """(path or mac app name, family 'chrome'|'firefox'|'safari') of the best browser, or None."""
    env = os.environ if env is None else env
    if osn == "mac":
        for app, fam in MAC_APPS:
            for root in ("/Applications", os.path.expanduser("~/Applications")):
                if exists(f"{root}/{app}.app"):
                    return app, fam
        return None
    if osn == "windows":
        for base in (env.get("ProgramFiles(x86)"), env.get("ProgramFiles"), env.get("LOCALAPPDATA")):
            for rel, fam in WIN_PATHS:
                if base and exists(os.path.join(base, rel)):
                    return os.path.join(base, rel), fam
        for name in ("msedge", "chrome"):
            p = which(name)
            if p:
                return p, "chrome"
        return None
    for name in LINUX:
        p = which(name)
        if p:
            return p, "firefox" if "firefox" in name else "chrome"
    return None


def command(osn: str, browser: tuple[str, str] | None, url: str, profile: Path | str,
            mode: str = "app") -> list[str] | None:
    """The argv that opens url. mode: 'kiosk' (full screen, no chrome), 'app' (a window), 'fullscreen'.
    None = no browser found: use the system default (webbrowser.open)."""
    if not browser:
        return None
    path, fam = browser
    if fam == "chrome":
        flags = [f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                 "--autoplay-policy=no-user-gesture-required", "--disable-features=Translate"]
        if mode == "kiosk":
            flags += ["--kiosk", url]
        else:
            flags += ([f"--app={url}"] + (["--start-fullscreen"] if mode == "fullscreen" else []))
        if osn == "mac":
            return ["open", "-n", "-a", path, "--args", *flags]
        return [path, *flags]
    if fam == "firefox":
        args = ["--kiosk", url] if mode == "kiosk" else ["--new-window", url]
        if osn == "mac":
            return ["open", "-n", "-a", path, "--args", *args]
        return [path, *args]
    # safari: no kiosk switch; a normal window (the face page itself goes full screen on F11 / ctrl-cmd-F)
    return ["open", "-a", path, url]


def open_face(osn: str, url: str, profile: Path, mode: str = "app", spawn=subprocess.Popen,
              which=shutil.which, exists=os.path.exists):
    """Start the browser (returns the process, or None when the system default opened it)."""
    Path(profile).mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(profile, 0o700)
    cmd = command(osn, find(osn, which, exists), url, profile, mode)
    if cmd is None:
        import webbrowser
        webbrowser.open(url)
        return None
    return spawn(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                 start_new_session=(os.name != "nt"))
