#!/bin/bash
# Walter desktop face for macOS (UNTESTED on a real Mac: written on Linux; see docs/DESKTOP.md).
# Double-click in Finder (it opens Terminal), or run: bash install.command
# Puts the app in ~/Library/Application Support/walter-desktop/app with its own Python venv,
# a command at ~/.local/bin/walter-desktop, and a LaunchAgent so it starts at login.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$HERE"; [[ -d "$ROOT/desktop" ]] || ROOT="$(cd "$HERE/../../.." && pwd)"   # tarball or repo
APP="$HOME/Library/Application Support/walter-desktop/app"
PY=""
for c in /opt/homebrew/bin/python3 /usr/local/bin/python3 /Library/Frameworks/Python.framework/Versions/Current/bin/python3 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [[ -z "$PY" ]]; then
  echo "Python 3.10+ is needed. Install it from https://www.python.org/downloads/macos/ (or: brew install python), then run this again."
  exit 1
fi
mkdir -p "$APP"
for d in desktop walter bin docs; do rm -rf "${APP:?}/$d"; cp -R "$ROOT/$d" "$APP/$d"; done
REQ="$ROOT/requirements-desktop.txt"; [[ -f "$REQ" ]] || REQ="$ROOT/scripts/desktop/requirements-desktop.txt"
cp "$REQ" "$APP/requirements-desktop.txt"
[[ -x "$APP/.venv/bin/python" ]] || "$PY" -m venv "$APP/.venv"
"$APP/.venv/bin/python" -m pip install -q --upgrade pip || true
"$APP/.venv/bin/python" -m pip install -q -r "$APP/requirements-desktop.txt" || echo "some extras didn't install; the face still works"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/walter-desktop" <<EOW
#!/bin/sh
PYTHONPATH="$APP" exec "$APP/.venv/bin/python" -m desktop "\$@"
EOW
chmod +x "$HOME/.local/bin/walter-desktop"
"$HOME/.local/bin/walter-desktop" autostart enable || true
cat <<'EOT'

Installed. macOS will ask for Microphone access the first time (allow it for Python/Terminal).
For hands you also need: brew install cliclick, and System Settings > Privacy & Security >
Accessibility AND Screen Recording for Python.
Next: on the wall say "add a face called Office", then here:
  ~/.local/bin/walter-desktop pair http://<wall-ip>:8765 <code> --name Office
  ~/.local/bin/walter-desktop run
EOT
