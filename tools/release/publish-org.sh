#!/usr/bin/env bash
# Publish a CLEAN copy of Walter (no history, no secrets, no personal data) to the
# org repo qiiver-llc/walter (meant to be public: everything here must be safe to show).
#
#   tools/release/publish-org.sh [--squash] [--dry-run] [--dir DIR]
#
#   default    export HEAD; if org main exists, add ONE new commit on top of it
#              (append mode: org history = one commit per sync, never ours)
#   --squash   replace org main with a single new root commit (force-with-lease)
#   --dry-run  build + scrub-check only, do not push
#   WALTER_PUBLISH_BROKER=1  also publish tools/tunnel-broker (left out by default)
#
# Development stays in ~/walter (origin = the private dev repo). This script
# never touches ~/walter's remotes or config. Token: ~/.walter/secrets/github-qiiver-llc.token,
# handed to git via a GIT_ASKPASS helper that reads the file (never in argv or .git/config).
set -euo pipefail

SRC="$(cd "$(dirname "$0")/../.." && pwd)"
URL="https://github.com/qiiver-llc/walter.git"
TOKEN_FILE="$HOME/.walter/secrets/github-qiiver-llc.token"
WORDS="$HOME/.walter/private-words.txt"
# second, semantic list (businesses, places, schools, teams, stations, coordinates...):
# one term per line, whole-word, case-insensitive; a line "re:PATTERN" is an extended regex
# (e.g. rounded coordinates). Never printed: a hit names the file, not the term.
WORDS_EXTRA="$HOME/.walter/private-words-extra.txt"
AUTHOR_NAME="Qiiver LLC"; AUTHOR_EMAIL="hello@wallter.app"
SQUASH=0; DRY=0; OUT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --squash) SQUASH=1 ;; --dry-run) DRY=1 ;; --dir) OUT="$2"; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac; shift
done

if [ -n "$(git -C "$SRC" status --porcelain)" ]; then
  echo "publish-org: $SRC has uncommitted changes; commit first (only HEAD is exported)." >&2; exit 1
fi
VERSION="$(cat "$SRC/VERSION" 2>/dev/null || git -C "$SRC" describe --tags --abbrev=0 2>/dev/null | sed "s/^v//" || echo 0.1)"
SRC_SHA="$(git -C "$SRC" rev-parse --short HEAD)"

OUT="${OUT:-$(mktemp -d /tmp/walter-publish.XXXXXX)}"
mkdir -p "$OUT"
[ -z "$(ls -A "$OUT")" ] || { echo "publish-org: $OUT is not empty" >&2; exit 1; }

# askpass helper (lives outside the repo, deleted on exit)
ASK="$(mktemp /tmp/walter-askpass.XXXXXX)"; trap 'rm -f "$ASK"' EXIT
cat > "$ASK" <<EOF
#!/bin/sh
case "\$1" in Username*) echo x-access-token ;; *) cat "$TOKEN_FILE" | tr -d '[:space:]' ;; esac
EOF
chmod 700 "$ASK"
gitorg() { GIT_ASKPASS="$ASK" GIT_TERMINAL_PROMPT=0 git -c credential.helper= "$@"; }

# 1. export the tree
git -C "$SRC" archive --format=tar HEAD | tar -x -C "$OUT"

# 2. strip anything that must never ship
cd "$OUT"
find . \( -name node_modules -o -name .venv -o -name venv -o -name .wrangler -o -name dist \
          -o -name __pycache__ -o -name .pytest_cache \) -prune -exec rm -rf {} +
find . -type f \( -name '.env' -o -name '.env.*' -o -name '*.key' -o -name '*.p12' \
          -o -name '*.iso' -o -name '*.tar.gz' -o -name '*.tgz' -o -name '*.sig' \) -delete
find . -type f -name '*.pem' ! -path './deploy/release-public.pem' -delete
find home/journal -mindepth 1 ! -name .keep -exec rm -rf {} + 2>/dev/null || true
# Explicit exclusions: internal or hosted pieces that are not part of the public device code.
# Edit this list (or set WALTER_PUBLISH_BROKER=1) to change what ships.
EXCLUDE=(
  tools/tunnel-broker           # hosted Cloud Link broker (closed / FSL per the licensing plan)
  docs/SUPPORT-PLAYBOOK.md      # internal support process
  ops                           # internal ops docs, letters, plans (if ever committed)
  journal                       # dated notes (if ever committed at the top level)
  .githooks/pre-push.local
  tests/fixtures/nsaa           # recorded pages of one real school's season (identifies the owner's town)
  tests/test_hs_sports.py       # the tests that read them (the dev repo keeps both)
)
for p in "${EXCLUDE[@]}"; do
  [ "$p" = tools/tunnel-broker ] && [ "${WALTER_PUBLISH_BROKER:-0}" = 1 ] && continue
  rm -rf -- "./$p"
done

# 3. scrub check (must be ZERO hits)
fail=0
hit() { echo "SCRUB: $1"; fail=1; }
if [ -s "$WORDS" ]; then
  r=$(grep -rniwIF -f <(grep -v '^[[:space:]]*$' "$WORDS") --exclude-dir=.git -l . || true)
  [ -z "$r" ] || hit "private words in: $r"
  r=$(find . -path ./.git -prune -o -print | grep -iwF -f <(grep -v '^[[:space:]]*$' "$WORDS") || true)
  [ -z "$r" ] || hit "private words in file names: $r"
fi
if [ -s "$WORDS_EXTRA" ]; then
  fixed=$(grep -vE '^[[:space:]]*(#|$)|^re:' "$WORDS_EXTRA" || true)
  regex=$(grep -E '^re:' "$WORDS_EXTRA" | sed 's/^re://' || true)
  if [ -n "$fixed" ]; then
    r=$(grep -rliwIF -f <(printf '%s\n' "$fixed") --exclude-dir=.git . || true)
    [ -z "$r" ] || hit "owner-specific terms (private-words-extra) in: $r"
    r=$(find . -path ./.git -prune -o -print | grep -iwF -f <(printf '%s\n' "$fixed") || true)
    [ -z "$r" ] || hit "owner-specific terms (private-words-extra) in file names: $r"
  fi
  if [ -n "$regex" ]; then
    r=$(grep -rliIE -f <(printf '%s\n' "$regex") --exclude-dir=.git --exclude-dir=vendor . || true)
    [ -z "$r" ] || hit "owner-specific patterns (private-words-extra) in: $r"
  fi
fi
r=$(grep -rnIE 'github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9_-]{24,}|sk-ant-[a-z0-9]{2,8}-[A-Za-z0-9_-]{20,}|\bre_[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|Bearer [A-Za-z0-9._-]{24,}|"accessToken" *: *"[^"]{20,}' --exclude-dir=.git . \
    | grep -vE 'sk-test-FAKE|abcdefghijklmnop' || true)   # obvious test fixtures
[ -z "$r" ] || hit "token-like strings:
$r"
# Google / Cloudflare / Slack / Stripe credentials and IDs (the fakes in tests say so)
r=$(grep -rnIE --exclude-dir=.git 'GOCSPX-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{35}|ya29\.[A-Za-z0-9_-]{20,}|[0-9]{6,}-[a-z0-9]{24,}\.apps\.googleusercontent\.com|@group\.calendar\.google\.com|xox[abprs]-[A-Za-z0-9-]{10,}|[sr]k_live_[A-Za-z0-9]{10,}|[0-9a-f-]{36}\.cfargotunnel\.com|(account|zone|tunnel)[A-Za-z_]*["'"'"' =:-]+[0-9a-f]{32}\b' . \
    | grep -viE 'not-a-real|FAKE' || true)
[ -z "$r" ] || hit "Google/Cloudflare/other credentials or IDs:
$r"
# the actual secret values this machine holds must never appear (file names only, never values)
SECRETS_DIR="$HOME/.walter/secrets"
if [ -d "$SECRETS_DIR" ]; then
  vals=$(find "$SECRETS_DIR" -maxdepth 1 -type f -size -64k -exec cat {} + 2>/dev/null \
         | tr ' ",{}' '\n\n\n\n\n' | grep -E '^[A-Za-z0-9+/=._:-]{16,}$' | grep -vE '^-+(BEGIN|END)|://|^https?:|\.(com|app|net|org)$|^[a-z0-9]*_[a-z0-9_]*$' | sort -u || true)
  if [ -n "$vals" ]; then
    r=$(grep -rlIF --exclude-dir=.git -f <(printf '%s\n' "$vals") . || true)
    [ -z "$r" ] || hit "a value from ~/.walter/secrets appears in: $r"
  fi
fi
# this install's own public hostnames (tunnel, tailnet) from its config
CFG="$HOME/.walter/config.toml"
if [ -f "$CFG" ]; then
  for v in $(grep -oE 'https?://[A-Za-z0-9.-]+' "$CFG" | sed -E 's#https?://##' | sort -u \
             | grep -E '\.(wallter\.app|ts\.net|trycloudflare\.com)$' || true); do
    r=$(grep -rliwIF --exclude-dir=.git -- "$v" . || true); [ -z "$r" ] || hit "this install's hostname in: $r"
  done
fi
# saved Wi-Fi network names
if command -v nmcli >/dev/null 2>&1; then
  while IFS= read -r v; do
    [ ${#v} -ge 4 ] || continue; case "$v" in walter-wifi|Hotspot|preconfigured) continue ;; esac
    r=$(grep -rliwIF --exclude-dir=.git -- "$v" . || true); [ -z "$r" ] || hit "a saved Wi-Fi name appears in: $r"
  done < <(nmcli -t -f NAME,TYPE connection show 2>/dev/null | awk -F: '$2 ~ /wireless/ {print $1}')
fi
r=$(grep -rnIE --exclude-dir=.git -- '-----BEGIN [A-Z ]*PRIVATE KEY-----' . || true)
[ -z "$r" ] || hit "private key:
$r"
# emails: only clearly generic/product addresses allowed
r=$(grep -rnoIE --exclude-dir=.git --exclude-dir=vendor '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}' . \
    | grep -viE '@(wallter\.app|example\.(com|org|net|invalid)(\.json)?|[a-z0-9.-]+\.invalid|anthropic\.com|users\.noreply\.github\.com|localhost|walter\.local)$|noreply@|@[0-9.]+$|\.(png|jpg|svg|js|css|service)$' \
    | grep -viE ':(git|user|you|me|name|someone|person|email|your[a-z._-]*|first\.last)@' || true)
[ -z "$r" ] || hit "email addresses (check they are generic):
$r"
# this house's IPs / MACs / hostname
for v in $(ip -o -4 addr show scope global 2>/dev/null | awk '{split($4,a,"/"); print a[1]}') \
         $(ip -o -6 addr show scope global 2>/dev/null | awk '{split($4,a,"/"); print a[1]}') \
         $(ip route 2>/dev/null | awk '/^default/{print $3}') \
         $(cat /sys/class/net/*/address 2>/dev/null | grep -v '^00:00:00:00:00:00$'); do
  case "$v" in 192.168.1.1|192.168.0.1|10.0.0.1) continue ;; esac   # textbook defaults used in tests
  r=$(grep -rliF --exclude-dir=.git -- "$v" . || true); [ -z "$r" ] || hit "host address $v in: $r"
done
h=$(hostname); case "$h" in walter|localhost|debian) ;; *) r=$(grep -rliwIF --exclude-dir=.git -- "$h" . || true); [ -z "$r" ] || hit "hostname $h in: $r";; esac
# private files the pre-push hook also blocks
r=$(find . -path ./.git -prune -o -type f \( -name config.toml -o -name .credentials.json -o -name usage.json -o -name card.json -o -name brain.json -o -name tasks.json -o -name hosts.yml \) -print)
[ -z "$r" ] || hit "private files: $r"
if grep -qE '^[[:space:]]*[-*+][[:space:]]|^[[:space:]]*[0-9]+\.[[:space:]]' home/memory.md 2>/dev/null; then hit "home/memory.md is not the blank template"; fi
[ $fail -eq 0 ] || { echo "publish-org: scrub check FAILED, nothing pushed ($OUT kept for inspection)." >&2; exit 1; }
echo "scrub check: 0 hits ($(find . -path ./.git -prune -o -type f -print | wc -l) files)"

# 4. commit (append on top of org main, or a fresh root with --squash)
git init -q -b main .
git config user.name "$AUTHOR_NAME"; git config user.email "$AUTHOR_EMAIL"
git remote add org "$URL"
MSG="Walter $VERSION (clean history)"
if [ $SQUASH -eq 0 ] && gitorg ls-remote --exit-code org refs/heads/main >/dev/null 2>&1; then
  gitorg fetch -q org main
  git reset -q --soft FETCH_HEAD          # keep exported tree, parent = org main
  MSG="Walter $VERSION (sync from $SRC_SHA)"
fi
git add -A
git commit -q -m "$MSG" --author="$AUTHOR_NAME <$AUTHOR_EMAIL>" || { echo "publish-org: nothing changed since last sync"; exit 0; }

# 5. run the repo's own pre-push hook over what will be pushed
if [ $SQUASH -eq 0 ] && git rev-parse -q --verify FETCH_HEAD >/dev/null; then
  RANGE_REMOTE=$(git rev-parse FETCH_HEAD)
else RANGE_REMOTE=0000000000000000000000000000000000000000; fi
printf 'refs/heads/main %s refs/heads/main %s\n' "$(git rev-parse HEAD)" "$RANGE_REMOTE" \
  | bash "$SRC/.githooks/pre-push" org "$URL" \
  || { echo "publish-org: pre-push hook FAILED, nothing pushed." >&2; exit 1; }
echo "pre-push hook: OK"

if [ $DRY -eq 1 ]; then echo "dry run: built $OUT ($(git rev-parse --short HEAD)), not pushed"; exit 0; fi

# 6. push
if [ $SQUASH -eq 1 ]; then
  cur=$(gitorg ls-remote org refs/heads/main | cut -f1)
  gitorg push --force-with-lease="main:${cur}" org main
else
  gitorg push org main
fi
echo "pushed $(git rev-parse --short HEAD) to $URL (from $SRC_SHA); export in $OUT"
