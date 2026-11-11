#!/usr/bin/env bash
# Build "walter-installer.iso": Debian's official netinst image + Walter.
#
# Boot it on a laptop and it erases the internal disk, installs Debian with
# no questions (except Wi-Fi if there's no network cable), copies Walter on,
# and on first boot installs everything else and restarts into Walter.
#
#   ./iso/build-iso.sh [output.iso]
#
# Needs: xorriso, curl, sha256sum (apt install xorriso curl).
# Set DEBIAN_ISO=/path/to/debian-*-netinst.iso to skip the download.
set -euo pipefail

OUT="${1:-walter-installer.iso}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CACHE="${CACHE_DIR:-$ROOT/.iso-cache}"
BASE_URL="${DEBIAN_CD_URL:-https://cdimage.debian.org/debian-cd/current/amd64/iso-cd}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
say() { printf '\n==> %s\n' "$*"; }

command -v xorriso >/dev/null || { echo "Please install xorriso (sudo apt install xorriso)." >&2; exit 1; }

# ------------------------------------------------------------ Debian ISO
ISO="${DEBIAN_ISO:-}"
if [[ -z "$ISO" ]]; then
  mkdir -p "$CACHE"
  say "Finding the current Debian netinst image"
  curl -fsSL "$BASE_URL/SHA256SUMS" -o "$WORK/SHA256SUMS"
  NAME="$(awk '$2 ~ /^debian-[0-9.]+-amd64-netinst\.iso$/ {print $2; exit}' "$WORK/SHA256SUMS")"
  [[ -n "$NAME" ]] || { echo "Couldn't find a netinst image at $BASE_URL" >&2; exit 1; }
  ISO="$CACHE/$NAME"
  if [[ ! -f "$ISO" ]]; then
    say "Downloading $NAME"
    curl -fL --retry 3 -o "$ISO.part" "$BASE_URL/$NAME"
    mv "$ISO.part" "$ISO"
  fi
  say "Verifying checksum"
  (cd "$CACHE" && grep " $NAME\$" "$WORK/SHA256SUMS" | sha256sum -c -)
fi
echo "Using $ISO"

# ------------------------------------------------------------ Walter payload
say "Packing Walter"
mkdir -p "$WORK/walter"
# safe.directory: in a CI container the checkout belongs to another uid, and git
# then refuses the repo ("dubious ownership"), which silently dropped VERSION.
GIT=(git -c safe.directory="$ROOT" -C "$ROOT")
if "${GIT[@]}" rev-parse --git-dir >/dev/null 2>&1; then
  "${GIT[@]}" ls-files -z | (cd "$ROOT" && tar --null -T - -cf -) | tar -xf - -C "$WORK/walter"
  "${GIT[@]}" rev-parse --short HEAD > "$WORK/walter/VERSION"
else
  [[ -d "$ROOT/.git" ]] && echo "warning: $ROOT has a .git but git can't read it; packing without VERSION" >&2
  tar -C "$ROOT" --exclude=.git --exclude=.venv --exclude=.iso-cache --exclude='__pycache__' -cf - . \
    | tar -xf - -C "$WORK/walter"
fi

# ------------------------------------------------------------ boot menus
KARGS="auto=true priority=high preseed/file=/cdrom/preseed.cfg locale=en_US.UTF-8 keymap=us hostname=walter domain=local"
# After "---": used by the installer AND copied into the installed system.
# Disk safety for laptops whose SSD talks garbage to Debian's kernel
# (seen on a ThinkPad X1 Carbon with a newer NVMe SSD: "genctr mismatch",
# "invalid id completed", then every read and write fails):
#  - keep the SSD out of deep power-saving states (APST),
#  - keep the PCIe link out of power-saving (ASPM),
#  - turn off the IOMMU so the SSD's DMA goes straight to memory.
# A wall-mounted, always-plugged-in box loses nothing with these.
DISK_ARGS="nvme_core.default_ps_max_latency_us=0 pcie_aspm=off intel_iommu=off amd_iommu=off"

say "Writing boot menus"
# BIOS (isolinux): our own small menu, starts by itself after 15 s.
cat > "$WORK/isolinux.cfg" <<EOF
path
ui vesamenu.c32
menu title Walter the Wall Buddy
timeout 150
default walter
label walter
	menu label ^Install Walter  (ERASES THIS COMPUTER - starts in 15 s)
	kernel /install.amd/vmlinuz
	append vga=788 initrd=/install.amd/initrd.gz $KARGS --- quiet $DISK_ARGS
label debian
	menu label Plain Debian installer (asks everything)
	kernel /install.amd/vmlinuz
	append vga=788 initrd=/install.amd/initrd.gz --- quiet $DISK_ARGS
EOF

# UEFI (GRUB): keep Debian's menu, put ours first and default to it.
xorriso -osirrox on -indev "$ISO" -extract /boot/grub/grub.cfg "$WORK/grub.orig.cfg" 2>/dev/null
chmod u+w "$WORK/grub.orig.cfg"
cat > "$WORK/walter-grub.cfg" <<EOF
set default=0
set timeout=15
menuentry 'Install Walter  (ERASES THIS COMPUTER - starts in 15 s)' {
    set background_color=black
    linux    /install.amd/vmlinuz $KARGS --- quiet $DISK_ARGS
    initrd   /install.amd/initrd.gz
}
EOF
awk -v ins="$WORK/walter-grub.cfg" '
  !done && /^menuentry/ { while ((getline line < ins) > 0) print line; print ""; done=1 }
  { print }' "$WORK/grub.orig.cfg" > "$WORK/grub.cfg"
grep -q "Install Walter" "$WORK/grub.cfg" || { echo "Couldn't patch grub.cfg" >&2; exit 1; }

# ------------------------------------------------------------ build
say "Building $OUT"
rm -f "$OUT"
xorriso -indev "$ISO" -outdev "$OUT" \
  -map "$WORK/walter" /walter \
  -map "$ROOT/iso/preseed.cfg" /preseed.cfg \
  -map "$WORK/isolinux.cfg" /isolinux/isolinux.cfg \
  -map "$WORK/grub.cfg" /boot/grub/grub.cfg \
  -boot_image any replay

ls -lh "$OUT"
say "Done. Flash $OUT to a USB stick (balenaEtcher, Rufus in DD mode, or dd)."
