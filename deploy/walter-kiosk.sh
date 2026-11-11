#!/usr/bin/env bash
# Runs on tty1 after auto-login: a full-screen browser showing only Walter's face.
# No desktop, no windows, no cursor. If the browser ever exits it comes back.
#
# Graphics and sandboxing differ a lot between old laptops (and virtual
# machines), so the kiosk checks that Walter's face really connected to his
# brain within 45 seconds. If not, it steps down to safer settings and
# remembers the first level that works:
#   level 0  Chromium, GPU                     (smoothest)
#   level 1  Chromium, software compositor
#   level 2  Firefox, software compositor
#   level 3  Chromium, no GPU, no sandbox      (last resorts: they only ever
#   level 4  Firefox, no content sandbox        show Walter's own local page)
URL="${WALTER_FACE_URL:-http://127.0.0.1:8765/}"
CHROMIUM="$(command -v chromium || command -v chromium-browser)"
LOG="$HOME/.walter/kiosk.log"
LEVEL_FILE="$HOME/.walter/kiosk-level"
MAX_LEVEL=4
mkdir -p "$HOME/.walter/firefox"
: > "$LOG.new"; tail -n 400 "$LOG" >> "$LOG.new" 2>/dev/null; mv "$LOG.new" "$LOG"
{
  echo "$(date -Is) kiosk starting"
  echo "cpu: $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2)"
  echo "cpu flags: $(grep -m1 '^flags' /proc/cpuinfo | grep -ow -E 'sse4_2|avx|avx2|avx512f|xsave|bmi2|fma|popcnt' | sort -u | tr '\n' ' ')"
  echo "dri: $(ls /dev/dri 2>/dev/null | tr '\n' ' ')"
  echo "gpu: $(cat /sys/class/drm/card*/device/uevent 2>/dev/null | grep -E 'DRIVER|PCI_ID' | tr '\n' ' ')"
} >>"$LOG"
export MOZ_CRASHREPORTER_DISABLE=1
# Browsers abort if they can't write their config/cache dirs; if the normal
# ones aren't writable for some reason, use private ones instead.
for v in CONFIG:.config CACHE:.cache; do
  name=${v%%:*} dir=$HOME/${v#*:}
  if ! { mkdir -p "$dir" && [ -w "$dir" ]; } 2>/dev/null; then
    export "XDG_${name}_HOME=$HOME/.walter/xdg-${v#*:.}"; mkdir -p "$HOME/.walter/xdg-${v#*:.}"
    echo "$(date -Is) $dir not writable; using \$XDG_${name}_HOME instead" >>"$LOG"
  fi
done

# make sure the brain is running, then wait for the face server
systemctl --user start walter.service 2>/dev/null || true
for _ in $(seq 1 90); do curl -fs "$URL" >/dev/null && break; sleep 1; done

face_connected() {
  curl -fs --max-time 3 "${URL}api/status" | grep -qE '"faces_connected": *[1-9]'
}

# Optional extra environment for cage/browser, one KEY=VALUE per line, read at
# every (re)launch, e.g. WLR_DRM_NO_ATOMIC=1 (legacy KMS page flips: a workaround
# for "Atomic commit failed: Device or resource busy" freezes). Delete to undo.
ENV_FILE="$HOME/.walter/kiosk.env"
# Portrait wall: WALTER_ROTATE=90|180|270 in kiosk.env (or `walterctl display portrait`)
# turns the screen inside cage before the browser starts (deploy/kiosk-rotate.sh).
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

launch() {  # launch <level>: start cage + browser in the background; sets LAUNCH_PID
  local level=$1 env_args=() line wrap=()
  (( level >= 1 )) && env_args+=(WLR_RENDERER=pixman WLR_NO_HARDWARE_CURSORS=1)
  if [[ -r $ENV_FILE ]]; then
    while IFS= read -r line; do
      [[ $line =~ ^[A-Za-z_][A-Za-z0-9_]*=.*$ ]] && env_args+=("$line")
    done < "$ENV_FILE"
    (( ${#env_args[@]} )) && echo "$(date -Is) kiosk env: ${env_args[*]}" >>"$LOG"
  fi
  for line in "${env_args[@]}"; do
    [[ $line =~ ^WALTER_ROTATE=(90|180|270|flipped.*)$ ]] && [[ -x $HERE/kiosk-rotate.sh ]] && wrap=("$HERE/kiosk-rotate.sh")
  done
  # -d: no window decorations   -m last: use the external monitor if one is plugged in
  case $level in
    0|1)
      env "${env_args[@]}" cage -d -m last -- "${wrap[@]}" "$CHROMIUM" --kiosk --app="$URL" \
        --ozone-platform=wayland --no-first-run --enable-logging=stderr --v=0 \
        --user-data-dir="$HOME/.walter/chromium" >>"$LOG" 2>&1 & ;;
    2)
      env "${env_args[@]}" MOZ_ENABLE_WAYLAND=1 cage -d -m last -- \
        "${wrap[@]}" firefox-esr --kiosk --profile "$HOME/.walter/firefox" "$URL" >>"$LOG" 2>&1 & ;;
    3)
      env "${env_args[@]}" cage -d -m last -- "${wrap[@]}" "$CHROMIUM" --kiosk --app="$URL" \
        --ozone-platform=wayland --no-first-run --enable-logging=stderr --v=0 \
        --disable-gpu --no-sandbox \
        --user-data-dir="$HOME/.walter/chromium" >>"$LOG" 2>&1 & ;;
    *)
      env "${env_args[@]}" MOZ_ENABLE_WAYLAND=1 MOZ_DISABLE_CONTENT_SANDBOX=1 \
        MOZ_DISABLE_RDD_SANDBOX=1 MOZ_DISABLE_GMP_SANDBOX=1 MOZ_DISABLE_SOCKET_PROCESS_SANDBOX=1 \
        cage -d -m last -- "${wrap[@]}" firefox-esr --kiosk --profile "$HOME/.walter/firefox" "$URL" >>"$LOG" 2>&1 & ;;
  esac
  LAUNCH_PID=$!
}

level=$(cat "$LEVEL_FILE" 2>/dev/null || echo 0)
file_level=$level   # what the level file said at the last look
fails=0
while true; do
  # someone may have changed the level file by hand since the last launch (e.g.
  # to try the software compositor): honour it instead of overwriting it
  want=$(cat "$LEVEL_FILE" 2>/dev/null)
  if [[ $want =~ ^[0-9]+$ ]] && (( want <= MAX_LEVEL )) && [[ $want != "$file_level" ]]; then
    echo "$(date -Is) level file changed to $want by hand" >>"$LOG"; level=$want; fails=0
  fi
  file_level=$want
  echo "$(date -Is) starting kiosk at level $level" >>"$LOG"
  launch "$level"; pid=$LAUNCH_PID
  up=0
  for _ in $(seq 1 45); do
    sleep 1
    kill -0 "$pid" 2>/dev/null || break
    if face_connected; then up=1; break; fi
  done
  if (( up )); then
    echo "$(date -Is) Walter's face is up at level $level" >>"$LOG"
    echo "$level" > "$LEVEL_FILE"   # remember what works for next boot
    file_level=$level
    fails=0
    wait "$pid"
    echo "$(date -Is) kiosk exited; restarting" >>"$LOG"
  else
    echo "$(date -Is) no face after 45 s at level $level" >>"$LOG"
    kill "$pid" 2>/dev/null; sleep 1; kill -9 "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
    pkill -u "$USER" -x cage 2>/dev/null
    fails=$((fails + 1))
    if (( fails >= 2 )); then
      fails=0
      level=$(( level < MAX_LEVEL ? level + 1 : 0 ))
      echo "$(date -Is) stepping to level $level" >>"$LOG"
    fi
  fi
  sleep 2
done
