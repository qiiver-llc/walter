#!/usr/bin/env bash
# Clean-machine install test: boot a throwaway Debian 13 VM (QEMU/KVM, cloud-init),
# run the REAL beta bootstrap (curl .../install.sh | sudo bash -s -- --invite TEST)
# and collect logs, timings and service checks. Every run starts from a fresh
# qcow2 overlay; the VM is deleted afterwards (unless --keep). See README.md.
#
#   tools/vmtest/run.sh [--keep] [--channel URL] [--invite CODE] [--local-tree]
#                       [--release VER] [--prep CMD] [--post SCRIPT]
#
# default       the real beta path: the published install.sh from the channel, which
#               verifies and installs the signed release.
# --local-tree  test THIS checkout's committed code (git HEAD) laid out exactly like
#               a verified release (/var/lib/walter/src, versioned ~/.walter-releases),
#               at the channel's current version; for checking fixes before a release.
# --release VER install a SPECIFIC signed release from the channel (manifest-VER.json;
#               sha256 + Ed25519 checked in the VM exactly like the bootstrap).
# --prep CMD    shell command run in the VM (as walter, sudo ok) before installing.
# --post SCRIPT host script run after checks, VM still up; gets VM_SSH_PORT, VM_KEY,
#               VM_RUN in its environment (update/rollback/tamper scenarios).
# QEMU runs under nice -n 10 so the host (Walter himself) stays responsive.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
CACHE="${WALTER_VMTEST_DIR:-$HOME/.walter/vmtest}"
CHANNEL="https://releases.wallter.app"
INVITE="TEST"
KEEP=0; LOCAL=0; RELEASE=""; PREP=""; POST=""
while [[ $# -gt 0 ]]; do
  case $1 in
    --keep) KEEP=1; shift ;;
    --channel) CHANNEL="$2"; shift 2 ;;
    --invite) INVITE="$2"; shift 2 ;;
    --local-tree) LOCAL=1; shift ;;
    --release) RELEASE="$2"; shift 2 ;;
    --prep) PREP="$2"; shift 2 ;;
    --post) POST="$(cd "$(dirname "$2")" && pwd)/$(basename "$2")"; shift 2 ;;
    -h|--help) sed -n 2,20p "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
IMG_URL="https://cloud.debian.org/images/cloud/trixie/latest"
IMG_NAME="debian-13-genericcloud-amd64.qcow2"
RUN="$CACHE/runs/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$CACHE/img" "$RUN"
log() { printf '[vmtest %s] %s\n' "$(date +%T)" "$*" | tee -a "$RUN/harness.log"; }

# --- host prerequisites -------------------------------------------------------
need=()
command -v qemu-system-x86_64 >/dev/null || need+=(qemu-system-x86)
command -v qemu-img >/dev/null || need+=(qemu-utils)
command -v cloud-localds >/dev/null || need+=(cloud-image-utils)
if ((${#need[@]})); then
  log "installing ${need[*]}"
  sudo -n apt-get install -y "${need[@]}" >/dev/null
fi
ACCEL=(-accel tcg); CPU=max
if [[ -r /dev/kvm && -w /dev/kvm ]]; then ACCEL=(-enable-kvm); CPU=host; else
  log "WARNING: no /dev/kvm access; using slow software emulation"; fi

# --- base image (cached, sha512-verified against Debian's SHA512SUMS) ----------
(
  cd "$CACHE/img"
  curl -fsSL -o SHA512SUMS.new "$IMG_URL/SHA512SUMS" && mv SHA512SUMS.new SHA512SUMS
  want=$(awk -v n="$IMG_NAME" '$2==n{print $1}' SHA512SUMS)
  [[ -n "$want" ]] || { echo "image not listed in SHA512SUMS" >&2; exit 1; }
  if [[ ! -f "$IMG_NAME" ]] || [[ "$(sha512sum "$IMG_NAME" | cut -d' ' -f1)" != "$want" ]]; then
    curl -fL --retry 3 -o "$IMG_NAME.part" "$IMG_URL/$IMG_NAME"
    [[ "$(sha512sum "$IMG_NAME.part" | cut -d' ' -f1)" == "$want" ]] || { echo "REFUSED: image sha512 mismatch" >&2; exit 1; }
    mv "$IMG_NAME.part" "$IMG_NAME"
  fi
)
log "base image verified: $CACHE/img/$IMG_NAME"

# --- per-run overlay, key, cloud-init seed ---------------------------------------
qemu-img create -q -f qcow2 -F qcow2 -b "$CACHE/img/$IMG_NAME" "$RUN/disk.qcow2" 20G
ssh-keygen -q -t ed25519 -N '' -C vmtest -f "$RUN/key"
cat > "$RUN/user-data" <<UD
#cloud-config
hostname: walter-vmtest
users:
  - name: walter
    gecos: Walter VM test
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: true
    ssh_authorized_keys: [$(cat "$RUN/key.pub")]
ssh_pwauth: false
package_update: false
UD
printf 'instance-id: vmtest-%s\nlocal-hostname: walter-vmtest\n' "$(basename "$RUN")" > "$RUN/meta-data"
cloud-localds "$RUN/seed.img" "$RUN/user-data" "$RUN/meta-data"
PORT=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')

# --- boot (headless, user-mode NAT: outbound only, ssh on 127.0.0.1:PORT) --------
nice -n 10 qemu-system-x86_64 "${ACCEL[@]}" -cpu "$CPU" -smp 2 -m 4096 \
  -drive file="$RUN/disk.qcow2",if=virtio,format=qcow2 \
  -drive file="$RUN/seed.img",if=virtio,format=raw \
  -netdev user,id=n0,hostfwd=tcp:127.0.0.1:$PORT-:22 -device virtio-net-pci,netdev=n0 \
  -device virtio-rng-pci -serial file:"$RUN/console.log" -monitor none -display none \
  -pidfile "$RUN/qemu.pid" -daemonize
cleanup() {
  if [[ -f "$RUN/qemu.pid" ]]; then kill "$(cat "$RUN/qemu.pid")" 2>/dev/null || true; fi
  if (( KEEP )); then log "kept: $RUN (ssh -p $PORT -i $RUN/key walter@127.0.0.1)"; else rm -f "$RUN/disk.qcow2" "$RUN/seed.img" "$RUN/local.tar.gz"; fi
}
trap cleanup EXIT
SSH=(ssh -q -p "$PORT" -i "$RUN/key" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
     -o ConnectTimeout=5 -o ServerAliveInterval=30 walter@127.0.0.1)
log "booting VM (ssh port $PORT)"
T0=$(date +%s)
for _ in $(seq 120); do "${SSH[@]}" true 2>/dev/null && break; sleep 3; done
"${SSH[@]}" 'cloud-init status --wait >/dev/null 2>&1; true'
log "VM up after $(( $(date +%s) - T0 ))s"

# --- the real bootstrap ------------------------------------------------------
if [[ -n "$PREP" ]]; then
  log "prep: $PREP"
  "${SSH[@]}" "$PREP" >> "$RUN/prep.log" 2>&1 || log "prep exited non-zero (see prep.log)"
fi
if [[ -n "$RELEASE" ]]; then
  [[ "$RELEASE" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { log "bad --release"; exit 2; }
  scp -q -P "$PORT" -i "$RUN/key" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "$REPO/deploy/release-public.pem" walter@127.0.0.1:/tmp/pub.pem
  B="${CHANNEL%/}/"
  CMD="set -e; cd /tmp; curl -fsSL -o m.json '${B}manifest-$RELEASE.json'; \
T=\$(python3 -c 'import json;print(json.load(open(\"m.json\"))[\"tarball\"])'); \
S=\$(python3 -c 'import json;print(json.load(open(\"m.json\"))[\"signature\"])'); \
H=\$(python3 -c 'import json;print(json.load(open(\"m.json\"))[\"sha256\"])'); \
curl -fsSL -o r.tar.gz '$B'\$T; curl -fsSL -o r.sig '$B'\$S; \
[ \"\$(sha256sum r.tar.gz | cut -d' ' -f1)\" = \"\$H\" ] || { echo REFUSED sha256; exit 1; }; \
openssl pkeyutl -verify -rawin -pubin -inkey pub.pem -in r.tar.gz -sigfile r.sig; \
echo 'release $RELEASE verified (sha256 + signature)'; \
sudo mkdir -p /var/lib/walter/src; sudo tar -xzf r.tar.gz -C /var/lib/walter/src --strip-components=1 --no-same-owner; \
sudo env WALTER_RELEASE_VERSION=$RELEASE bash /var/lib/walter/src/deploy/install.sh --invite '$INVITE' --channel '$CHANNEL'"
elif (( LOCAL )); then
  VER=$(curl -fsSL "${CHANNEL%/}/latest.json" | python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])')
  git -C "$REPO" archive --format=tar.gz --prefix="walter-$VER/" HEAD > "$RUN/local.tar.gz"
  scp -q -P "$PORT" -i "$RUN/key" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    "$RUN/local.tar.gz" walter@127.0.0.1:/tmp/local.tar.gz
  CMD="set -e; sudo mkdir -p /var/lib/walter/src; sudo tar -xzf /tmp/local.tar.gz -C /var/lib/walter/src --strip-components=1 --no-same-owner; \
sudo env WALTER_RELEASE_VERSION=$VER bash /var/lib/walter/src/deploy/install.sh --invite '$INVITE' --channel '$CHANNEL'"
else
  CMD="curl -fsSL '${CHANNEL%/}/install.sh' | sudo bash -s -- --invite '$INVITE' --channel '$CHANNEL'"
fi
log "installing: $CMD"
T1=$(date +%s)
set +e
"${SSH[@]}" "$CMD" > "$RUN/install.log" 2>&1
RC=$?
set -e
INSTALL_S=$(( $(date +%s) - T1 ))
log "installer exit=$RC after ${INSTALL_S}s (log: $RUN/install.log)"

# --- reboot, as the installer tells people to, then check -----------------------
if (( RC == 0 )); then
  "${SSH[@]}" 'sudo systemctl reboot' 2>/dev/null || true
  sleep 10
  for _ in $(seq 60); do "${SSH[@]}" true 2>/dev/null && break; sleep 3; done
  log "rebooted"
fi
scp -q -P "$PORT" -i "$RUN/key" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  "$HERE/checks.sh" walter@127.0.0.1:/tmp/checks.sh
"${SSH[@]}" 'bash /tmp/checks.sh' > "$RUN/checks.log" 2>&1 || true
log "checks done (log: $RUN/checks.log)"
grep -E '^(PASS|FAIL|WARN) ' "$RUN/checks.log" | tee -a "$RUN/harness.log" || true
if [[ -n "$POST" ]]; then
  log "post: $POST"
  VM_SSH_PORT=$PORT VM_KEY="$RUN/key" VM_RUN="$RUN" bash "$POST" > "$RUN/post.log" 2>&1 || log "post exited $?"
  grep -E '^(PASS|FAIL|WARN) ' "$RUN/post.log" | tee -a "$RUN/harness.log" || true
fi
printf '{"installer_exit": %d, "install_seconds": %d, "run_dir": "%s"}\n' "$RC" "$INSTALL_S" "$RUN" > "$RUN/summary.json"
"${SSH[@]}" 'sudo poweroff' 2>/dev/null || true
sleep 3
log "summary: $RUN/summary.json"
