#!/usr/bin/env bash
# Build a signed Walter release from the committed tree (nothing is uploaded).
#
#   tools/release/make-release.sh [VERSION] [--out DIR] [--key FILE] [--base-url URL]
#
#   VERSION     X.Y.Z (default: next patch after the newest vX.Y.Z tag, or 0.1.0)
#   --out       output directory (default: dist/)
#   --key       Ed25519 signing key (default: ~/.walter/secrets/release-signing.key;
#               created, mode 600, if missing, and deploy/release-public.pem is then written)
#   --base-url  absolute URL prefix for latest.json (default: URLs relative to latest.json)
#   --allow-dirty  build even with uncommitted changes (they are NOT included either way)
#
# Output (DIR/):
#   latest.json                                  what the update channel serves
#   releases/X.Y.Z/walter-X.Y.Z.tar.gz           clean `git archive` of HEAD minus dev-only files
#   releases/X.Y.Z/walter-X.Y.Z.tar.gz.sig       detached Ed25519 signature of the tarball
#   releases/X.Y.Z/manifest.json (+ .sig)        version, date, sha256, min installer, changelog
#
# Upload the whole DIR to the channel (e.g. an R2 bucket) with latest.json LAST.
# Tagging (git tag vX.Y.Z) is left to you so the next changelog starts from here.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
HELPER="$HERE/walter_release.py"
OUT="$REPO/dist"
KEY="${WALTER_RELEASE_KEY:-$HOME/.walter/secrets/release-signing.key}"
PUB="$REPO/deploy/release-public.pem"
BASE_URL=""
VERSION=""
ALLOW_DIRTY=0
# Bump when releases need a newer deploy/update.sh or install.sh than older devices have.
MIN_INSTALLER_VERSION=2

while [[ $# -gt 0 ]]; do
  case $1 in
    --out) OUT="$2"; shift 2 ;;
    --key) KEY="$2"; shift 2 ;;
    --base-url) BASE_URL="$2"; shift 2 ;;
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) VERSION="$1"; shift ;;
  esac
done

cd "$REPO"
LAST_TAG=$(git describe --tags --abbrev=0 --match 'v[0-9]*.[0-9]*.[0-9]*' 2>/dev/null || true)
if [[ -z "$VERSION" ]]; then
  if [[ -n "$LAST_TAG" ]]; then
    IFS=. read -r a b c <<<"${LAST_TAG#v}"; VERSION="$a.$b.$((c + 1))"
  else
    VERSION=0.1.0
  fi
fi
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must be X.Y.Z, got '$VERSION'" >&2; exit 2; }
if [[ $ALLOW_DIRTY == 0 && -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "uncommitted changes; commit first (or --allow-dirty: only committed files are packed anyway)" >&2; exit 1
fi

# Signing key lives OUTSIDE the repo. Never printed.
if [[ ! -f "$KEY" ]]; then
  python3 -I "$HELPER" genkey "$KEY" "$PUB"
  echo "NOTE: new public key written to deploy/release-public.pem: commit it (and update install.sh's copy)."
fi
[[ "$(stat -c %a "$KEY")" == 600 ]] || chmod 600 "$KEY"
TMPPUB=$(mktemp); trap 'rm -f "$TMPPUB"' EXIT
python3 -I "$HELPER" pubkey "$KEY" "$TMPPUB"
if ! cmp -s "$TMPPUB" "$PUB"; then
  echo "the signing key does not match deploy/release-public.pem; refusing" >&2; exit 1
fi

NAME="walter-$VERSION"
REL="$OUT/releases/$VERSION"
[[ -e "$REL" ]] && { echo "$REL already exists (versions are immutable; pick a new one)" >&2; exit 1; }
STAGE=$(mktemp -d); trap 'rm -rf "$STAGE" "$TMPPUB"' EXIT
mkdir -p "$STAGE/$NAME"
COMMIT=$(git rev-parse HEAD)
git archive "$COMMIT" | tar -x -C "$STAGE/$NAME"

# Dev/CI-only files that aren't needed on a device (docs are kept).
for p in tests .github .githooks iso tools/tunnel-broker dist .gitignore .gitattributes; do
  rm -rf "${STAGE:?}/$NAME/$p"
done
find "$STAGE/$NAME" -name '__pycache__' -prune -exec rm -rf {} +
echo "$VERSION" > "$STAGE/$NAME/VERSION"

# Personal data check: the owner's private words must not ship (names, town, emails).
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

mkdir -p "$REL"
TAR="$REL/$NAME.tar.gz"
MTIME=$(git log -1 --format=%cI "$COMMIT")
tar --sort=name --mtime="$MTIME" --owner=0 --group=0 --numeric-owner \
    -C "$STAGE" -cf - "$NAME" | gzip -n -9 > "$TAR"

# Changelog: commit subjects since the last release tag.
CHANGES="$STAGE/changelog.txt"
if [[ -n "$LAST_TAG" ]]; then range="$LAST_TAG..$COMMIT"; else range="$COMMIT"; fi
git log --no-merges --format='- %s' -n 200 "$range" | sed 's/ \[skip ci\]$//' > "$CHANGES"
if [[ -f "$PRIV" ]] && grep -v -e '^\s*#' -e '^\s*$' "$PRIV" | grep -qiwFf - "$CHANGES"; then
  echo "changelog mentions private words; replacing it with a generic line" >&2
  echo "- Walter $VERSION" > "$CHANGES"
fi

python3 -I "$HELPER" manifest "$VERSION" "$TAR" "$MIN_INSTALLER_VERSION" "$CHANGES" "$COMMIT" "$REL/manifest.json"
python3 -I "$HELPER" sign "$KEY" "$TAR" "$TAR.sig"
python3 -I "$HELPER" sign "$KEY" "$REL/manifest.json" "$REL/manifest.json.sig"
python3 -I "$HELPER" verify "$PUB" "$TAR" "$TAR.sig" "$(python3 -I "$HELPER" sha256 "$TAR")" >/dev/null \
  || { echo "self-check of the signature failed" >&2; exit 1; }
python3 -I "$HELPER" latest-from-manifest "$REL/manifest.json" "$OUT/latest.json" "$BASE_URL"

echo "built $TAR ($(du -h "$TAR" | cut -f1))"
echo "manifest: $REL/manifest.json   channel index: $OUT/latest.json"
echo "nothing was uploaded; tag it with: git tag v$VERSION"
