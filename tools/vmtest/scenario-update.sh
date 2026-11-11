#!/usr/bin/env bash
# Host-side --post script for run.sh: update -> rollback -> update again, then tamper tests.
#   tools/vmtest/run.sh --release 0.1.0 --post tools/vmtest/scenario-update.sh
# Needs VM_SSH_PORT, VM_KEY (from run.sh). Prints PASS/FAIL/WARN lines + details.
# Tamper channels (TAMPER_DIR, default ~/.walter/vmtest/tamper) are served on 127.0.0.1
# (the VM reaches them as 10.0.2.2); each holds a latest.json advertising a NEWER version:
#   badsha/    genuine signed tarball, wrong sha256 in latest.json
#   badsig/    modified tarball, correct sha256, the original (now wrong) signature
#   otherkey/  modified tarball signed with a key that isn't Walter's
set -uo pipefail
TAMPER_DIR="${TAMPER_DIR:-$HOME/.walter/vmtest/tamper}"
SSH=(ssh -q -p "$VM_SSH_PORT" -i "$VM_KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
     -o ConnectTimeout=5 walter@127.0.0.1)
vm() { "${SSH[@]}" "export XDG_RUNTIME_DIR=/run/user/\$(id -u); $*"; }
ok() { echo "PASS $*"; }; bad() { echo "FAIL $*"; }; warn() { echo "WARN $*"; }
sect() { printf '\n----- %s -----\n' "$*"; }
cur() { vm 'readlink ~/.walter-releases/current'; }
healthy() {   # walter.service active, /api/status 200, stays up 10 s
  for _ in $(seq 40); do
    [[ "$(vm 'systemctl --user is-active walter; curl -s -o /dev/null -w %{http_code} --max-time 5 http://127.0.0.1:8765/api/status' | tr '\n' ' ')" == "active 200" ]] && break
    sleep 3; done
  sleep 10
  [[ "$(vm 'systemctl --user is-active walter; curl -s -o /dev/null -w %{http_code} --max-time 5 http://127.0.0.1:8765/api/status' | tr '\n' ' ')" == "active 200" ]]
}
run_update() {   # walterctl update, then wait for walter-update.service to finish
  vm '~/walter/bin/walterctl update'
  sleep 5
  for _ in $(seq 200); do
    s=$(vm 'systemctl is-active walter-update.service'); [[ "$s" == activating || "$s" == active ]] || break; sleep 5; done
  vm 'sudo tail -6 /var/log/walter-update.log'
}

sect "start"
vm '~/walter/bin/walterctl update status; cat /var/lib/walter/release-version; ls -la /var/lib/walter/'
[[ "$(cur)" == 0.1.0 ]] && ok "starts on 0.1.0" || bad "starts on $(cur), not 0.1.0"

sect "update 0.1.0 -> latest"
if ! vm 'test -f /var/lib/walter/installed'; then
  warn "0.1.0 curl install has no /var/lib/walter/installed (bug fixed in 0.1.1): walterctl update is skipped"
  run_update; vm 'sudo journalctl -u walter-update --no-pager -n 5'
  [[ "$(cur)" == 0.1.0 ]] && echo "(confirmed: still 0.1.0, update skipped by ConditionPathExists)"
  vm 'sudo touch /var/lib/walter/installed'; echo "marker created by hand to continue"
fi
T=$(date +%s); run_update; echo "update took $(( $(date +%s) - T ))s"
NEW=$(cur); echo "now on $NEW"
[[ "$NEW" == 0.1.1 ]] && ok "walterctl update moved 0.1.0 -> 0.1.1" || bad "after update current=$NEW"
[[ "$(vm 'cat ~/walter/VERSION')" == 0.1.1 ]] && ok "~/walter/VERSION = 0.1.1" || bad "VERSION=$(vm 'cat ~/walter/VERSION')"
vm 'test -d ~/.walter-releases/0.1.0 && test -f ~/.walter-releases/0.1.0/VERSION' && ok "previous release 0.1.0 kept" || bad "0.1.0 copy missing"
vm '~/walter/bin/walterctl update status'
vm 'test -f /var/lib/walter/installed' && ok "installed marker present" || bad "marker missing after update"
healthy && ok "walter healthy on 0.1.1 (service active, /api/status 200)" || bad "walter unhealthy after update"
vm 'echo NRestarts=$(systemctl --user show -p NRestarts --value walter); journalctl --user -u walter --no-pager -n 400 | grep -ciE "traceback" | sed "s/^/tracebacks: /"'

sect "rollback"
vm '~/walter/bin/walterctl update rollback'; echo "rollback exit=$?"
sleep 5
[[ "$(cur)" == 0.1.0 ]] && ok "rollback -> 0.1.0" || bad "after rollback current=$(cur)"
[[ "$(vm 'cat ~/walter/VERSION')" == 0.1.0 ]] && ok "~/walter/VERSION = 0.1.0 after rollback" || bad "VERSION=$(vm 'cat ~/walter/VERSION')"
healthy && ok "walter healthy after rollback" || bad "walter unhealthy after rollback"
vm '~/walter/bin/walterctl update status; systemctl --user show -p ActiveEnterTimestamp walter'

sect "update forward again"
run_update
[[ "$(cur)" == 0.1.1 ]] && ok "walterctl update forward again -> 0.1.1" || bad "forward again: current=$(cur)"
healthy && ok "walter healthy after going forward again" || bad "walter unhealthy after forward again"
vm '~/walter/bin/walterctl update status'

sect "tamper"
HP=$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')
python3 -m http.server "$HP" --bind 127.0.0.1 --directory "$TAMPER_DIR" >/dev/null 2>&1 &
SRV=$!; trap 'kill $SRV 2>/dev/null' EXIT; sleep 1
vm 'sudo cp /etc/walter/update.conf /tmp/update.conf.orig'
for c in badsha badsig otherkey; do
  vm "sudo sed -i 's#^WALTER_UPDATE_URL=.*#WALTER_UPDATE_URL=http://10.0.2.2:$HP/$c/#' /etc/walter/update.conf"
  out=$(vm 'sudo /usr/local/sbin/walter-update --now; echo "exit=$?"'); echo "[$c] $out"
  if grep -q REFUSED <<<"$out" && grep -q 'exit=1' <<<"$out" && [[ "$(cur)" == 0.1.1 ]] \
     && vm 'test ! -e ~/.walter-releases/0.1.2 && test ! -e ~/.walter-releases/0.1.2.new && ! sudo test -e /var/lib/walter/src.new && ! grep -rqs pwned ~/walter/EVIL.sh'; then
    ok "tamper $c: updater REFUSED, still on 0.1.1, nothing unpacked"
  else bad "tamper $c: not refused cleanly (current=$(cur))"; fi
done
vm 'sudo cp /tmp/update.conf.orig /etc/walter/update.conf; grep WALTER_UPDATE_URL /etc/walter/update.conf'
healthy && ok "walter still healthy after tamper attempts" || bad "walter unhealthy after tamper"
vm 'ls ~/.walter-releases; cat /var/lib/walter/release-version'
