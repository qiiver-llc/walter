#!/bin/bash
# Recipe: a signed-less .pkg for macOS from a PyInstaller build (UNTESTED: needs a Mac).
#   1. python3 -m pip install pyinstaller pystray pillow sounddevice numpy zeroconf
#   2. pyinstaller desktop/walter-desktop.spec          -> dist/walter-desktop/ (one folder)
#   3. this script                                      -> dist/walter-desktop-VERSION.pkg
# Notarising/signing (Developer ID) is a separate, paid step and is NOT done here.
set -euo pipefail
cd "$(dirname "$0")/../../.."
VERSION=$(python3 -c "import re;print(re.search(r'VERSION = \"(.+?)\"', open('desktop/__init__.py').read()).group(1))")
[[ -d dist/walter-desktop ]] || { echo "run: pyinstaller desktop/walter-desktop.spec first" >&2; exit 1; }
ROOT=$(mktemp -d); trap 'rm -rf "$ROOT"' EXIT
mkdir -p "$ROOT/Applications/Walter Desktop"
cp -R dist/walter-desktop/ "$ROOT/Applications/Walter Desktop/"
pkgbuild --root "$ROOT" --identifier app.wallter.walter-desktop --version "$VERSION" \
  --install-location / "dist/walter-desktop-$VERSION.pkg"
echo "built dist/walter-desktop-$VERSION.pkg (unsigned). After install: '/Applications/Walter Desktop/walter-desktop' pair ..."
