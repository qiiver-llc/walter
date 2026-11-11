#!/usr/bin/env bash
# Walter's root helper for admin rights. Installed as
# /usr/local/sbin/walter-admin-password (and the alias walter-admin).
#
#   walter-admin-password          one-time door (run by walter-admin-password.path)
#   walter-admin status            what's set up
#   walter-admin grant             give Walter full admin (passwordless sudo)
#   walter-admin revoke            take it away again
#   walter-admin usb /dev/sdX1     physical-presence request from a USB stick
#                                  (run by udev + walter-admin-usb@.service)
#
# One-time door: during first-time setup Walter's phone page drops the
# owner's choices into /var/lib/walter-setup/ (an admin password in
# "admin-password", and "yes" in "full-admin" if the owner ticked "Give
# Walter full admin rights"). This applies them exactly once. After that the
# door is closed for good, so the AI can't use it to give itself rights later.
#
# Later, if the door was closed (e.g. the password step was skipped), only
# someone with root or with their hands on the laptop can change anything:
#   - as root:   walter-admin grant | revoke
#   - USB stick: a FAT/exFAT stick with a file "walter-admin.txt", plugged in
#     while Walter is running (sticks already in at boot are ignored). See
#     docs/admin-access.md. The file is deleted after use.
#
# Tests run this unprivileged against a fake root: WALTER_ADMIN_ROOT=/tmp/x
# (ignored when running as real root).
set -euo pipefail

if [[ $EUID -eq 0 ]]; then
  ROOT=""
  PATH=/usr/sbin:/usr/bin:/sbin:/bin
else
  ROOT="${WALTER_ADMIN_ROOT:-}"
  [[ -n "$ROOT" ]] || { echo "Please run as root." >&2; exit 1; }
fi
WALTER_USER=walter
REQ_DIR="$ROOT/var/lib/walter-setup"
REQ="$REQ_DIR/admin-password"
REQ_FULL="$REQ_DIR/full-admin"
STATE="$ROOT/var/lib/walter"
DONE="$STATE/admin-password-handled"
LOGF="$STATE/admin.log"           # readable by Walter, never contains secrets
SUDOERS="$ROOT/etc/sudoers.d/90-walter"
SUDO_LINE="$WALTER_USER ALL=(ALL) NOPASSWD: ALL"
USB_FILE=walter-admin.txt
USB_MIN_UPTIME=${WALTER_ADMIN_USB_MIN_UPTIME:-120}
[[ $EUID -eq 0 ]] && USB_MIN_UPTIME=120

note() {
  mkdir -p "$STATE"
  echo "$(date -Is) $*" >> "$LOGF"; chmod 644 "$LOGF"
  echo "$*"
  [[ -z "$ROOT" ]] && command -v logger >/dev/null && logger -t walter-admin "$*" || true
}

grant() {
  mkdir -p "$(dirname "$SUDOERS")"
  local tmp; tmp="$(mktemp "$(dirname "$SUDOERS")/.90-walter.XXXXXX")"   # dot files are ignored by sudo
  printf '# Walter has full admin rights (granted %s by %s).\n# Remove this file to take them away.\n%s\n' \
    "$(date -I)" "$1" "$SUDO_LINE" > "$tmp"
  chmod 0440 "$tmp"
  [[ -z "$ROOT" ]] && chown root:root "$tmp"
  if ! visudo -c -q -f "$tmp" >/dev/null 2>&1; then
    rm -f "$tmp"; note "full admin NOT granted: sudoers check failed"; return 1
  fi
  mv -f "$tmp" "$SUDOERS"
  note "full admin granted ($1)"
}

revoke() {
  if [[ -e "$SUDOERS" ]]; then rm -f "$SUDOERS"; note "full admin revoked ($1)"; else echo "full admin was not granted"; fi
}

set_password() {   # $1 = password (8+ chars)
  printf '%s:%s\n' "$WALTER_USER" "$1" | chpasswd
}

status() {
  if [[ -e "$SUDOERS" ]]; then echo "full admin: yes ($SUDOERS)"; else echo "full admin: no"; fi
  if [[ -e "$DONE" ]]; then echo "setup door: closed ($(head -n1 "$DONE"))"; else echo "setup door: open (first-time setup not finished)"; fi
}

# ------------------------------------------------------- one-time door
setup_door() {
  [[ -f "$REQ" ]] || exit 0
  if [[ -e "$DONE" ]]; then rm -f "$REQ" "$REQ_FULL"; exit 0; fi
  local pw full=no
  pw="$(head -c 512 "$REQ")"
  [[ "$(head -c 8 "$REQ_FULL" 2>/dev/null || true)" == yes* ]] && full=yes
  rm -f "$REQ" "$REQ_FULL"
  mkdir -p "$STATE"
  # Close the door FIRST, so nothing below can be repeated.
  local msg
  if [[ "${#pw}" -ge 8 ]]; then msg="admin password set"; else msg="skipped"; fi
  printf '%s\nfull admin: %s\n' "$msg" "$full" > "$DONE"; chmod 600 "$DONE"
  [[ "${#pw}" -ge 8 ]] && set_password "$pw"
  if [[ $full == yes ]]; then grant "first-time setup"; fi
  note "first-time setup: $msg, full admin: $full"
}

# ------------------------------------------------- USB physical presence
usb() {
  local dev="${1:-}"
  [[ -n "$dev" ]] || { echo "usage: walter-admin usb /dev/sdXN" >&2; exit 2; }
  # Only sticks plugged in by hand while running: one left in at boot could
  # have been written to by Walter himself before a reboot.
  local up; up="$(cut -d. -f1 "$ROOT/proc/uptime")"
  if (( up < USB_MIN_UPTIME )); then echo "ignored $dev: present at boot"; exit 0; fi
  # Only real USB hardware (not loop devices or other virtual disks Walter could create).
  local syspath; syspath="$(udevadm info -q path -n "$dev" 2>/dev/null || true)"
  if [[ "$syspath" != */usb* || "$syspath" == */virtual/* ]]; then echo "ignored $dev: not a USB device"; exit 0; fi
  local fstype; fstype="$(blkid -o value -s TYPE "$dev" 2>/dev/null || true)"
  case "$fstype" in vfat|exfat) ;; *) echo "ignored $dev: filesystem '$fstype'"; exit 0 ;; esac
  mkdir -p "$ROOT/run"
  local mnt; mnt="$(mktemp -d "$ROOT/run/walter-admin-usb.XXXXXX")"
  if ! mount -t "$fstype" -o nosuid,nodev,noexec "$dev" "$mnt"; then rmdir "$mnt" 2>/dev/null || true; exit 0; fi
  local req=""
  if [[ -f "$mnt/$USB_FILE" && ! -L "$mnt/$USB_FILE" ]]; then
    req="$(head -c 2048 "$mnt/$USB_FILE" | tr -d '\r')"
    rm -f "$mnt/$USB_FILE"; sync   # one use per file
  fi
  umount "$mnt" 2>/dev/null || true; rmdir "$mnt" 2>/dev/null || true
  [[ -n "$req" ]] || exit 0
  local line key val did=0
  while IFS= read -r line; do
    [[ "$line" =~ ^[[:space:]]*([A-Za-z ]+)[[:space:]]*:[[:space:]]*(.*)$ ]] || continue
    key="$(echo "${BASH_REMATCH[1]}" | tr 'A-Z' 'a-z' | xargs)"; val="${BASH_REMATCH[2]}"
    val="${val%"${val##*[![:space:]]}"}"
    case "$key" in
      "full admin")
        case "${val,,}" in yes) grant "USB stick"; did=1 ;; no) revoke "USB stick"; did=1 ;; esac ;;
      "password")
        if [[ "${#val}" -ge 8 ]]; then set_password "$val"; note "admin password set (USB stick)"; did=1
        else note "USB password ignored: shorter than 8 characters"; fi ;;
    esac
  done <<< "$req"
  (( did )) || note "USB request had nothing I understood"
}

case "${1:-}" in
  "")      setup_door ;;
  status)  status ;;
  grant)   grant "${2:-by root}" ;;
  revoke)  revoke "${2:-by root}" ;;
  usb)     usb "${2:-}" ;;
  *)       sed -n '2,25p' "$0"; exit 2 ;;
esac
