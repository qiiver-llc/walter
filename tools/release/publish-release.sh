#!/usr/bin/env bash
# Build, sign, check and publish a Walter release to the update channel
# (Cloudflare Pages project "walter-releases" at https://releases.wallter.app).
#
#   tools/release/publish-release.sh [VERSION] [--dry-run] [--setup] [--keep N]
#
#   VERSION    X.Y.Z (default: make-release.sh's choice, next patch after the newest tag)
#   --dry-run  build + assemble the site dir, but don't deploy (prints where it is)
#   --setup    first create/attach the Pages project, custom domain and its CNAME
#   --keep N   releases kept on the channel (default 5; older ones stay in the local mirror)
#
# Steps: refuse VERSION <= the channel's current version; make-release.sh (refuses private
# words, signs with ~/.walter/secrets/release-signing.key); verify the signature with the
# repo's deploy/release-public.pem; assemble the site from the local mirror
# (~/.walter/releases-mirror); wrangler pages deploy; download latest.json + tarball +
# signatures over https and verify them with the updater's code; tag vVERSION locally.
# The Cloudflare token is read from ~/.walter/secrets/cloudflare.token into the
# environment only. Nothing is pushed to GitHub. See docs/RELEASES.md.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
PY="$HERE/publish_release.py"
BASE_URL="${WALTER_RELEASES_URL:-https://releases.wallter.app}"
PROJECT="${WALTER_RELEASES_PROJECT:-walter-releases}"
ACCOUNT_FILE="${CLOUDFLARE_ACCOUNT_FILE:-$HOME/.walter/secrets/cloudflare.account}"
ACCOUNT_ID="${CLOUDFLARE_ACCOUNT_ID:-$(tr -d '[:space:]' < "$ACCOUNT_FILE" 2>/dev/null || true)}"   # never hard-code it
ZONE="${WALTER_RELEASES_ZONE:-wallter.app}"
KEY="${WALTER_RELEASE_KEY:-$HOME/.walter/secrets/release-signing.key}"
TOKFILE="${CLOUDFLARE_TOKEN_FILE:-$HOME/.walter/secrets/cloudflare.token}"
MIRROR="${WALTER_RELEASES_MIRROR:-$HOME/.walter/releases-mirror}"
WRANGLER="${WRANGLER:-$HOME/.walter/tools/wrangler/node_modules/.bin/wrangler}"
PUB="$REPO/deploy/release-public.pem"
VERSION="" DRY=0 SETUP=0 KEEP=5
while [[ $# -gt 0 ]]; do
  case $1 in
    --dry-run) DRY=1; shift ;;
    --setup) SETUP=1; shift ;;
    --keep) KEEP="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) VERSION="$1"; shift ;;
  esac
done
cd "$REPO"
if [[ -z "$VERSION" ]]; then
  LAST_TAG=$(git describe --tags --abbrev=0 --match 'v[0-9]*.[0-9]*.[0-9]*' 2>/dev/null || true)
  if [[ -n "$LAST_TAG" ]]; then IFS=. read -r a b c <<<"${LAST_TAG#v}"; VERSION="$a.$b.$((c + 1))"; else VERSION=0.1.0; fi
fi
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "version must be X.Y.Z" >&2; exit 2; }

load_token() { [[ -n "$ACCOUNT_ID" ]] || { echo "publish-release: set CLOUDFLARE_ACCOUNT_ID or put it in $ACCOUNT_FILE" >&2; exit 2; }; [[ -n "${CLOUDFLARE_API_TOKEN:-}" ]] || export CLOUDFLARE_API_TOKEN="$(cat "$TOKFILE")"; export CLOUDFLARE_ACCOUNT_ID="$ACCOUNT_ID"; }

# 1. never go backwards (also checks the local mirror, in case the channel is unreachable)
CUR=$(python3 -I "$PY" live-version "$BASE_URL")
LOCAL=$( (ls "$MIRROR" 2>/dev/null || true) | sed -nE 's/^manifest-([0-9]+\.[0-9]+\.[0-9]+)\.json$/\1/p' | sort -V | tail -1)
for c in "$CUR" "$LOCAL"; do
  python3 -I "$PY" check-newer "$VERSION" "$c" || { echo "REFUSED: $VERSION is not newer than $c (already published)" >&2; exit 1; }
done
echo "channel has ${CUR:-nothing yet}; publishing $VERSION"

# 2. build + sign (make-release refuses private words and dirty trees)
DIST=$(mktemp -d /tmp/walter-dist.XXXXXX); SITE=$(mktemp -d /tmp/walter-site.XXXXXX)
trap 'rm -rf "$DIST"; (( DRY )) || rm -rf "$SITE"' EXIT
"$HERE/make-release.sh" "$VERSION" --out "$DIST" --key "$KEY"

# 3. verify with the committed public key, then assemble the Pages dir
python3 -I "$HERE/walter_release.py" verify "$PUB" "$DIST/releases/$VERSION/walter-$VERSION.tar.gz" \
  "$DIST/releases/$VERSION/walter-$VERSION.tar.gz.sig" >/dev/null || { echo "REFUSED: signature check failed" >&2; exit 1; }
STAGE_MIRROR="$MIRROR"
if (( DRY )); then STAGE_MIRROR=$(mktemp -d /tmp/walter-mirror.XXXXXX); [[ -d "$MIRROR" ]] && cp -a "$MIRROR/." "$STAGE_MIRROR/"; fi
python3 -I "$PY" assemble "$DIST" "$VERSION" "$STAGE_MIRROR" "$SITE" "$KEY" "$PUB" "$KEEP" | sed 's/^/deploying releases: /'
# belt and braces: nothing private in the site's plain files either
PRIV="$HOME/.walter/private-words.txt"
if [[ -f "$PRIV" ]] && grep -v -e '^\s*#' -e '^\s*$' "$PRIV" | grep -rliIwFf - "$SITE" --include='*.json' --include='*.sh' --include='*.html' --include='_headers'; then
  echo "REFUSED: private words in the site files above" >&2; exit 1
fi
if (( DRY )); then echo "dry run: site assembled in $SITE (not deployed)"; exit 0; fi

# 4. deploy
load_token
if (( SETUP )); then python3 -I "$HERE/cf_pages_setup.py" "$ACCOUNT_ID" "$ZONE" "$PROJECT" "${BASE_URL#https://}"; fi
"$WRANGLER" pages deploy "$SITE" --project-name "$PROJECT" --branch main --commit-dirty=true

# 5. verify from the internet, like a device (retry while the edge catches up)
ok=0
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  if python3 -I "$PY" verify-remote "$BASE_URL" "$PUB" "$VERSION"; then ok=1; break; fi
  sleep 10
done
(( ok )) || { echo "PUBLISHED but the internet check failed; see docs/RELEASES.md (rollback)" >&2; exit 1; }

# 6. local tag (never pushed by this script)
git rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null || git tag -a "v$VERSION" -m "Walter $VERSION"
echo "published $VERSION at $BASE_URL/latest.json"
