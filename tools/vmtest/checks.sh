#!/usr/bin/env bash
# Runs INSIDE the test VM as the walter user after the installer. Prints
# PASS/FAIL/WARN lines (parsed by run.sh) plus raw details for the report.
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$XDG_RUNTIME_DIR/bus"
ok()   { echo "PASS $*"; }
bad()  { echo "FAIL $*"; }
warn() { echo "WARN $*"; }
sect() { printf '\n----- %s -----\n' "$*"; }

sect layout
ls -la ~/walter ~/.walter-releases 2>&1
[[ -L ~/walter ]] && ok "~/walter is a release symlink -> $(readlink -f ~/walter)" || bad "~/walter is not release-managed"
[[ -x ~/walter/.venv/bin/python ]] && ok "venv present" || bad "venv missing"
[[ -f ~/.walter/config.toml ]] && ok "config.toml written" || bad "no ~/.walter/config.toml"
ls ~/.walter/voices/*.onnx >/dev/null 2>&1 && ok "piper voice downloaded" || bad "no piper voice"
for b in cage chromium pipewire wpctl nmcli git; do command -v $b >/dev/null && ok "binary $b" || bad "binary $b missing"; done
bash -lc 'command -v claude' >/dev/null && ok "claude CLI installed" || bad "claude CLI missing"
cat /etc/walter/update.conf 2>&1; ls -la /etc/walter /etc/walter/firstrun 2>&1
[[ -f /etc/walter/firstrun/pending ]] && ok "first-run wizard pending" || bad "first-run not pending"
[[ "$(sudo cat /etc/walter/invite 2>/dev/null)" == TEST ]] && ok "invite stored" || bad "invite not stored"

sect "user services"
for i in $(seq 30); do systemctl --user is-active -q walter && break; sleep 2; done
systemctl --user --no-pager status walter walter-workers-live 2>&1 | head -40
systemctl --user is-active -q walter && ok "walter.service active" || bad "walter.service not active ($(systemctl --user is-active walter))"
systemctl --user is-active -q walter-workers-live && ok "walter-workers-live active" || warn "walter-workers-live not active"
systemctl --user --no-pager --failed 2>&1
sleep 15
systemctl --user is-active -q walter && ok "walter.service still active after 15s" || bad "walter.service died"
echo "NRestarts=$(systemctl --user show -p NRestarts --value walter)"
journalctl --user -u walter --no-pager -n 120 2>&1 | tail -120

sect "http"
code() { : > /tmp/body; curl -s -o /tmp/body -w '%{http_code}' --max-time 10 "$1"; }
for p in / /api/status /firstrun /dashboard /app/; do
  c=$(code "http://127.0.0.1:8765$p"); echo "GET $p -> $c  $(head -c 300 /tmp/body | tr '\n' ' ')"
done
[[ "$(code http://127.0.0.1:8765/api/status)" == 200 ]] && ok "/api/status 200" || bad "/api/status not 200"
c=$(code http://127.0.0.1:8765/firstrun); [[ $c == 200 || $c == 403 ]] && ok "/firstrun wizard reachable ($c; 403 = key-gated, as designed)" || bad "/firstrun -> $c"
c=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 http://127.0.0.1:8766/setup/ping); echo "setup ping 8766 -> $c $(curl -s --max-time 5 http://127.0.0.1:8766/setup/ping)"

sect "system services"
systemctl --no-pager --failed 2>&1
for u in walter-update.timer walter-admin-password.path NetworkManager avahi-daemon; do
  systemctl is-enabled -q $u 2>/dev/null && ok "$u enabled" || warn "$u not enabled"; done

sect "update flow"
~/walter/bin/walterctl update status 2>&1 && ok "walterctl update status" || bad "walterctl update status failed"
before=$(readlink ~/.walter-releases/current)
sudo -n /usr/bin/systemctl start walter-update.service 2>&1; echo "update service exit=$?"
sudo tail -40 /var/log/walter-update.log 2>&1; sudo journalctl -u walter-update --no-pager -n 40 2>&1
after=$(readlink ~/.walter-releases/current)
[[ "$before" == "$after" ]] && ok "no-op update kept $after" || warn "update switched $before -> $after"
~/walter/bin/walterctl update rollback --force 2>&1; rc=$?
echo "rollback exit=$rc"
[[ $rc == 1 ]] && ok "rollback with a single release refuses cleanly" || warn "rollback exit $rc"

sect "hardware-dependent"
systemctl --no-pager status getty@tty1 2>&1 | head -8
pgrep -a cage; pgrep -a chromium | head -2
aplay -l 2>&1 | head -3; arecord -l 2>&1 | head -3; ls /dev/dri /dev/video* 2>&1
journalctl --user -u walter --no-pager 2>&1 | grep -iE 'audio|portaudio|pipewire|camera|display|error|traceback' | sort | uniq -c | sort -rn | head -30
