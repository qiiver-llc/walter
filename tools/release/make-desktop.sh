#!/usr/bin/env bash
# Build the Linux tarball of the Walter desktop face from the committed tree (nothing is uploaded).
#
#   tools/release/make-desktop.sh [VERSION] [--out DIR] [--key FILE] [--no-sign]
#
#   VERSION   X.Y.Z (default: desktop/__init__.py VERSION)
#   --out     output directory (default: dist/desktop)
#   --key     Ed25519 signing key (default: ~/.walter/secrets/release-signing.key); signed the
#             same way as make-release.sh, only if the key exists and matches
#             deploy/release-public.pem (never created here)
#
# Output: DIR/walter-desktop-VERSION-linux.tar.gz (+ .sha256, + .sig when signed)
# Contents: desktop/, the walter/ modules a face needs, bin/walter-desktop, docs, install.sh.
# Verify: python3 tools/release/walter_release.py verify deploy/release-public.pem TAR TAR.sig SHA256
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
HELPER="$HERE/walter_release.py"
OUT="$REPO/dist/desktop"
KEY="${WALTER_RELEASE_KEY:-$HOME/.walter/secrets/release-signing.key}"
PUB="$REPO/deploy/release-public.pem"
SIGN=1
VERSION=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --out) OUT="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --no-sign) SIGN=0; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) VERSION="$1"; shift ;;
  esac
done
cd "$REPO"
[[ -n "$VERSION" ]] || VERSION=$(python3 -I -c "import re;print(re.search(r'VERSION = \"(.+?)\"', open('desktop/__init__.py').read()).group(1))")
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must be X.Y.Z, got '$VERSION'" >&2; exit 2; }

NAME="walter-desktop-$VERSION"
STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
COMMIT=$(git rev-parse HEAD)
FILES=(desktop bin/walter-desktop docs/DESKTOP.md docs/HANDS.md docs/FACES.md LICENSE NOTICE
       walter/__init__.py walter/face_agent.py walter/faces.py walter/devices.py walter/hands.py
       walter/config.py walter/ears.py walter/turn.py
       scripts/desktop/install.sh scripts/desktop/requirements-desktop.txt)
mkdir -p "$STAGE/$NAME"
git archive "$COMMIT" "${FILES[@]}" | tar -x -C "$STAGE/$NAME"
mv "$STAGE/$NAME/scripts/desktop/install.sh" "$STAGE/$NAME/scripts/desktop/requirements-desktop.txt" "$STAGE/$NAME/"
rm -rf "$STAGE/$NAME/scripts" "$STAGE/$NAME/desktop/walter-desktop.spec"
find "$STAGE/$NAME" -name '__pycache__' -prune -exec rm -rf {} +
echo "$VERSION" > "$STAGE/$NAME/VERSION"

# Personal data check (same list as make-release.sh): the owner's private words must not ship.
PRIV="$HOME/.walter/private-words.txt"
if [[ -f "$PRIV" ]]; then
  hits=$(grep -v -e '^\s*#' -e '^\s*$' "$PRIV" | grep -rliIwFf - "$STAGE/$NAME" || true)
  if [[ -n "$hits" ]]; then
    echo "private words found in these files (not shipping):" >&2
    sed "s#^$STAGE/##" <<<"$hits" >&2
    exit 1
  fi
fi
# Second, semantic list (businesses, places, schools, teams, stations; "re:" lines = regex).
PRIVX="$HOME/.walter/private-words-extra.txt"
if [[ -s "$PRIVX" ]]; then
  hits=$( { grep -vE '^\s*(#|$)|^re:' "$PRIVX" | grep -rliIwFf - "$STAGE/$NAME";
            grep -E '^re:' "$PRIVX" | sed 's/^re://' | grep -rliIEf - "$STAGE/$NAME"; } 2>/dev/null | sort -u || true)
  if [[ -n "$hits" ]]; then
    echo "owner-specific terms found in these files (not shipping):" >&2
    sed "s#^$STAGE/##" <<<"$hits" >&2
    exit 1
  fi
fi

mkdir -p "$OUT"
TAR="$OUT/$NAME-linux.tar.gz"
[[ -e "$TAR" ]] && { echo "$TAR already exists (versions are immutable; bump desktop/__init__.py VERSION)" >&2; exit 1; }
MTIME=$(git log -1 --format=%cI "$COMMIT")
tar --sort=name --mtime="$MTIME" --owner=0 --group=0 --numeric-owner -C "$STAGE" -cf - "$NAME" | gzip -n -9 > "$TAR"
SHA=$(python3 -I "$HELPER" sha256 "$TAR")
echo "$SHA  $(basename "$TAR")" > "$TAR.sha256"

if [[ $SIGN == 1 && -f "$KEY" && -f "$PUB" ]]; then
  TMPPUB=$(mktemp); trap 'rm -rf "$STAGE" "$TMPPUB"' EXIT
  python3 -I "$HELPER" pubkey "$KEY" "$TMPPUB"
  if cmp -s "$TMPPUB" "$PUB"; then
    python3 -I "$HELPER" sign "$KEY" "$TAR" "$TAR.sig"
    python3 -I "$HELPER" verify "$PUB" "$TAR" "$TAR.sig" "$SHA" >/dev/null \
      || { echo "self-check of the signature failed" >&2; exit 1; }
    echo "signed: $TAR.sig"
  else
    echo "signing key doesn't match deploy/release-public.pem: left unsigned" >&2
  fi
else
  echo "not signed (no key, or --no-sign)"
fi
echo "built $TAR ($(du -h "$TAR" | cut -f1)); sha256 $SHA"
echo "nothing was uploaded"
