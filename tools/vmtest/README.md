# Clean-machine install test (VM)

Proves the beta installer works on a blank Debian 13 machine, no spare laptop needed.

```
tools/vmtest/run.sh                 # the real beta path (published install.sh + signed release)
tools/vmtest/run.sh --local-tree    # this checkout's HEAD, laid out like a signed release
tools/vmtest/run.sh --keep          # leave the run dir + VM disk for poking at (VM is still stopped)
tools/vmtest/run.sh --release 0.1.0 --post tools/vmtest/scenario-update.sh
                                    # install an OLD signed release, then update -> rollback ->
                                    # update again -> tamper tests (bad sha256 / bad sig / foreign key)
```

`--prep CMD` runs a command in the VM before installing (e.g. enabling `non-free-firmware`
so 0.1.0, which predates the firmware fix, installs on a cloud image). `scenario-update.sh`
needs tamper channels (build them first: `tools/vmtest/make-tamper.sh`) in `~/.walter/vmtest/tamper/{badsha,badsig,otherkey}/` (each a
`latest.json` advertising a newer version + tarball + .sig; see the script header). QEMU
runs under `nice -n 10`; run one VM at a time on the 4-thread wall laptop.

What it does:

1. Installs `qemu-system-x86 qemu-utils cloud-image-utils` if missing (`sudo -n apt-get`).
2. Downloads the official `debian-13-genericcloud-amd64.qcow2` from cloud.debian.org,
   checked against the published `SHA512SUMS`; cached in `~/.walter/vmtest/img/`.
3. Every run gets a fresh 20 GB qcow2 **overlay** (the base image is never written), a
   throwaway ed25519 key and a cloud-init seed creating user `walter` (passwordless sudo).
4. Boots headless with KVM (software emulation if `/dev/kvm` isn't usable: slow),
   2 vCPU, 4 GB RAM, user-mode NAT only (outbound internet; nothing can reach in except
   ssh forwarded to a random port on 127.0.0.1).
5. Runs `curl -fsSL https://releases.wallter.app/install.sh | sudo bash -s -- --invite TEST`
   (or the `--local-tree` equivalent) and times it.
6. Copies `checks.sh` in and runs it: files and binaries, `systemctl --user` services,
   `/api/status`, the first-run wizard page `/firstrun` on 127.0.0.1:8765,
   `walterctl update status`, a no-op update via `walter-update.service`, and a rollback.
   The VM has no display, sound card, mic or camera, so kiosk/audio failures are expected
   and listed separately under "hardware-dependent".
7. Powers the VM off and deletes the disk overlay + seed.

Output per run in `~/.walter/vmtest/runs/<timestamp>/`: `install.log`, `checks.log`
(PASS/FAIL/WARN lines + details), `console.log` (serial), `harness.log`, `summary.json`.

Typical timing on the wall laptop (KVM): boot ~20 s, install ~10-15 min (pip + Whisper
+ Piper downloads dominate).

Notes
- `--local-tree` packs `git HEAD` (commit first!) and installs it with
  `WALTER_RELEASE_VERSION` = the channel's current version, so the update check sees
  "up to date" and the no-op update is meaningful.
- Don't edit `run.sh` while a run is going: bash reads scripts lazily.
- Nothing is published; this only reads from the release channel.
