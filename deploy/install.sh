#!/usr/bin/env bash
# Turn a fresh Debian (12 "bookworm" or 13 "trixie") laptop into Walter.
#
# Run as root (or with sudo) from a checkout of this repo:
#     sudo ./deploy/install.sh
#
# What it does:
#   - creates a `walter` user that auto-logs in on the laptop's screen
#   - installs a tiny Wayland kiosk (cage) + Chromium showing only Walter's face
#   - installs Python deps, a Piper voice and Whisper (all speech is local)
#   - runs Walter's brain as a systemd user service that restarts on failure
#   - stops the laptop from sleeping, even with the lid closed
#
# Safe to run again after `git pull` to update.
#
# Beta bootstrap (no checkout needed; see docs/BETA-INSTALL.md):
#     curl -fsSL https://releases.wallter.app/install.sh | sudo bash -s -- --invite CODE
# (channel defaults to https://releases.wallter.app in bootstrap mode only; a re-run
# from a checkout never adds a channel by itself) downloads latest.json from the channel, checks the release's sha256 AND its
# Ed25519 signature against the public key embedded below, unpacks it to
# ~walter/.walter-releases/<version> and carries on from there.
#
# Options:
#   --invite CODE            beta invite (opaque; stored for the Cloud Link broker, not checked yet)
#   --channel URL            update channel (same as WALTER_UPDATE_URL; bootstrap
#                            default: https://releases.wallter.app)
#   --permission-mode MODE   brain permission mode for a NEW config: acceptEdits (safer,
#                            Walter can't run arbitrary commands, no self-programming) or
#                            bypassPermissions (default: full power as the walter user)
#   --bootstrap              force bootstrap mode
set -euo pipefail
INSTALLER_VERSION=2

WALTER_USER="${WALTER_USER:-walter}"
VOICE="${WALTER_VOICE:-en_GB-alan-medium}"
REPO_SRC="$(cd "$(dirname "$0")/.." 2>/dev/null && pwd || true)"
INVITE="${WALTER_INVITE:-}"
UPDATE_URL="${WALTER_UPDATE_URL:-}"
PERMISSION_MODE="${WALTER_PERMISSION_MODE:-}"
BOOTSTRAP=0
DEFAULT_UPDATE_URL="https://releases.wallter.app"
while [[ $# -gt 0 ]]; do
  case $1 in
    --invite) INVITE="${2:-}"; shift 2 ;;
    --channel) UPDATE_URL="${2:-}"; shift 2 ;;
    --permission-mode) PERMISSION_MODE="${2:-}"; shift 2 ;;
    --bootstrap) BOOTSTRAP=1; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
case "$PERMISSION_MODE" in ""|acceptEdits|bypassPermissions) ;;
  *) echo "--permission-mode must be acceptEdits or bypassPermissions" >&2; exit 2 ;; esac
[[ "$INVITE" =~ ^[A-Za-z0-9._-]{0,128}$ ]] || { echo "invite code: letters, digits, . _ - only" >&2; exit 2; }
[[ -f "$REPO_SRC/walter/config.py" && -f "$REPO_SRC/deploy/install.sh" ]] || BOOTSTRAP=1

if [[ $EUID -ne 0 ]]; then echo "Please run with sudo." >&2; exit 1; fi
say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

# Public half of the release signing key (same as deploy/release-public.pem; a test
# keeps them in sync). The private half never leaves the release machine.
RELEASE_PUBLIC_PEM='-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAPQ5QkAXysR0g5FxXgqVdJQpqC7NxQeLpltDaATR02NA=
-----END PUBLIC KEY-----'

if (( BOOTSTRAP )); then
  # New installs get the signed Cloudflare channel unless told otherwise.
  UPDATE_URL="${UPDATE_URL:-$DEFAULT_UPDATE_URL}"
  [[ "$UPDATE_URL" == https://* ]] || { echo "the update channel must be https://" >&2; exit 2; }
  say "Fetching the latest signed Walter release"
  export DEBIAN_FRONTEND=noninteractive
  command -v curl >/dev/null && command -v openssl >/dev/null && command -v python3 >/dev/null \
    || { apt-get update && apt-get install -y --no-install-recommends curl ca-certificates openssl python3; }
  BT=$(mktemp -d /tmp/walter-bootstrap.XXXXXX)
  trap 'rm -rf "$BT"' EXIT
  base="${UPDATE_URL%/}/"
  curl -fsSL --max-time 30 -o "$BT/latest.json" "${base}latest.json"
  read -r REL_VERSION REL_TAR REL_SHA REL_SIG < <(python3 -I - "$BT/latest.json" "$base" <<'PY'
import json, re, sys
from urllib.parse import urljoin
d = json.load(open(sys.argv[1])); b = sys.argv[2]
v, sha = str(d["version"]), str(d["sha256"]).lower()
assert re.fullmatch(r"\d+\.\d+\.\d+", v) and re.fullmatch(r"[0-9a-f]{64}", sha)
print(v, urljoin(b, d["tarball_url"]), sha, urljoin(b, d["signature_url"]))
PY
)
  [[ -n "${REL_SIG:-}" ]] || { echo "bad latest.json from the channel" >&2; exit 1; }
  curl -fsSL --max-time 900 -o "$BT/r.tar.gz" "$REL_TAR"
  curl -fsSL --max-time 60 -o "$BT/r.tar.gz.sig" "$REL_SIG"
  printf '%s\n' "$RELEASE_PUBLIC_PEM" > "$BT/pub.pem"
  [[ "$(sha256sum "$BT/r.tar.gz" | cut -d' ' -f1)" == "$REL_SHA" ]] || { echo "REFUSED: sha256 mismatch" >&2; exit 1; }
  openssl pkeyutl -verify -rawin -pubin -inkey "$BT/pub.pem" -in "$BT/r.tar.gz" -sigfile "$BT/r.tar.gz.sig" >/dev/null \
    || { echo "REFUSED: the release signature does not verify" >&2; exit 1; }
  echo "release $REL_VERSION verified (sha256 + signature)"
  mkdir -p "$BT/x"
  tar -xzf "$BT/r.tar.gz" -C "$BT/x" --no-same-owner --no-same-permissions
  SRC_DIR=/var/lib/walter/src
  mkdir -p /var/lib/walter && rm -rf "$SRC_DIR" && mv "$BT/x/walter-$REL_VERSION" "$SRC_DIR"
  chown -R root:root "$SRC_DIR" && chmod -R go-w "$SRC_DIR"
  args=(); [[ -n "$INVITE" ]] && args+=(--invite "$INVITE")
  [[ -n "$PERMISSION_MODE" ]] && args+=(--permission-mode "$PERMISSION_MODE")
  args+=(--channel "$UPDATE_URL")
  exec env WALTER_RELEASE_VERSION="$REL_VERSION" "$SRC_DIR/deploy/install.sh" "${args[@]}"
fi
[[ -z "${WALTER_RELEASE_VERSION:-}" && -f "$REPO_SRC/VERSION" ]] && grep -qE '^[0-9]+\.[0-9]+\.[0-9]+$' "$REPO_SRC/VERSION" \
  && WALTER_RELEASE_VERSION=$(cat "$REPO_SRC/VERSION")

export DEBIAN_FRONTEND=noninteractive
say "Installing system packages"
apt-get update
apt-get install -y --no-install-recommends \
  git curl ca-certificates sudo openssh-server avahi-daemon libnss-mdns \
  python3 python3-venv python3-dev build-essential \
  libportaudio2 alsa-utils espeak-ng \
  pipewire pipewire-pulse pipewire-alsa wireplumber \
  cage foot wlr-randr chromium firefox-esr fonts-dejavu-core \
  libgl1-mesa-dri libegl-mesa0 libgles2 libgbm1 mesa-vulkan-drivers \
  dbus-user-session network-manager polkitd wpasupplicant iw wireless-regdb
# Wi-Fi firmware lives in Debian's non-free-firmware component, which cloud images and
# some installs don't enable: best effort, one by one, so a missing one never takes the
# core packages above down with it (it used to: the fallback then skipped the kiosk,
# python3-venv and PipeWire).
for fw in firmware-iwlwifi firmware-realtek firmware-atheros firmware-brcm80211; do
  if apt-cache show "$fw" >/dev/null 2>&1; then
    apt-get install -y --no-install-recommends "$fw" || echo "note: $fw not installed (Wi-Fi firmware, optional)"
  else
    echo "note: $fw not available (enable non-free-firmware in apt sources if this Wi-Fi card needs it)"
  fi
done

say "Creating user '$WALTER_USER'"
if ! id "$WALTER_USER" &>/dev/null; then
  adduser --disabled-password --gecos "Walter the Wall Buddy" "$WALTER_USER"
fi
usermod -aG audio,video,input,render "$WALTER_USER" 2>/dev/null || usermod -aG audio,video,input "$WALTER_USER"
WALTER_HOME="$(getent passwd "$WALTER_USER" | cut -d: -f6)"
REPO="$WALTER_HOME/walter"
# Fresh install (no Walter config yet)? Then the first-run wizard runs once (walter/firstrun.py).
FRESH=0; [[ -f "$WALTER_HOME/.walter/config.toml" ]] || FRESH=1

say "Installing Walter's code into $REPO"
RELROOT="$WALTER_HOME/.walter-releases"
if [[ -L "$REPO" ]]; then
  : # release-managed (signed channel): deploy/update.sh installs and switches versions
elif [[ -n "${WALTER_RELEASE_VERSION:-}" && ! -e "$REPO" ]]; then
  # a signed release: versioned dir + current symlink, so updates switch atomically
  # and the previous release stays for `walterctl update rollback`
  sudo -H -u "$WALTER_USER" mkdir -p "$RELROOT"
  rm -rf "${RELROOT:?}/$WALTER_RELEASE_VERSION"
  cp -a "$REPO_SRC" "$RELROOT/$WALTER_RELEASE_VERSION"
  ln -sfn ../venv "$RELROOT/$WALTER_RELEASE_VERSION/.venv"
  ln -sfn "$WALTER_RELEASE_VERSION" "$RELROOT/current"
  ln -sfn .walter-releases/current "$REPO"
  chown -R "$WALTER_USER:$WALTER_USER" "$RELROOT" && chown -h "$WALTER_USER:$WALTER_USER" "$REPO"
  echo "$WALTER_RELEASE_VERSION" > /var/lib/walter/release-version 2>/dev/null \
    || { mkdir -p /var/lib/walter && echo "$WALTER_RELEASE_VERSION" > /var/lib/walter/release-version; }
elif [[ "$REPO_SRC" != "$REPO" ]]; then
  if [[ -d "$REPO/.git" ]]; then
    : # already installed: his code is updated by deploy/update.sh (merged into his own history)
  else
    cp -a "$REPO_SRC" "$REPO"
    chown -R "$WALTER_USER:$WALTER_USER" "$REPO"
  fi
fi
# Walter commits his own changes to himself, so he needs a git identity.
sudo -H -u "$WALTER_USER" git config --global user.name "Walter"
sudo -H -u "$WALTER_USER" git config --global user.email "walter@localhost"
sudo -H -u "$WALTER_USER" git config --global --add safe.directory "$REPO"
sudo -H -u "$WALTER_USER" git config --global init.defaultBranch main
if [[ ! -d "$REPO/.git" && ! -L "$REPO" ]]; then
  # installed from the USB stick: start his history here so he can undo himself
  sudo -H -u "$WALTER_USER" bash -c "cd '$REPO' && git init -q && git add -A && git commit -qm 'Walter, as installed'"
  # where updates come from (GitHub "owner/repo"): WALTER_UPDATE_REPO in the environment or
  # /etc/walter/update.conf; without it Walter has no origin and deploy/update.sh skips updates
  UPD_REPO="${WALTER_UPDATE_REPO:-}"
  [[ -z "$UPD_REPO" && -f /etc/walter/update.conf ]] && UPD_REPO=$(. /etc/walter/update.conf; echo "${WALTER_UPDATE_REPO:-}")
  [[ -n "$UPD_REPO" ]] && sudo -H -u "$WALTER_USER" git -C "$REPO" remote add origin "https://github.com/$UPD_REPO.git"
fi

say "Python environment + speech models (this takes a while the first time)"
sudo -H -u "$WALTER_USER" bash -c "
  set -e
  cd '$REPO'
  if [ -L .venv ]; then python3 -m venv \"\$(readlink -f .venv)\"; else python3 -m venv .venv; fi  # releases share ../venv
  .venv/bin/pip install --upgrade pip wheel
  .venv/bin/pip install -r requirements.txt
  mkdir -p ~/.walter/voices
  [ -f ~/.walter/voices/$VOICE.onnx ] || .venv/bin/python -m piper.download_voices $VOICE --download-dir ~/.walter/voices
  if [ ! -f ~/.walter/config.toml ]; then
    cp config.example.toml ~/.walter/config.toml
    # Safe beta defaults are in config.example.toml (camera, recognition, cloud speech off).
    [ -z '$PERMISSION_MODE' ] || sed -i 's#^permission_mode = .*#permission_mode = \"$PERMISSION_MODE\"#' ~/.walter/config.toml
  fi
  # Walter's own folder: personality, memory, skills (never overwrites his edits)
  mkdir -p ~/.walter/home && cp -rn home/. ~/.walter/home/
  sed -i 's#^piper_model = .*#piper_model = \"~/.walter/voices/$VOICE.onnx\"#' ~/.walter/config.toml
  # face / voice recognition models (only used if enabled in config; checksummed)
  .venv/bin/python -m walter.people fetch || echo 'recognition models not downloaded (retry: .venv/bin/python -m walter.people fetch)'
  # pre-download the speech recogniser so the first conversation is fast
  .venv/bin/python -c 'from faster_whisper import WhisperModel; WhisperModel(\"base.en\", device=\"cpu\", compute_type=\"int8\")'
"

say "Claude Code"
if ! sudo -H -u "$WALTER_USER" bash -lc 'command -v claude' &>/dev/null; then
  sudo -H -u "$WALTER_USER" bash -lc 'curl -fsSL https://claude.ai/install.sh | bash'
fi

say "Systemd user service for Walter's brain"
# NB: "install -d -o" only chowns the last directory, so create each level
# as walter; a root-owned ~/.config made the browser crash at startup.
sudo -H -u "$WALTER_USER" mkdir -p "$WALTER_HOME/.config/systemd/user" "$WALTER_HOME/.cache" "$WALTER_HOME/.local/share"
sed "s#@REPO@#$REPO#g" "$REPO/deploy/walter.service" > "$WALTER_HOME/.config/systemd/user/walter.service"
chown "$WALTER_USER:$WALTER_USER" "$WALTER_HOME/.config/systemd/user/walter.service"
install -d -o "$WALTER_USER" -g "$WALTER_USER" "$WALTER_HOME/.config/systemd/user/default.target.wants"
ln -sf ../walter.service "$WALTER_HOME/.config/systemd/user/default.target.wants/walter.service"
chown -h "$WALTER_USER:$WALTER_USER" "$WALTER_HOME/.config/systemd/user/default.target.wants/walter.service"
# live worker status for the face (face/dashboard-workers.json), separate from Walter
sed "s#@REPO@#$REPO#g" "$REPO/deploy/walter-workers-live.service" > "$WALTER_HOME/.config/systemd/user/walter-workers-live.service"
chown "$WALTER_USER:$WALTER_USER" "$WALTER_HOME/.config/systemd/user/walter-workers-live.service"
ln -sf ../walter-workers-live.service "$WALTER_HOME/.config/systemd/user/default.target.wants/walter-workers-live.service"
chown -h "$WALTER_USER:$WALTER_USER" "$WALTER_HOME/.config/systemd/user/default.target.wants/walter-workers-live.service"
# live CPU/memory gauges for the build cockpit (face/dashboard-gauges.json)
sed "s#@REPO@#$REPO#g" "$REPO/deploy/walter-gauges-live.service" > "$WALTER_HOME/.config/systemd/user/walter-gauges-live.service"
chown "$WALTER_USER:$WALTER_USER" "$WALTER_HOME/.config/systemd/user/walter-gauges-live.service"
ln -sf ../walter-gauges-live.service "$WALTER_HOME/.config/systemd/user/default.target.wants/walter-gauges-live.service"
chown -h "$WALTER_USER:$WALTER_USER" "$WALTER_HOME/.config/systemd/user/default.target.wants/walter-gauges-live.service"
# build view for conversation mode (face/dashboard-build.json), a 30 s timer separate from Walter
for u in walter-build.service walter-build.timer; do
  sed "s#@REPO@#$REPO#g" "$REPO/deploy/$u" > "$WALTER_HOME/.config/systemd/user/$u"
  chown "$WALTER_USER:$WALTER_USER" "$WALTER_HOME/.config/systemd/user/$u"
done
install -d -o "$WALTER_USER" -g "$WALTER_USER" "$WALTER_HOME/.config/systemd/user/timers.target.wants"
ln -sf ../walter-build.timer "$WALTER_HOME/.config/systemd/user/timers.target.wants/walter-build.timer"
chown -h "$WALTER_USER:$WALTER_USER" "$WALTER_HOME/.config/systemd/user/timers.target.wants/walter-build.timer"
loginctl enable-linger "$WALTER_USER"

say "Echo cancellation, so people can talk over Walter (barge-in)"
# PipeWire's WebRTC echo canceller + ALSA names for it (deploy/pipewire/README.md).
sudo -H -u "$WALTER_USER" mkdir -p "$WALTER_HOME/.config/pipewire/pipewire.conf.d"
sudo -H -u "$WALTER_USER" cp "$REPO/deploy/pipewire/60-walter-echo-cancel.conf" \
  "$WALTER_HOME/.config/pipewire/pipewire.conf.d/60-walter-echo-cancel.conf"
if ! grep -qs walter_aec_mic "$WALTER_HOME/.asoundrc"; then
  sudo -H -u "$WALTER_USER" bash -c "cat '$REPO/deploy/pipewire/asoundrc-walter-aec' >> '$WALTER_HOME/.asoundrc'"
fi

say "First-time setup helpers"
# Walter's setup page may set the system time zone from your phone...
echo "$WALTER_USER ALL=(root) NOPASSWD: /usr/bin/timedatectl set-timezone *" > /etc/sudoers.d/walter-timezone
chmod 440 /etc/sudoers.d/walter-timezone
# ...and hand over a one-time admin password (applied by a root helper that
# Walter can't modify, and only once).
install -d -m 700 -o "$WALTER_USER" -g "$WALTER_USER" /var/lib/walter-setup
install -d -m 755 /var/lib/walter
install -m 755 -o root -g root "$REPO_SRC/deploy/admin-password.sh" /usr/local/sbin/walter-admin-password
ln -sf walter-admin-password /usr/local/sbin/walter-admin
install -m 644 "$REPO_SRC/deploy/walter-admin-password.path" /etc/systemd/system/
install -m 644 "$REPO_SRC/deploy/walter-admin-password.service" /etc/systemd/system/
# Later path to grant/revoke admin rights: a USB stick plugged in by hand
# (physical presence), see docs/admin-access.md.
install -m 644 "$REPO_SRC/deploy/walter-admin-usb@.service" /etc/systemd/system/
install -m 644 "$REPO_SRC/deploy/90-walter-admin-usb.rules" /etc/udev/rules.d/
udevadm control --reload 2>/dev/null || true
systemctl daemon-reload || true
systemctl enable walter-admin-password.path
systemctl start walter-admin-password.path 2>/dev/null || true
systemctl enable avahi-daemon 2>/dev/null || true   # reach him as walter.local

# Firefox is the kiosk's fallback browser: no first-run pages, no updates.
for d in /etc/firefox-esr/policies /etc/firefox/policies; do
  mkdir -p "$d"
  cat > "$d/policies.json" <<'EOF'
{"policies": {"DisableTelemetry": true, "DisableAppUpdate": true, "DontCheckDefaultBrowser": true,
  "OverrideFirstRunPage": "", "OverridePostUpdatePage": "", "NoDefaultBookmarks": true,
  "UserMessaging": {"WhatsNew": false, "ExtensionRecommendations": false, "FeatureRecommendations": false,
                    "UrlbarInterventions": false, "SkipOnboarding": true, "MoreFromMozilla": false}}}
EOF
done

say "Wi-Fi: NetworkManager, so Walter can join networks from his screen"
# Let the walter user (Walter's Wi-Fi panel) manage networks without a password.
mkdir -p /etc/polkit-1/rules.d
cat > /etc/polkit-1/rules.d/50-walter-network.rules <<EOF
polkit.addRule(function(action, subject) {
  if (action.id.indexOf("org.freedesktop.NetworkManager.") == 0 && subject.user == "$WALTER_USER") {
    return polkit.Result.YES;
  }
});
EOF
# Hand the network over from the Debian installer's setup (ifupdown) to
# NetworkManager, keeping a Wi-Fi network chosen during installation.
# Takes effect at the next boot, so this run's network stays up.
if [[ -f /etc/network/interfaces ]] && grep -qE '^\s*iface\s+[^l]' /etc/network/interfaces; then
  SSID=$(awk '/^\s*wpa-ssid/{ $1=""; sub(/^ /,""); print; exit }' /etc/network/interfaces)
  [[ -z "$SSID" ]] && SSID=$(awk '/^\s*wireless-essid/{ $1=""; sub(/^ /,""); print; exit }' /etc/network/interfaces)
  PSK=$(awk '/^\s*wpa-psk/{ $1=""; sub(/^ /,""); print; exit }' /etc/network/interfaces)
  SSID=${SSID%\"}; SSID=${SSID#\"}; PSK=${PSK%\"}; PSK=${PSK#\"}
  if [[ -n "$SSID" ]]; then
    f="/etc/NetworkManager/system-connections/walter-wifi.nmconnection"
    mkdir -p /etc/NetworkManager/system-connections
    rm -f "$f"
    # Let NetworkManager write (and check) the profile itself; hand-written keyfile as a fallback.
    systemctl start NetworkManager 2>/dev/null || true
    if nmcli connection add type wifi con-name walter-wifi ssid "$SSID" \
         connection.autoconnect yes connection.autoconnect-priority 10 \
         $( [[ -n "$PSK" ]] && echo wifi-sec.key-mgmt wpa-psk ) \
         ${PSK:+wifi-sec.psk "$PSK"} save yes >/dev/null 2>&1; then
      :
    else
    {
      printf '[connection]\nid=walter-wifi\nuuid=%s\ntype=wifi\nautoconnect=true\n\n[wifi]\nmode=infrastructure\nssid=%s\n\n' "$(cat /proc/sys/kernel/random/uuid)" "$SSID"
      [[ -n "$PSK" ]] && printf '[wifi-security]\nkey-mgmt=wpa-psk\npsk=%s\n\n' "$PSK"
      printf '[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n'
    } > "$f"
    chmod 600 "$f"
    fi
    echo "Wi-Fi network '$SSID' handed over to NetworkManager"
  fi
  cp /etc/network/interfaces /etc/network/interfaces.before-walter
  printf 'auto lo\niface lo inet loopback\n' > /etc/network/interfaces
fi
systemctl enable NetworkManager 2>/dev/null || true

say "Updates: signed releases (WALTER_UPDATE_URL) or tested versions from GitHub, nightly"
# Root only ever runs files from a root-owned copy of a tested release,
# never Walter's own (editable) copy, so he can't give himself root.
install -m 755 -o root -g root "$REPO_SRC/deploy/update.sh" /usr/local/sbin/walter-update
install -m 644 "$REPO_SRC/deploy/walter-update.service" "$REPO_SRC/deploy/walter-update.timer" /etc/systemd/system/
# Signed-release verification helper + public key, root-owned (Walter can't swap the key).
install -d -m 755 /usr/local/lib/walter /usr/local/share/walter
install -m 644 -o root -g root "$REPO_SRC/tools/release/walter_release.py" /usr/local/lib/walter/walter_release.py
install -m 644 -o root -g root "$REPO_SRC/deploy/release-public.pem" /usr/local/share/walter/release-public.pem
mkdir -p /etc/walter
[[ -f /etc/walter/update.conf ]] || printf '# WALTER_UPDATE=off   # uncomment to stop automatic updates\n# WALTER_UPDATE_URL=   # signed release channel (serves latest.json); empty = no channel\n' > /etc/walter/update.conf
if [[ -n "$UPDATE_URL" ]] && ! grep -q '^WALTER_UPDATE_URL=' /etc/walter/update.conf; then
  printf 'WALTER_UPDATE_URL=%q\n' "$UPDATE_URL" >> /etc/walter/update.conf
fi
# Beta invite code: opaque, kept for the Cloud Link broker later (not validated yet).
if [[ -n "$INVITE" ]]; then
  printf '%s\n' "$INVITE" > /etc/walter/invite
  chown "root:$WALTER_USER" /etc/walter/invite && chmod 640 /etc/walter/invite
fi
# First-run wizard: only on a fresh install. Existing Walters are marked done so it never shows.
install -d -m 750 -o "$WALTER_USER" -g "$WALTER_USER" /etc/walter/firstrun
if [[ "$FRESH" == 1 && ! -f /etc/walter/firstrun-done ]]; then
  sudo -u "$WALTER_USER" touch /etc/walter/firstrun/pending
elif [[ ! -f /etc/walter/firstrun/pending ]]; then
  touch /etc/walter/firstrun-done
fi
# Walter may ask for an update (he can't change what it runs).
echo "$WALTER_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start walter-update.service, /usr/bin/systemctl start --no-block walter-update.service" > /etc/sudoers.d/walter-update
chmod 440 /etc/sudoers.d/walter-update
# Which tested version is installed (first install: the one on the USB stick).
[[ -f /var/lib/walter/upstream ]] || { [[ -f "$REPO_SRC/VERSION" ]] && cp "$REPO_SRC/VERSION" /var/lib/walter/upstream; } || true
systemctl daemon-reload
systemctl enable walter-update.timer 2>/dev/null || true

say "Auto-login on the screen + kiosk"
mkdir -p /etc/systemd/system/getty@tty1.service.d
cat > /etc/systemd/system/getty@tty1.service.d/autologin.conf <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $WALTER_USER --noclear %I \$TERM
EOF
PROFILE="$WALTER_HOME/.bash_profile"
if [[ ! -f "$PROFILE" ]]; then
  # bash skips ~/.profile once ~/.bash_profile exists, so keep loading it
  # (it puts ~/.local/bin, where Claude Code lives, on the PATH)
  echo '[ -f ~/.profile ] && . ~/.profile' > "$PROFILE"
fi
if ! grep -q walter-kiosk "$PROFILE"; then
  cat >> "$PROFILE" <<EOF

# Walter: on the laptop's own screen, become the wall buddy.
if [ "\$(tty)" = "/dev/tty1" ] && [ -z "\$WAYLAND_DISPLAY" ]; then
  exec $REPO/deploy/walter-kiosk.sh
fi
EOF
fi
chown "$WALTER_USER:$WALTER_USER" "$PROFILE"

# Everything in walter's home belongs to walter (belt and braces).
chown -R "$WALTER_USER:$WALTER_USER" "$WALTER_HOME/.config" "$WALTER_HOME/.cache" "$WALTER_HOME/.local" 2>/dev/null || true

say "Never sleep, ignore the lid"
mkdir -p /etc/systemd/logind.conf.d
cat > /etc/systemd/logind.conf.d/walter.conf <<EOF
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
IdleAction=ignore
EOF
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
# boot straight through, no menu
if [[ -f /etc/default/grub ]] && ! grep -q '^GRUB_TIMEOUT=1$' /etc/default/grub; then
  sed -i 's/^GRUB_TIMEOUT=.*/GRUB_TIMEOUT=1/' /etc/default/grub
  update-grub 2>/dev/null || true
fi

# Mark the install complete: walter-update.service only runs once this exists. The USB
# path's firstboot.sh wrote it, but the curl bootstrap never did, so beta homes never updated.
[[ -f /var/lib/walter/installed ]] || date -Is > /var/lib/walter/installed

say "Done!"
cat <<EOF

Next: reboot. Walter shows a QR code on his screen; scan it with your
phone (same Wi-Fi). Sign in to Claude on Walter's own screen with Claude's
official sign-in (press Enter there, keyboard plugged in), or use your own
Anthropic API key. Walter never sees your Claude password or login code.
Then say "Hey Walter!".
(Optional: tweak ~/.walter/config.toml as $WALTER_USER.)

Handy:  sudo -iu $WALTER_USER journalctl --user -u walter -f      (logs)
        sudo -iu $WALTER_USER walter/bin/walterctl status
EOF
