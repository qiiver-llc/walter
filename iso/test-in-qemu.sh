#!/usr/bin/env bash
# End-to-end test of the USB installer in a virtual machine:
#   1. boot the ISO, let the unattended Debian install run
#   2. boot the installed disk: first-boot setup installs Walter, then reboots
#   3. boot again: Walter must come up and serve his setup page on :8766
# Screenshots of every phase land in $OUT_DIR (they are uploaded by CI).
#
#   ./iso/test-in-qemu.sh walter-installer.iso bios|uefi
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ISO="$(realpath "$1")"
FIRMWARE="${2:-bios}"
OUT_DIR="${OUT_DIR:-$PWD/qemu-test}"
mkdir -p "$OUT_DIR"
cd "$OUT_DIR"

DISK=disk.qcow2
MON="$OUT_DIR/monitor.sock"
qemu-img create -f qcow2 "$DISK" 24G >/dev/null

# Like a real used laptop: the disk already holds an old system (a filesystem
# and a swap partition), which the installer must wipe and replace.
if [[ "${OLD_OS_DISK:-1}" == 1 ]]; then
  sudo modprobe nbd max_part=8
  sudo qemu-nbd -c /dev/nbd2 "$DISK"; sleep 2
  printf 'label: dos\n,16G,L\n,,E\n,2G,S\n' | sudo sfdisk -q /dev/nbd2 >/dev/null
  sleep 1; sudo partprobe /dev/nbd2 2>/dev/null || true; sleep 1
  sudo mkfs.ext4 -q -F /dev/nbd2p1 && sudo mkswap /dev/nbd2p5 >/dev/null
  echo "old system on the test disk: $(sudo sfdisk -l /dev/nbd2 | grep -c '^/dev/') partitions"
  sudo qemu-nbd -d /dev/nbd2 >/dev/null
fi

FW_ARGS=()
if [[ "$FIRMWARE" == uefi ]]; then
  first() { for f in "$@"; do [[ -f "$f" ]] && { echo "$f"; return 0; }; done; return 1; }
  CODE=$(first /usr/share/OVMF/OVMF_CODE_4M.fd /usr/share/OVMF/OVMF_CODE.fd) || { echo "OVMF not found" >&2; exit 1; }
  VARS=$(first /usr/share/OVMF/OVMF_VARS_4M.fd /usr/share/OVMF/OVMF_VARS.fd) || { echo "OVMF vars not found" >&2; exit 1; }
  echo "UEFI firmware: $CODE"
  cp "$VARS" vars.fd
  FW_ARGS=(-drive "if=pflash,format=raw,readonly=on,file=$CODE" -drive "if=pflash,format=raw,file=vars.fd")
fi

ACCEL=(-accel kvm -cpu "${QEMU_CPU:-host}")
VGA="${QEMU_VGA:-std}"
echo "VM: cpu=${QEMU_CPU:-host} vga=$VGA"
[[ -w /dev/kvm ]] || { echo "No KVM: this would take many hours." >&2; exit 1; }

shot() {  # shot <name>
  echo "screendump $OUT_DIR/$1.ppm" | socat - "UNIX-CONNECT:$MON" >/dev/null 2>&1 || return 0
  sleep 1
  python3 -c "from PIL import Image; Image.open('$OUT_DIR/$1.ppm').save('$OUT_DIR/$1.png')" 2>/dev/null \
    && rm -f "$OUT_DIR/$1.ppm"
}

run_vm() {  # run_vm <phase> <timeout-min> [extra qemu args...]
  local phase=$1 limit=$2; shift 2
  rm -f "$MON"
  qemu-system-x86_64 "${ACCEL[@]}" -m 4096 -smp 2 "${FW_ARGS[@]}" \
    -drive "file=$DISK,if=virtio,format=qcow2" \
    -netdev user,id=n0,hostfwd=tcp::18766-:8766 -device virtio-net-pci,netdev=n0 \
    -vga "$VGA" -display none -no-reboot \
    -monitor "unix:$MON,server,nowait" "$@" &
  local pid=$! i=0
  echo "[$phase] VM started (pid $pid), up to $limit min"
  while kill -0 $pid 2>/dev/null; do
    sleep 30; i=$((i + 1))
    (( i % 4 == 0 )) && shot "$(printf '%s-%03d' "$phase" "$i")"
    if (( i >= limit * 2 )); then
      echo "[$phase] timed out"; shot "$phase-timeout"; kill $pid; return 1
    fi
  done
  wait $pid || true
  echo "[$phase] VM exited after ~$((i / 2)) min"
}

# 1. unattended Debian install (ends with a reboot, which exits QEMU)
run_vm install 90 -cdrom "$ISO" -boot order=d

# 2. first boot: installs Walter, then reboots
run_vm firstboot 75

# CI debugging only: let the harness SSH into this throwaway VM as root
# (key injected into the test disk; the real installer never has this).
if [[ "${VM_DEBUG:-0}" == 1 ]]; then
  ssh-keygen -q -t ed25519 -N '' -f "$OUT_DIR/id" <<<y >/dev/null
  sudo modprobe nbd max_part=8
  sudo qemu-nbd -c /dev/nbd1 "$DISK"; sleep 2
  for p in /dev/nbd1p*; do
    mkdir -p /tmp/vmroot; sudo mount "$p" /tmp/vmroot 2>/dev/null || continue
    if [[ -f /tmp/vmroot/etc/debian_version ]]; then
      sudo install -d -m 700 /tmp/vmroot/root/.ssh
      sudo tee -a /tmp/vmroot/root/.ssh/authorized_keys < "$OUT_DIR/id.pub" >/dev/null
      sudo chmod 600 /tmp/vmroot/root/.ssh/authorized_keys
      echo "debug key injected into $p"
    fi
    sudo umount /tmp/vmroot
  done
  sudo qemu-nbd -d /dev/nbd1 >/dev/null
fi

# 3. normal boot: wait for Walter's setup page
rm -f "$MON"
qemu-system-x86_64 "${ACCEL[@]}" -m 4096 -smp 2 "${FW_ARGS[@]}" \
  -drive "file=$DISK,if=virtio,format=qcow2" \
  -netdev user,id=n0,hostfwd=tcp::18766-:8766,hostfwd=tcp::10022-:22 -device virtio-net-pci,netdev=n0 \
  -vga "$VGA" -display none -monitor "unix:$MON,server,nowait" &
PID=$!
trap 'kill $PID 2>/dev/null || true; wait $PID 2>/dev/null || true' EXIT   # never leave the VM running
ok=0
for i in $(seq 1 60); do
  sleep 10
  if curl -fsS --max-time 5 http://127.0.0.1:18766/setup/ping 2>/dev/null | grep -q '"setup": true'; then
    ok=1; break
  fi
done
echo "setup page answering: $ok"
# Walter's face must actually be on the screen (the kiosk may need a few
# tries to find graphics settings that work, so give it up to 10 minutes).
face=0
for i in $(seq 1 40); do
  sleep 15
  shot walter-running
  if python3 "$HERE/describe-screenshot.py" walter-running.png --expect-walter; then face=1; break; fi
done
(( ok && face )) || ok=0
# The disk-safety settings (format without "write zeroes"/discard) must be in place.
# The updater must not offer a rollback; a stick built without WALTER_UPDATE_URL /
# WALTER_UPDATE_REPO has no update source at all, which is fine too.
if [[ "${VM_DEBUG:-0}" == 1 ]]; then
  echo "::group::disk safety settings"
  if ssh -i "$OUT_DIR/id" -p 10022 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=20 root@127.0.0.1 'set -x; cat /var/log/walter-mkfs.log && grep -q "ext4.*noinit_itable" /etc/fstab \
        && [ "$(systemctl is-enabled fstrim.timer 2>&1)" = masked ] \
        && [ "$(cat /sys/fs/ext4/*/extent_max_zeroout_kb | sort -u)" = 0 ] && cat /proc/cmdline \
        && [ "$(systemctl is-enabled walter-update.timer)" = enabled ] && cat /var/lib/walter/upstream \
        && /usr/local/sbin/walter-update --check | grep -E "up to date|no update source"' 2>&1; then
    echo "disk safety settings: ok"
  else
    echo "disk safety settings: MISSING"; ok=0
  fi
  echo "::endgroup::"
fi
if [[ "${VM_DEBUG:-0}" == 1 && $face == 0 ]]; then
  echo "::group::inside the VM (diagnostics)"
  ssh -i "$OUT_DIR/id" -p 10022 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=20 root@127.0.0.1 'cat > /tmp/diag.sh && bash /tmp/diag.sh </dev/null' < "$HERE/vm-diagnose.sh" 2>&1 | tail -n 300
  echo "::endgroup::"
fi
echo "info registers" | socat - "UNIX-CONNECT:$MON" >/dev/null 2>&1 || true
kill $PID 2>/dev/null || true
wait $PID 2>/dev/null || true

if (( ok )); then
  echo "PASS: Walter is up and waiting to be set up."
else
  echo "FAIL: Walter didn't come up properly (see setup-page and screenshot lines above)."
  exit 1
fi
