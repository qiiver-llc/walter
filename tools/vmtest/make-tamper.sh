#!/usr/bin/env bash
# Build the tamper channels scenario-update.sh serves (default ~/.walter/vmtest/tamper):
#   tools/vmtest/make-tamper.sh [CHANNEL] [VERSION]   (VERSION = a real signed release, default: latest)
set -euo pipefail
CH="${1:-https://releases.wallter.app}"; CH="${CH%/}"
V="${2:-$(curl -fsSL "$CH/latest.json" | python3 -c 'import json,sys;print(json.load(sys.stdin)["version"])')}"
NEXT=$(python3 -c "a,b,c='$V'.split('.');print(f'{a}.{b}.{int(c)+1}')")
T="${TAMPER_DIR:-$HOME/.walter/vmtest/tamper}"
rm -rf "$T"; mkdir -p "$T"/{dl,x,badsha,badsig,otherkey}
curl -fsSL -o "$T/dl/r.tar.gz" "$CH/walter-$V.tar.gz"; curl -fsSL -o "$T/dl/r.sig" "$CH/walter-$V.tar.gz.sig"
tar -xzf "$T/dl/r.tar.gz" -C "$T/x" --no-same-owner
mv "$T/x/walter-$V" "$T/x/walter-$NEXT"; echo "$NEXT" > "$T/x/walter-$NEXT/VERSION"; echo 'echo pwned' > "$T/x/walter-$NEXT/EVIL.sh"
tar -czf "$T/dl/mod.tar.gz" -C "$T/x" "walter-$NEXT"; rm -rf "$T/x"
mk() { printf '{"format":1,"version":"%s","date":"2026-01-01T00:00:00Z","tarball_url":"walter-%s.tar.gz","sha256":"%s","signature_url":"walter-%s.tar.gz.sig","min_installer_version":2}\n' \
  "$NEXT" "$NEXT" "$2" "$NEXT" > "$T/$1/latest.json"; }
# genuine signed tarball, wrong sha256
cp "$T/dl/r.tar.gz" "$T/badsha/walter-$NEXT.tar.gz"; cp "$T/dl/r.sig" "$T/badsha/walter-$NEXT.tar.gz.sig"
mk badsha "$(echo x | sha256sum | cut -d' ' -f1)"
# modified tarball, correct sha256, original signature
MS=$(sha256sum "$T/dl/mod.tar.gz" | cut -d' ' -f1)
cp "$T/dl/mod.tar.gz" "$T/badsig/walter-$NEXT.tar.gz"; cp "$T/dl/r.sig" "$T/badsig/walter-$NEXT.tar.gz.sig"; mk badsig "$MS"
# modified tarball signed by a throwaway key that isn't Walter's
openssl genpkey -algorithm ed25519 -out "$T/dl/evil.pem" 2>/dev/null
cp "$T/dl/mod.tar.gz" "$T/otherkey/walter-$NEXT.tar.gz"
openssl pkeyutl -sign -rawin -inkey "$T/dl/evil.pem" -in "$T/dl/mod.tar.gz" -out "$T/otherkey/walter-$NEXT.tar.gz.sig"; mk otherkey "$MS"
echo "tamper channels for fake $NEXT (from $V) in $T"
