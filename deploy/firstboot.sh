#!/usr/bin/env bash
# Runs once, on the first boot after the USB installer finished.
# Shows progress on the laptop's screen, installs Walter, then reboots
# into Walter. If something fails (usually the network), it retries.
set -uo pipefail
LOG=/var/log/walter-firstboot.log
SRC="$(cd "$(dirname "$0")/.." && pwd)"

banner() {
  clear
  setterm --blank 0 --powerdown 0 2>/dev/null || true
  cat <<'ART'

        .-""""-.
       /  o  o  \        Walter is getting ready.
       |        |        This takes 10-30 minutes depending on your
        \  __  /         internet. Please leave the laptop on.
         '----'          It will restart by itself when it's done.

ART
}

# ------------------------------------------------------------ network
# Plain http on purpose: if the laptop's clock is wrong (dead CMOS battery),
# https fails even though the internet works.
online() { curl -fs --max-time 8 -o /dev/null http://deb.debian.org/debian/; }

wifi_if() { for d in /sys/class/net/*; do [ -d "$d/wireless" ] && { basename "$d"; return; }; done; }

net_status() {
  echo "  Network check:"
  local W; W=$(wifi_if)
  if [ -n "$W" ]; then
    local ssid; ssid=$(iw dev "$W" link 2>/dev/null | sed -n 's/^[[:space:]]*SSID: //p')
    echo "    Wi-Fi ($W): ${ssid:+connected to $ssid}${ssid:-not connected}"
  else
    echo "    Wi-Fi: no Wi-Fi adapter found (missing driver/firmware?)"
  fi
  ip -br -4 addr 2>/dev/null | grep -v '^lo ' | sed 's/^/    /'
  ip route 2>/dev/null | grep -q '^default' || echo "    no route to the internet"
  grep -q '^nameserver' /etc/resolv.conf 2>/dev/null || echo "    no DNS server"
  echo
}

repair_net() {
  # Wi-Fi switched off by software (rfkill)?
  for r in /sys/class/rfkill/rfkill*/soft; do [ -w "$r" ] && echo 0 > "$r"; done 2>/dev/null
  # Restart the network the installer set up (Wi-Fi or cable).
  local ifs
  ifs=$( { ifquery --list --allow=hotplug; ifquery --list --allow=auto; } 2>/dev/null | grep -vx lo | sort -u)
  for i in $ifs; do
    ifdown --force "$i" >/dev/null 2>&1
    timeout 60 ifup "$i" >>"$LOG" 2>&1
  done
  # Connected but no name lookups: add public DNS servers.
  if ip route | grep -q '^default' && ! getent hosts deb.debian.org >/dev/null; then
    printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' >> /etc/resolv.conf
  fi
}

ask_wifi() {
  local W; W=$(wifi_if)
  [ -n "$W" ] && command -v wpa_supplicant >/dev/null || return 1
  ip link set "$W" up 2>/dev/null
  echo "  Let's connect Walter to Wi-Fi (or plug in a network cable)."
  local nets=()
  if command -v iw >/dev/null; then
    mapfile -t nets < <(iw dev "$W" scan 2>/dev/null | sed -n 's/^[[:space:]]*SSID: //p' | grep -v '^$' | sort -u | head -n 15)
    local n=1; for x in "${nets[@]}"; do printf '    %2d) %s\n' "$n" "$x"; n=$((n+1)); done
  fi
  local ssid pw
  read -r -t 300 -p "  Wi-Fi name${nets:+ or number}: " ssid || { echo; return 1; }
  [ -z "$ssid" ] && return 1
  if [[ "$ssid" =~ ^[0-9]+$ ]] && (( ssid >= 1 && ssid <= ${#nets[@]} )); then ssid=${nets[$((ssid-1))]}; fi
  read -r -s -p "  Password for $ssid (leave empty if none): " pw; echo
  ifdown --force "$W" >/dev/null 2>&1
  # keep everything except the old stanza for this adapter
  awk -v w="$W" '
    $1 ~ /^(auto|allow-hotplug)$/ && $2 == w { next }
    $1 == "iface" { skip = ($2 == w) }
    $1 ~ /^(auto|allow-hotplug|mapping|source)/ && $2 != w { skip = 0 }
    !skip' /etc/network/interfaces > /etc/network/interfaces.new
  {
    printf '\nallow-hotplug %s\niface %s inet dhcp\n\twpa-ssid %s\n' "$W" "$W" "$ssid"
    if [ -n "$pw" ]; then printf '\twpa-psk %s\n' "$pw"; else printf '\twpa-key-mgmt NONE\n'; fi
  } >> /etc/network/interfaces.new
  chmod 600 /etc/network/interfaces.new
  mv /etc/network/interfaces.new /etc/network/interfaces
  echo "  Connecting to $ssid..."
  timeout 60 ifup "$W" >>"$LOG" 2>&1
  return 0
}

wait_for_internet() {
  local tries=0
  until online; do
    tries=$((tries + 1))
    case $tries in
      1) echo "  Waiting for the internet..." ;;
      6) echo "  Still no internet; restarting the network..."; repair_net; net_status ;;
      18) net_status; ask_wifi || echo "  Plug in a network cable, or wait: trying again..."; tries=6 ;;
    esac
    sleep 5
  done
  echo "  Internet: OK"
  # Fix the clock if it's off (dead CMOS battery), or https and apt fail.
  local d; d=$(curl -fsI --max-time 8 http://deb.debian.org/debian/ | sed -n 's/^[Dd]ate: //p' | tr -d '\r')
  if [ -n "$d" ]; then
    local now web; now=$(date +%s); web=$(date -d "$d" +%s 2>/dev/null || echo "$now")
    if (( now - web > 300 || web - now > 300 )); then
      date -s "$d" >/dev/null && hwclock -w 2>/dev/null; echo "  Clock set to $(date)"
    fi
  fi
}

dmesg -n 1 2>/dev/null || true   # keep kernel messages off the progress screen

attempt=1
while true; do
  banner
  echo "  Attempt $attempt  —  details are saved in $LOG"
  echo
  wait_for_internet
  if WALTER_NONINTERACTIVE=1 "$SRC/deploy/install.sh" 2>&1 | tee -a "$LOG" | \
       grep --line-buffered '^==> ' | sed -u 's/^==> /  ✓ /'; [ "${PIPESTATUS[0]}" -eq 0 ]; then
    mkdir -p /var/lib/walter
    date -Is > /var/lib/walter/installed
    systemctl disable walter-firstboot.service 2>/dev/null || true
    echo; echo "  All done! Restarting into Walter..."
    sleep 5
    systemctl reboot
    exit 0
  fi
  echo
  echo "  Something went wrong (see $LOG). Trying again in 60 seconds..."
  tail -n 5 "$LOG" | sed 's/^/    /'
  attempt=$((attempt + 1))
  sleep 60
done
