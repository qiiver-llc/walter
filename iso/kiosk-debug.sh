#!/usr/bin/env bash
# Reproduce the kiosk (cage + browser) inside a Debian container without a
# real screen, take a screenshot of what each browser actually shows, and
# describe it. Used by CI to debug the kiosk quickly.
#   docker run --rm --privileged -v "$PWD:/w" debian:trixie bash /w/iso/kiosk-debug.sh
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends cage chromium firefox-esr fonts-dejavu-core \
  libgl1-mesa-dri libegl-mesa0 libgles2 libgbm1 python3 python3-pil grim curl ca-certificates procps >/dev/null
echo "chromium: $(chromium --version 2>&1 | head -1)"
echo "firefox: $(firefox-esr --version 2>&1 | head -1)"

# same Firefox policies as the real install
for d in /etc/firefox-esr/policies /etc/firefox/policies; do
  mkdir -p "$d"
  sed -n "/cat > \"\$d\/policies.json\" <<'EOF'/,/^EOF/p" /w/deploy/install.sh | sed '1d;$d' > "$d/policies.json"
done
python3 -c "import json; json.load(open('/etc/firefox/policies/policies.json')); print('policies.json OK')"

useradd -m w
mkdir -p /run/user/1000 /out && chown w /run/user/1000 /out && chmod 700 /run/user/1000
(cd /w/face && python3 -m http.server 8765 >/dev/null 2>&1 &)
sleep 1
URL=http://127.0.0.1:8765/

shoot() {  # shoot <name> <browser command...>: run it under cage for 25 s, then screenshot
  local name=$1; shift
  echo; echo "=================== $name"
  su w -s /bin/bash -c "export XDG_RUNTIME_DIR=/run/user/1000 WLR_BACKENDS=headless WLR_RENDERER=pixman \
      WLR_LIBINPUT_NO_DEVICES=1 HOME=/home/w; rm -rf /home/w/ud /home/w/ff; mkdir -p /home/w/ff;
    timeout 40 cage -d -- sh -c '$* >/tmp/$name.log 2>&1 & sleep 25; grim /out/$name.png; kill %1'" 2>&1 \
    | grep -vE 'dbus|Floss|UPower|ScreenSaver|PropertiesChanged' | tail -n 5
  grep -vE 'dbus|Floss|UPower|ScreenSaver|PropertiesChanged|crashpad|^Try ' /tmp/$name.log | tail -n 8
  [ -f /out/$name.png ] && python3 /w/iso/describe-screenshot.py /out/$name.png 2>/dev/null || echo "  (no screenshot)"
}

shoot chromium "chromium --kiosk --app=$URL --ozone-platform=wayland --no-first-run --user-data-dir=/home/w/ud"
shoot firefox  "env MOZ_ENABLE_WAYLAND=1 firefox-esr --kiosk --profile /home/w/ff $URL"
shoot firefox-nogpu "env MOZ_ENABLE_WAYLAND=1 MOZ_WEBRENDER=0 LIBGL_ALWAYS_SOFTWARE=1 firefox-esr --kiosk --profile /home/w/ff $URL"
cp /out/*.png /w/ 2>/dev/null || true
