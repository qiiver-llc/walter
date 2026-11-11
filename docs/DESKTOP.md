# Walter desktop face

`walter-desktop` turns a laptop you already have into a Walter **face** (docs/FACES.md): the
face on its screen (full screen or a window), its mic and speaker for talking, a tray icon,
and, if you switch them on, **hands** (docs/HANDS.md). It needs no Walter install of its own:
the brain stays on the wall.

| Piece | Where |
|---|---|
| Launcher (pair, run, tray, autostart, doctor, hands settings) | `desktop/launcher.py`, `bin/walter-desktop` |
| Per-OS settings folder | `desktop/paths.py` |
| Browser in app/kiosk mode | `desktop/browser.py` |
| Start at login | `desktop/autostart.py` |
| Readiness checks | `desktop/doctor.py` |
| Mic + speaker agent (shared with `walter-face`) | `walter/face_agent.py` |
| Hands on the laptop | `desktop/hands_agent.py`, `walter/hands.py` |

## What was tested where

| | Linux | macOS | Windows |
|---|---|---|---|
| Launcher, pairing, doctor, autostart text, hands rules | run on Linux (unit tests + by hand) | **untested** (unit tests render its files on Linux) | **untested** (same) |
| Installer | `install.sh` written for Linux; tarball built here | `install.command`, `build-pkg.sh`: **untested** | `install.ps1`, Inno Setup `.iss`: **untested** |
| PyInstaller build | spec written; built only by the tag workflow | **untested** | **untested** |
| Hands backends | command building unit-tested; the tools (xdotool etc.) aren't installed on the wall, so no real typing/clicking was tried | **untested** | **untested** |

The macOS and Windows parts are written to be correct on paper. Expect small fixes the first
time they run on a real Mac or PC.

## Install

**Linux** (any desktop with Python 3.10+):

    tar xzf walter-desktop-0.1.0-linux.tar.gz
    cd walter-desktop-0.1.0 && ./install.sh        # --minimal: no extras; --no-autostart

It copies the app to `~/.local/share/walter-desktop/app`, makes a venv there, installs the
extras in `requirements-desktop.txt` (mic, tray, mDNS; each optional), puts
`~/.local/bin/walter-desktop` on your PATH and turns on autostart. No root needed.
Helpful system packages: `python3-venv`, `chromium`, `libportaudio2`.

**macOS** (untested): unpack, double-click `scripts/desktop/macos/install.command` (or run it
with bash). Needs Python 3.10+ from python.org or Homebrew. macOS asks for Microphone access the
first time. A `.pkg` recipe is in `scripts/desktop/macos/build-pkg.sh` (unsigned; notarising is
a separate, paid step).

**Windows** (untested): unpack, then in PowerShell
`powershell -ExecutionPolicy Bypass -File scripts\desktop\windows\install.ps1`. Needs Python
3.10+ from python.org ("Add to PATH"). An Inno Setup recipe for a one-click installer is
`scripts/desktop/windows/walter-desktop.iss` (unsigned: SmartScreen will warn).

**From a Walter checkout** (any OS): `bin/walter-desktop ...` works directly.

## Pair it with the wall

1. On the wall say "add a face called Office" (or `walterctl face pair Office`). A 6-digit
   code shows for 5 minutes.
2. On the laptop: `walter-desktop pair http://<wall-ip>:8765 123456 --name Office [--room office]`
3. `walter-desktop run`

The face's key is kept in the settings folder (`face.json`, mode 600) and never printed.

## Run

    walter-desktop run                 # face in an app window + mic + speaker (+ tray, + hands if on)
    walter-desktop run --kiosk         # full screen, no browser bars (Alt+F4 / Cmd+Q to leave)
    walter-desktop run --fullscreen    # app window, started full screen
    walter-desktop run --no-screen     # just the mic and speaker
    walter-desktop run --no-ears --no-speaker --save   # screen only, remembered
    walter-desktop open                # just (re)open the face window
    walter-desktop status | forget

The face opens in Chromium, Chrome, Edge or Brave in app mode with **its own browser profile**
(in the settings folder), so its login cookie stays there. Firefox gets a kiosk window; Safari
a normal window; with none of these, the system default browser.

**Tray** (needs `pystray` + `pillow`): Open face, Mute microphone, Stop hands (kill switch),
Settings folder, Quit. Without them it runs in the terminal (Ctrl+C quits). Some Linux desktops
(GNOME without the AppIndicator extension) hide tray icons.

## Start at login

    walter-desktop autostart enable | disable | show

| OS | What it writes |
|---|---|
| Linux | `~/.config/systemd/user/walter-desktop.service` (systemd user unit, tied to the graphical session); without systemd, `~/.config/autostart/walter-desktop.desktop` |
| macOS | `~/Library/LaunchAgents/app.wallter.walter-desktop.plist` (LaunchAgent; log in the settings folder's `logs/`) |
| Windows | `walter-desktop.cmd` in the Startup folder (`autostart.render_windows_reg` gives the HKCU Run-key alternative) |

## Doctor

`walter-desktop doctor` checks Python, microphone and speaker, a browser, the pairing, whether
the brain answers (and still knows this face), mDNS, and the tools hands would need. Every
problem comes with a plain-language fix. Exit code 1 if something must be fixed.

## Settings folder

| OS | Folder |
|---|---|
| Linux | `$XDG_CONFIG_HOME/walter-desktop` (default `~/.config/walter-desktop`) |
| macOS | `~/Library/Application Support/walter-desktop` |
| Windows | `%APPDATA%\walter-desktop` |

`WALTER_DESKTOP_HOME` overrides it. Inside: `face.json` (key, 600), `desktop.json` (run
choices), `hands.json` (hands policy, 600), `hands.stop` (kill switch on), `hands-audit.log`,
`browser-profile/`, `logs/`.

## Build the downloads

    tools/release/make-desktop.sh [VERSION]    # dist/desktop/walter-desktop-VERSION-linux.tar.gz (+ .sha256, .sig)

It packs only what a face needs from the committed tree, refuses to ship the owner's private
words, and signs with the same Ed25519 release key as `make-release.sh` when that key is
present (verify: `python3 tools/release/walter_release.py verify deploy/release-public.pem TAR TAR.sig SHA256`).
Nothing is uploaded. `.github/workflows/desktop-release.yml` builds Linux/macOS/Windows with
PyInstaller (`desktop/walter-desktop.spec`) on `desktop-v*` tags only and keeps the results as
workflow artifacts; it publishes nothing.

## Limits

- Faces work on the home network or tailnet only (docs/FACES.md, Security).
- Plain http on the LAN, like the companion app: someone on the same network could watch the
  traffic. Use the tailnet for laptops that roam.
- Windows: the face's mic score works, but local whisper ears are untested there; without them
  the wall transcribes (audio goes to the brain).
