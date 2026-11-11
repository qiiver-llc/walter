"""Start the desktop face at login, per OS. The render_* functions only build text
(tested for every OS anywhere); install/remove write or delete exactly one file.

  Linux    ~/.config/systemd/user/walter-desktop.service  (systemd --user, tied to the
           graphical session); without systemd: ~/.config/autostart/walter-desktop.desktop
  macOS    ~/Library/LaunchAgents/app.wallter.walter-desktop.plist (LaunchAgent)
  Windows  %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\walter-desktop.cmd
           (a Startup-folder script; render_windows_reg() is the HKCU Run-key alternative)
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath
from xml.sax.saxutils import escape

LABEL = "app.wallter.walter-desktop"
UNIT = "walter-desktop.service"


def _q(arg: str) -> str:
    """Quote one argument for a systemd ExecStart / desktop Exec line."""
    arg = str(arg)
    if arg and all(c.isalnum() or c in "/._-:=+@," for c in arg):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _wq(arg: str) -> str:
    """Quote one argument for cmd.exe."""
    arg = str(arg)
    return arg if arg and not any(c in arg for c in ' "&|<>^%') else '"' + arg.replace('"', '""') + '"'


def render_systemd(argv: list[str], env: dict | None = None) -> str:
    lines = ["[Unit]", "Description=Walter desktop face (screen, mic, speaker; docs/DESKTOP.md)",
             "After=graphical-session.target network-online.target", "PartOf=graphical-session.target", "",
             "[Service]", "Type=simple", "ExecStart=" + " ".join(_q(a) for a in argv),
             "Restart=on-failure", "RestartSec=10"]
    for k, v in sorted((env or {}).items()):
        lines.append(f"Environment={_q(f'{k}={v}')}")
    lines += ["", "[Install]", "WantedBy=graphical-session.target", ""]
    return "\n".join(lines)


def render_xdg_desktop(argv: list[str]) -> str:
    return "\n".join(["[Desktop Entry]", "Type=Application", "Name=Walter face",
                      "Comment=Walter desktop face (docs/DESKTOP.md)",
                      "Exec=" + " ".join(_q(a) for a in argv), "X-GNOME-Autostart-enabled=true",
                      "NoDisplay=true", ""])


def render_launchd(argv: list[str], log_dir: str) -> str:
    args = "\n".join(f"    <string>{escape(str(a))}</string>" for a in argv)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ProcessType</key>
  <string>Interactive</string>
  <key>StandardOutPath</key>
  <string>{escape(log_dir)}/desktop.log</string>
  <key>StandardErrorPath</key>
  <string>{escape(log_dir)}/desktop.log</string>
</dict>
</plist>
"""


def render_windows_cmd(argv: list[str]) -> str:
    # start "" /min: no console window lingers; pythonw would hide it entirely
    return "@echo off\r\nrem Walter desktop face (docs/DESKTOP.md)\r\nstart \"\" /min " + \
        " ".join(_wq(a) for a in argv) + "\r\n"


def render_windows_reg(argv: list[str]) -> str:
    cmd = " ".join(_wq(a) for a in argv).replace("\\", "\\\\").replace('"', '\\"')
    return ("Windows Registry Editor Version 5.00\r\n\r\n"
            "[HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run]\r\n"
            f"\"WalterDesktop\"=\"{cmd}\"\r\n")


def target(osn: str, home: str, env: dict | None = None, systemd: bool = True):
    """Where the autostart file goes."""
    env = os.environ if env is None else env
    if osn == "windows":
        base = env.get("APPDATA") or str(PureWindowsPath(home, "AppData", "Roaming"))
        return PureWindowsPath(base, "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
                               "walter-desktop.cmd")
    if osn == "mac":
        return PurePosixPath(home, "Library", "LaunchAgents", LABEL + ".plist")
    base = env.get("XDG_CONFIG_HOME") or str(PurePosixPath(home, ".config"))
    if systemd:
        return PurePosixPath(base, "systemd", "user", UNIT)
    return PurePosixPath(base, "autostart", "walter-desktop.desktop")


def render(osn: str, argv: list[str], log_dir: str = "", systemd: bool = True, env: dict | None = None) -> str:
    if osn == "windows":
        return render_windows_cmd(argv)
    if osn == "mac":
        return render_launchd(argv, log_dir)
    return render_systemd(argv, env) if systemd else render_xdg_desktop(argv)


def has_systemd_user(which=shutil.which, run=subprocess.run) -> bool:
    if not which("systemctl"):
        return False
    try:
        r = run(["systemctl", "--user", "is-system-running"], capture_output=True, text=True, timeout=5)
        return (r.stdout or "").strip() not in ("", "offline")
    except Exception:
        return False


def enable(osn: str, argv: list[str], log_dir: str, run=subprocess.run, env: dict | None = None) -> str:
    """Install the autostart entry for this user; returns a sentence for the person."""
    home = str(Path.home())
    sysd = osn == "linux" and has_systemd_user(run=run)
    path = Path(str(target(osn, home, systemd=sysd)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(osn, argv, log_dir, systemd=sysd, env=env))
    if sysd:
        run(["systemctl", "--user", "daemon-reload"], check=False)
        run(["systemctl", "--user", "enable", UNIT], check=False)
        return f"autostart on: {path} (systemd user unit; starts with your desktop session)"
    if osn == "mac":
        run(["launchctl", "unload", str(path)], check=False, capture_output=True)
        run(["launchctl", "load", "-w", str(path)], check=False)
        return f"autostart on: {path} (LaunchAgent; starts when you log in)"
    return f"autostart on: {path}"


def disable(osn: str, run=subprocess.run) -> str:
    home = str(Path.home())
    gone = []
    for sysd in ((True, False) if osn == "linux" else (True,)):
        path = Path(str(target(osn, home, systemd=sysd)))
        if not path.exists():
            continue
        if osn == "linux" and sysd:
            run(["systemctl", "--user", "disable", UNIT], check=False)
        if osn == "mac":
            run(["launchctl", "unload", "-w", str(path)], check=False, capture_output=True)
        path.unlink()
        gone.append(str(path))
    return ("autostart off (removed " + ", ".join(gone) + ")") if gone else "autostart was already off"
