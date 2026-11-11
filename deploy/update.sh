#!/usr/bin/env bash
# Keep Walter up to date from GitHub, safely.
#
#   sudo walter-update [--now] [--check]     (installed as /usr/local/sbin/walter-update)
#
# - Only installs versions that passed the full test (installed end-to-end
#   in BIOS and UEFI virtual machines): the commit behind the
#   "installer-latest" release.
# - Keeps Walter's own changes to himself: the new version is merged into
#   his git history. If it can't be merged cleanly, nothing changes.
# - Runs nightly and shortly after boot (walter-update.timer); Walter can
#   also start it: `sudo systemctl start walter-update`.
#
# Settings (optional) in /etc/walter/update.conf:
#   WALTER_UPDATE_REPO=OWNER/REPO               # GitHub "owner/repo" to update from
#   WALTER_UPDATE=off            # turn automatic updates off
# Without WALTER_UPDATE_REPO the GitHub repo of Walter's own "origin" remote is used.
#
# Signed release channel (beta devices; no GitHub needed):
#   WALTER_UPDATE_URL=https://downloads.example/walter/   # serves latest.json
# When set, the channel is used INSTEAD of GitHub (empty = the GitHub path above, unchanged).
# latest.json -> {version, tarball_url, sha256, signature_url, min_installer_version}.
# The tarball's sha256 AND its Ed25519 signature (against the root-owned copy of
# deploy/release-public.pem) must match before anything is unpacked. Releases go to
# ~walter/.walter-releases/<version>; ~walter/walter -> .walter-releases/current;
# the previous release is kept for `walterctl update rollback`.
#   sudo walter-update --force    also update while background workers are running
set -uo pipefail
UPDATER_VERSION=2

WALTER_USER="${WALTER_USER:-walter}"
UPDATE_REPO=""
WALTER_UPDATE="on"
[[ -f /etc/walter/update.conf ]] && . /etc/walter/update.conf
WALTER_HOME="$(getent passwd "$WALTER_USER" | cut -d: -f6)"
REPO="$WALTER_HOME/walter"
UPDATE_REPO="${WALTER_UPDATE_REPO:-$UPDATE_REPO}"
if [[ -z "$UPDATE_REPO" ]]; then
  origin=$(sudo -H -u "$WALTER_USER" git -C "$REPO" remote get-url origin 2>/dev/null || true)
  UPDATE_REPO=$(sed -nE 's#^(https://github\.com/|git@github\.com:)([^/]+/[^/]+)$#\2#p' <<<"$origin")
  UPDATE_REPO="${UPDATE_REPO%.git}"
fi
LOG=/var/log/walter-update.log
NOW=0; CHECK=0; FORCE=0
for a in "$@"; do case $a in --now) NOW=1 ;; --check) CHECK=1 ;; --force) FORCE=1 ;; esac; done

log() { echo "$(date -Is) $*" | tee -a "$LOG"; }
as_walter() { sudo -H -u "$WALTER_USER" "$@"; }
g() { as_walter git -C "$REPO" "$@"; }

[[ $EUID -eq 0 ]] || { echo "Please run with sudo." >&2; exit 1; }
if [[ "$WALTER_UPDATE" == off && $NOW == 0 ]]; then log "automatic updates are off"; exit 0; fi

# ---------------------------------------------------------------- signed release channel
HELPER=/usr/local/lib/walter/walter_release.py
PUBKEY=/usr/local/share/walter/release-public.pem
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
[[ -f "$HELPER" ]] || HELPER="$SELF_DIR/../tools/release/walter_release.py"   # running from a checkout
[[ -f "$PUBKEY" ]] || PUBKEY="$SELF_DIR/release-public.pem"
RELROOT="$WALTER_HOME/.walter-releases"

running_workers() {   # background workers Walter has running right now (walterctl tasks)
  python3 -I - "$WALTER_HOME/.walter/tasks.json" <<'PY' 2>/dev/null || echo 0
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    print(0); raise SystemExit
items = d.get("items", d) if isinstance(d, dict) else d
print(sum(1 for i in items if isinstance(i, dict) and i.get("status") == "running"))
PY
}

restart_walter() {   # the same safe path the app uses: the user service, not a reboot
  local uid; uid=$(id -u "$WALTER_USER")
  sudo -H -u "$WALTER_USER" XDG_RUNTIME_DIR="/run/user/$uid" systemctl --user restart walter \
    || log "couldn't restart Walter's service (it picks the new release up at the next start)"
}

channel_update() {
  local url="${WALTER_UPDATE_URL%/}/latest.json" tmp
  tmp=$(mktemp -d /tmp/walter-update.XXXXXX); trap 'rm -rf "$tmp"' RETURN
  curl -fsSL --max-time 30 -o "$tmp/latest.json" "$url" || { log "can't reach the update channel"; return 1; }
  local vars; vars=$(python3 -I "$HELPER" latest "$tmp/latest.json" "${WALTER_UPDATE_URL%/}/") \
    || { log "bad latest.json from the channel"; return 1; }
  local REL_VERSION REL_TARBALL_URL REL_SHA256 REL_SIGNATURE_URL REL_MIN_INSTALLER_VERSION
  eval "$vars"
  if (( REL_MIN_INSTALLER_VERSION > UPDATER_VERSION )); then
    log "release $REL_VERSION needs a newer installer ($REL_MIN_INSTALLER_VERSION > $UPDATER_VERSION); re-run install.sh"
    return 1
  fi
  local cur=""; [[ -L "$RELROOT/current" ]] && cur=$(readlink "$RELROOT/current")
  [[ -z "$cur" ]] && cur=$(cat /var/lib/walter/release-version 2>/dev/null || true)
  if ! python3 -I "$HELPER" newer "$REL_VERSION" "$cur"; then
    log "up to date (installed $cur, channel has $REL_VERSION)"; return 0
  fi
  log "new release on the channel: $REL_VERSION (installed: ${cur:-none})"
  (( CHECK )) && return 0
  local n; n=$(running_workers)
  if (( n > 0 && FORCE == 0 )); then
    log "Walter has $n background worker(s) running; not updating now (sudo walter-update --force to override)"
    return 0
  fi

  curl -fsSL --max-time 900 -o "$tmp/release.tar.gz" "$REL_TARBALL_URL" || { log "download failed"; return 1; }
  curl -fsSL --max-time 60 -o "$tmp/release.tar.gz.sig" "$REL_SIGNATURE_URL" || { log "signature download failed"; return 1; }
  local why; why=$(python3 -I "$HELPER" verify "$PUBKEY" "$tmp/release.tar.gz" "$tmp/release.tar.gz.sig" "$REL_SHA256") \
    || { log "REFUSED release $REL_VERSION: $why"; return 1; }
  log "release $REL_VERSION verified (sha256 + signature)"

  install -d -o "$WALTER_USER" -g "$WALTER_USER" "$RELROOT"
  local stage="$RELROOT/$REL_VERSION.new" top
  rm -rf "$stage"
  top=$(python3 -I "$HELPER" extract "$tmp/release.tar.gz" "$stage") || { log "couldn't unpack"; rm -rf "$stage"; return 1; }
  [[ "$(cat "$top/VERSION" 2>/dev/null)" == "$REL_VERSION" ]] || { log "release VERSION file mismatch"; rm -rf "$stage"; return 1; }
  # keep a root-owned pristine copy for running the installer (Walter can't edit it)
  local src=/var/lib/walter/src
  rm -rf "$src.new" && cp -a "$top" "$src.new" && chown -R root:root "$src.new" && chmod -R go-w "$src.new"

  local dir="$RELROOT/$REL_VERSION"
  rm -rf "$dir" && mv "$top" "$dir" && rm -rf "$stage"
  ln -sfn ../venv "$dir/.venv"
  chown -R "$WALTER_USER:$WALTER_USER" "$dir"
  # Python packages into the shared venv (only when requirements changed)
  if ! sudo -H -u "$WALTER_USER" bash -c "
      set -e; cd '$RELROOT'
      [ -x venv/bin/python ] || python3 -m venv venv
      want=\$(sha256sum '$dir/requirements.txt' | cut -d' ' -f1)
      [ \"\$(cat venv/.requirements.sha256 2>/dev/null)\" = \"\$want\" ] || {
        venv/bin/pip install -q -r '$dir/requirements.txt' && echo \"\$want\" > venv/.requirements.sha256; }
    " >>"$LOG" 2>&1; then
    log "pip install failed; keeping the current release"; rm -rf "$dir" "$src.new"; return 1
  fi
  # checks BEFORE switching: everything compiles and the basics import
  if ! sudo -H -u "$WALTER_USER" bash -c "cd '$dir' && .venv/bin/python -m py_compile walter/*.py bin/walterctl \
        && .venv/bin/python -c 'import walter.config, walter.app_restart' \
        && .venv/bin/python bin/walterctl --help >/dev/null" >>"$LOG" 2>&1; then
    log "release $REL_VERSION failed its health check; keeping the current release"
    rm -rf "$dir" "$src.new"; return 1
  fi

  # First channel update on a git install: keep that copy as a release so rollback works.
  if [[ -d "$REPO" && ! -L "$REPO" ]]; then
    local legacy="legacy-$(date +%Y%m%d%H%M%S)"
    mv "$REPO" "$RELROOT/$legacy" && sudo -H -u "$WALTER_USER" ln -sfn "$legacy" "$RELROOT/current"
    ln -sfn .walter-releases/current "$REPO" && chown -h "$WALTER_USER:$WALTER_USER" "$REPO"
  fi
  [[ -L "$REPO" ]] || { ln -sfn .walter-releases/current "$REPO"; chown -h "$WALTER_USER:$WALTER_USER" "$REPO"; }
  sudo -H -u "$WALTER_USER" python3 -I "$HELPER" switch "$RELROOT" "$REL_VERSION" >>"$LOG" || { log "switch failed"; return 1; }
  rm -rf "$src" && mv "$src.new" "$src"
  if ! WALTER_NONINTERACTIVE=1 WALTER_FROM_UPDATER=1 "$src/deploy/install.sh" >>"$LOG" 2>&1; then
    log "installer failed; rolling back to the previous release"
    sudo -H -u "$WALTER_USER" python3 -I "$HELPER" rollback "$RELROOT" >>"$LOG" || true
    return 1
  fi
  echo "$REL_VERSION" > /var/lib/walter/release-version
  sudo -H -u "$WALTER_USER" python3 -I "$HELPER" prune "$RELROOT" >>"$LOG" 2>&1 || true
  log "release $REL_VERSION installed; restarting Walter"
  restart_walter
}

if [[ -n "${WALTER_UPDATE_URL:-}" ]]; then
  channel_update; exit $?
fi

# ---------------------------------------------------------------- GitHub path (unchanged)
[[ -d "$REPO/.git" ]] || { log "no git history in $REPO, can't update"; exit 1; }

# Which version passed the tests?
[[ -n "$UPDATE_REPO" ]] || { log "no update source (set WALTER_UPDATE_REPO in /etc/walter/update.conf)"; exit 0; }
api="https://api.github.com/repos/$UPDATE_REPO"
code=$(curl -sSL --max-time 20 -o /tmp/walter-release.json -w '%{http_code}' "$api/releases/tags/installer-latest") \
  || { log "can't reach GitHub"; exit 1; }
if [[ "$code" == 404 ]]; then log "up to date (no tested release published right now)"; exit 0; fi
[[ "$code" == 200 ]] || { log "GitHub answered $code"; exit 1; }
rel=$(cat /tmp/walter-release.json); rm -f /tmp/walter-release.json
NEW=$(printf '%s' "$rel" | python3 -c 'import json,sys; print(json.load(sys.stdin)["target_commitish"])') || { log "bad release info"; exit 1; }
[[ "$NEW" =~ ^[0-9a-f]{40}$ ]] || { log "release has no commit id ($NEW)"; exit 1; }

STATE=/var/lib/walter/upstream
INSTALLED=$(cat "$STATE" 2>/dev/null || cat "$REPO/VERSION" 2>/dev/null || true)
if [[ -n "$INSTALLED" && "$NEW" == "$INSTALLED"* ]]; then
  log "up to date ($INSTALLED)"; exit 0
fi

# Fetch it (and the history, so the version he was installed from can be found).
g remote get-url origin >/dev/null 2>&1 || g remote add origin "https://github.com/$UPDATE_REPO.git"
g remote set-url origin "https://github.com/$UPDATE_REPO.git"
g fetch -q origin "+refs/heads/*:refs/remotes/origin/*" || { log "git fetch failed"; exit 1; }
g cat-file -e "$NEW^{commit}" 2>/dev/null || { log "commit $NEW not found on GitHub"; exit 1; }

# Never go backwards: the tested release can be older than what's installed
# (e.g. installed from a newer USB stick while the next release is still testing).
INSTALLED_C=$(g rev-parse --verify -q "${INSTALLED:-none}^{commit}" || true)
if [[ -n "$INSTALLED_C" ]] && g merge-base --is-ancestor "$NEW" "$INSTALLED_C"; then
  log "up to date (installed ${INSTALLED:0:7} is newer than or equal to the tested release ${NEW:0:7})"; exit 0
fi
if g merge-base --is-ancestor "$NEW" HEAD 2>/dev/null; then
  log "up to date (${NEW:0:7} is already part of Walter's code)"; exit 0
fi
log "new tested version: $NEW (installed: ${INSTALLED:-unknown})"
(( CHECK )) && exit 0

# Common ancestor: from earlier updates, or the version on the USB stick.
BASE=$(g merge-base HEAD "$NEW" 2>/dev/null || true)
if [[ -z "$BASE" && -n "$INSTALLED" ]]; then
  BASE=$(g rev-parse --verify -q "$INSTALLED^{commit}" || true)
fi
[[ -n "$BASE" ]] || { log "can't tell which version is installed; not updating"; exit 1; }

# Don't merge into half-finished edits Walter is making right now.
if [[ -n "$(g status --porcelain --untracked-files=no)" ]]; then
  log "Walter has uncommitted changes; saving them first"
  g commit -qam "Walter's work in progress (saved before update)" || { log "couldn't save his changes"; exit 1; }
fi

OLD=$(g rev-parse HEAD)
TREE=$(g merge-tree --write-tree --merge-base="$BASE" HEAD "$NEW" 2>>"$LOG") || {
  log "the update conflicts with Walter's own changes; keeping the current version"; exit 1; }
TREE=$(printf '%s\n' "$TREE" | head -n1)
MSG="Update to tested version ${NEW:0:7} from GitHub"
COMMIT=$(g commit-tree "$TREE" -p HEAD -p "$NEW" -m "$MSG") || { log "commit failed"; exit 1; }
g read-tree -u -m HEAD "$COMMIT" && g update-ref -m "$MSG" HEAD "$COMMIT" "$OLD" \
  || { log "couldn't switch to the new version"; g reset -q --hard "$OLD"; exit 1; }
log "merged ${NEW:0:7} into Walter's code; running the tested installer"

# Run the installer from a clean, root-owned copy of the tested version.
SRC=/var/lib/walter/src
rm -rf "$SRC.new" && mkdir -p "$SRC.new"
if g archive "$NEW" | tar -x -C "$SRC.new" && chown -R root:root "$SRC.new" && chmod -R go-w "$SRC.new"; then
  git -C "$REPO" rev-parse --short "$NEW" > "$SRC.new/VERSION" 2>/dev/null || echo "${NEW:0:7}" > "$SRC.new/VERSION"
  rm -rf "$SRC" && mv "$SRC.new" "$SRC"
else
  log "couldn't unpack the new version"; g reset -q --hard "$OLD"; exit 1
fi
if WALTER_NONINTERACTIVE=1 "$SRC/deploy/install.sh" >>"$LOG" 2>&1; then
  echo "$NEW" > "$STATE"
  log "update installed; restarting"
  systemctl reboot
else
  log "installer failed; going back to the previous version of Walter's code"
  g reset -q --hard "$OLD"
  exit 1
fi
