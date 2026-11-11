# -*- mode: python -*-
# PyInstaller recipe for the Walter desktop face (one folder, per OS; docs/DESKTOP.md).
#   pip install pyinstaller pystray pillow sounddevice numpy zeroconf
#   pyinstaller desktop/walter-desktop.spec        -> dist/walter-desktop/walter-desktop[.exe]
# Built by .github/workflows/desktop-release.yml on desktop-v* tags. Only the Linux build has
# been tried by hand; Mac and Windows builds are untested until that workflow first runs.
import os
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
hidden = ["walter.face_agent", "walter.faces", "walter.devices", "walter.hands", "walter.config",
          "walter.ears", "walter.turn", "desktop.launcher", "desktop.hands_agent", "desktop.doctor",
          "desktop.autostart", "desktop.browser", "desktop.paths"]
for mod, plat in (("pystray._xorg", "linux"), ("pystray._darwin", "darwin"), ("pystray._win32", "win32")):
    if sys.platform.startswith(plat):
        hidden.append(mod)

a = Analysis([os.path.join(ROOT, "desktop", "pyinstaller_entry.py")], pathex=[ROOT], hiddenimports=hidden,
             datas=[(os.path.join(ROOT, "docs", f), "docs") for f in ("DESKTOP.md", "HANDS.md")],
             excludes=["faster_whisper", "ctranslate2", "torch", "cv2", "aiohttp", "piper"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="walter-desktop", console=True)
coll = COLLECT(exe, a.binaries, a.datas, name="walter-desktop")
