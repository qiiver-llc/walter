#!/usr/bin/env bash
# Install the Walter desktop face for this user (Linux). Run from the unpacked tarball:
#
#   ./install.sh [--minimal] [--no-autostart] [--prefix DIR]
#
#   --minimal       don't install the optional Python extras (mic, tray, mDNS)
#   --no-autostart  don't start it at login
#   --prefix        where the app goes (default ~/.local/share/walter-desktop/app)
#
# Nothing here needs root. System packages that help (ask your package manager):
#   python3-venv, chromium (or Chrome/Edge), pipewire or pulseaudio-utils, libportaudio2,
#   and only for hands: xdotool scrot xclip xprintidle (X11) or wtype grim wl-clipboard (Wayland).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}/walter-desktop/app"
MINIMAL=0; AUTOSTART=1
while [[ $# -gt 0 ]]; do
  case $1 in
    --minimal) MINIMAL=1; shift ;;
    --no-autostart) AUTOSTART=0; shift ;;
    --prefix) PREFIX="$2"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

PY=$(command -v python3 || true)
[[ -n "$PY" ]] || { echo "Python 3.10+ is needed: sudo apt install python3 python3-venv" >&2; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Python 3.10 or newer is needed (found $("$PY" -V))" >&2; exit 1; }

echo "installing to $PREFIX"
mkdir -p "$PREFIX"
# copy the app (desktop/, the few walter/ modules a face needs, bin/, docs/)
for d in desktop walter bin docs; do
  rm -rf "${PREFIX:?}/$d"
  cp -R "$HERE/$d" "$PREFIX/$d"
done
cp "$HERE/VERSION" "$HERE/requirements-desktop.txt" "$PREFIX/" 2>/dev/null || true

if [[ ! -x "$PREFIX/.venv/bin/python" ]]; then
  "$PY" -m venv "$PREFIX/.venv" || { echo "couldn't make a venv: sudo apt install python3-venv" >&2; exit 1; }
fi
if [[ $MINIMAL == 0 ]]; then
  "$PREFIX/.venv/bin/python" -m pip install -q --upgrade pip >/dev/null 2>&1 || true
  if ! "$PREFIX/.venv/bin/python" -m pip install -q -r "$PREFIX/requirements-desktop.txt"; then
    echo "some extras didn't install (no internet, or missing libportaudio2?); the face still works without them" >&2
  fi
fi

mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/walter-desktop" <<EOW
#!/bin/sh
PYTHONPATH="$PREFIX\${PYTHONPATH:+:\$PYTHONPATH}" exec "$PREFIX/.venv/bin/python" -m desktop "\$@"
EOW
chmod +x "$HOME/.local/bin/walter-desktop"
echo "installed: ~/.local/bin/walter-desktop"

if [[ $AUTOSTART == 1 ]]; then
  "$HOME/.local/bin/walter-desktop" autostart enable || echo "autostart: skipped (walter-desktop autostart enable later)"
fi

cat <<'EOT'

Next:
  1. On the wall, say "add a face called Office" (or run: walterctl face pair Office).
  2. Here: walter-desktop pair http://<wall-ip>:8765 <6-digit code> --name Office
  3. walter-desktop run          (walter-desktop doctor checks everything)
EOT
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "(add ~/.local/bin to your PATH, or log out and in)";; esac
