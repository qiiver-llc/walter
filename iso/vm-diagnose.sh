#!/usr/bin/env bash
# Runs INSIDE the test VM (as root, over SSH) to show why the kiosk browser
# isn't showing Walter. CI only; never part of a real install.
set -u
W=walter
UID_W=$(id -u $W)
export XDG_RUNTIME_DIR=/run/user/$UID_W
section() { echo; echo "======== $*"; }

section "processes"
ps -eo pid,ppid,etimes,rss,args --forest | grep -E "cage|chrom|firefox|python|walter-kiosk|agetty|login" | grep -v grep | cut -c1-200

section "session"
loginctl list-sessions --no-legend; ls -la "$XDG_RUNTIME_DIR" 2>&1 | head -20
WD=$(ls "$XDG_RUNTIME_DIR" | grep -E '^wayland-[0-9]+$' | head -1); echo "wayland display: ${WD:-none}"

section "kiosk.log (text)"
tr -cd '[:print:]\n' < /home/$W/.walter/kiosk.log | grep -vE 'dbus|Floss|UPower|ScreenSaver|PropertiesChanged|bluez' | tail -n 50

section "crashes"
journalctl -b --no-pager -t systemd-coredump 2>/dev/null | tail -n 60
journalctl -b --no-pager -k | grep -iE "traps|segfault|general protection|invalid opcode" | tail -n 10

section "face page from inside"
curl -s -o /dev/null -w "GET / -> %{http_code}\n" http://127.0.0.1:8765/
curl -s http://127.0.0.1:8765/api/status; echo

if [ -n "${WD:-}" ]; then
  export WAYLAND_DISPLAY=$WD
  section "chromium probe (same compositor, 15 s)"
  sudo -u $W -E timeout 15 chromium --ozone-platform=wayland --no-first-run --user-data-dir=/tmp/probe-ch \
     --enable-logging=stderr --v=1 --app=http://127.0.0.1:8765/ 2>&1 \
     | grep -vE 'dbus|Floss|UPower|ScreenSaver|PropertiesChanged|bluez' | head -n 40
  echo "exit: ${PIPESTATUS[0]}"
  section "chromium --no-sandbox --disable-gpu probe"
  sudo -u $W -E timeout 15 chromium --ozone-platform=wayland --no-first-run --user-data-dir=/tmp/probe-ch2 \
     --no-sandbox --disable-gpu --enable-logging=stderr --app=http://127.0.0.1:8765/ 2>&1 \
     | grep -vE 'dbus|Floss|UPower|ScreenSaver|PropertiesChanged|bluez' | head -n 25
  echo "exit: ${PIPESTATUS[0]}"
  section "firefox probe (headless screenshot of the page)"
  sudo -u $W -E timeout 60 firefox-esr --headless --profile /tmp/probe-ff --screenshot /tmp/ff.png \
     --window-size=800,500 http://127.0.0.1:8765/ 2>&1 | tail -n 15
  ls -la /tmp/ff.png 2>&1
fi

section "chromium headless probe (no display at all)"
sudo -u $W timeout 30 chromium --headless=new --no-first-run --user-data-dir=/tmp/probe-h --dump-dom \
   http://127.0.0.1:8765/ 2>&1 | grep -vE 'dbus|Floss|UPower|ScreenSaver|PropertiesChanged|bluez' | head -n 15
echo "exit: ${PIPESTATUS[0]}"

section "environment"
grep -m1 '^flags' /proc/cpuinfo | grep -ow -E 'pku|ospke|la57|avx2|avx512f|sse4_2|xsave' | sort -u | tr '\n' ' '; echo
uname -r; cat /proc/sys/kernel/unprivileged_userns_clone 2>/dev/null; sysctl vm.overcommit_memory vm.max_map_count 2>/dev/null
sudo -u $W bash -lc 'echo "LANG=$LANG LC_ALL=${LC_ALL:-} HOME=$HOME"; ulimit -a | grep -E "virtual|open files|max user"'
locale -a 2>/dev/null | head -5; df -h /dev/shm /tmp | tail -2

section "strace of headless chromium (last syscalls before the crash)"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq strace >/dev/null 2>&1
sudo -u $W -H timeout 40 strace -f -o /tmp/ch.strace chromium --headless=new --no-sandbox --no-first-run \
   --user-data-dir=/tmp/probe-s --dump-dom http://127.0.0.1:8765/ >/dev/null 2>&1
echo "exit: $?"
grep -nE "SIGTRAP|= -1 E|killed by" /tmp/ch.strace | grep -vE "ENOENT|EAGAIN|ENOTDIR|ENXIO|EACCES .*\.so|ENOTTY|ESRCH" | tail -n 40
echo "--- last 25 lines of the crashing thread"
pid=$(grep -m1 -E "SIGTRAP" /tmp/ch.strace | awk '{print $1}')
[ -n "$pid" ] && grep -E "^$pid " /tmp/ch.strace | tail -n 25

section "chromium headless as root, LANG=C, --single-process"
LANG=C timeout 30 chromium --headless=new --no-sandbox --single-process --no-first-run --user-data-dir=/tmp/probe-r \
   --dump-dom http://127.0.0.1:8765/ 2>&1 | grep -vE "dbus|crashpad|^Try " | head -n 8; echo "exit: ${PIPESTATUS[0]}"
section "chromium with --js-flags=--jitless"
timeout 30 chromium --headless=new --no-sandbox --no-first-run --user-data-dir=/tmp/probe-j --js-flags=--jitless \
   --dump-dom http://127.0.0.1:8765/ 2>&1 | grep -vE "dbus|crashpad|^Try " | head -n 8; echo "exit: ${PIPESTATUS[0]}"

section "firefox headless screenshot (as walter)"
sudo -u $W -H timeout 60 firefox-esr --headless --profile /tmp/probe-ff2 --screenshot /tmp/ff.png \
   --window-size=800,500 http://127.0.0.1:8765/ 2>&1 | tail -n 15
echo "exit: ${PIPESTATUS[0]}"; ls -la /tmp/ff.png 2>&1
python3 - <<'PY' 2>/dev/null
try:
    from PIL import Image
except ImportError:
    import sys; print("(no PIL to describe the screenshot)"); sys.exit()
im = Image.open("/tmp/ff.png").convert("RGB").resize((160, 100)); px = list(im.getdata())
print("ff screenshot: dark %.0f%%, distinct colours %d" % (100*sum(max(p) < 40 for p in px)/len(px), len(set(px))))
PY

section "packages"
dpkg -l chromium firefox-esr cage xkb-data libxkbcommon0 2>/dev/null | awk '/^ii/{print $2, $3}'
