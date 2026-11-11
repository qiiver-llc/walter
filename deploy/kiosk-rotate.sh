#!/usr/bin/env bash
# Runs INSIDE cage, as its app: turns the screen, then becomes the browser.
#   kiosk-rotate.sh BROWSER [ARGS...]
#
# For a laptop panel mounted sideways on the wall (portrait), while the compositor
# still drives it in landscape. Set in ~/.walter/kiosk.env (or `walterctl display`):
#   WALTER_ROTATE=90        0 | 90 | 180 | 270 (clockwise, as wlr-randr --transform)
#   WALTER_OUTPUT=eDP-1     optional; default: every enabled output
# Needs wlr-randr (sudo apt install wlr-randr); without it the screen stays as is.
# The face lays itself out for portrait by itself once the window is taller than wide.
LOG="$HOME/.walter/kiosk.log"
# Invisible mouse pointer for the wall (the owner, 10 Nov): a 1x1 transparent cursor theme.
if [[ -d "$HOME/.local/share/icons/walter-blank/cursors" ]]; then
  export XCURSOR_THEME=walter-blank XCURSOR_SIZE=1 XCURSOR_PATH="$HOME/.local/share/icons:/usr/share/icons"
fi
rot="${WALTER_ROTATE:-0}"
case "$rot" in
  90|180|270|flipped|flipped-90|flipped-180|flipped-270) ;;
  *) exec "$@" ;;
esac
if ! command -v wlr-randr >/dev/null; then
  echo "$(date -Is) WALTER_ROTATE=$rot but wlr-randr is not installed (sudo apt install wlr-randr); not rotating" >>"$LOG"
  exec "$@"
fi
outs=()
for _ in $(seq 1 20); do   # cage may still be bringing its output up
  if [[ -n ${WALTER_OUTPUT:-} ]]; then outs=("$WALTER_OUTPUT")
  else
    mapfile -t outs < <(wlr-randr 2>/dev/null | awk '/^[^ \t]/ {name=$1} /^[ \t]+Enabled: yes/ {print name}')
  fi
  (( ${#outs[@]} )) && break
  sleep 0.25
done
for o in "${outs[@]}"; do
  if wlr-randr --output "$o" --transform "$rot" >>"$LOG" 2>&1; then
    echo "$(date -Is) rotated $o to $rot" >>"$LOG"
  else
    echo "$(date -Is) could not rotate $o to $rot" >>"$LOG"
  fi
done
(( ${#outs[@]} )) || echo "$(date -Is) WALTER_ROTATE=$rot: no output found to rotate" >>"$LOG"
exec "$@"
