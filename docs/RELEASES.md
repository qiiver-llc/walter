# Walter releases and the update channel

Beta households get Walter, and every update, from **https://releases.wallter.app**:
a Cloudflare Pages project (`walter-releases`, direct upload, static files only).
GitHub is for code and contributions only; devices never update from it.
Releases are built and **signed on Walter's own box**; the signing key never
leaves `~/.walter/secrets/release-signing.key` (mode 600).

## What the channel serves
| Path | What | Cache |
|---|---|---|
| `/latest.json` (+ `.sig`) | newest version, sha256, tarball + signature URLs | 60 s |
| `/install.sh` (+ `.sig`) | bootstrap installer, taken from the newest signed tarball | 60 s |
| `/walter-X.Y.Z.tar.gz` (+ `.sig`) | the release (Ed25519 detached signature) | 1 year, immutable |
| `/manifest-X.Y.Z.json` (+ `.sig`) | version, date, commit, sha256, changelog, min installer | 1 year, immutable |
| `/release-public.pem` | public key, for humans to verify by hand | 5 min |

Only the newest 5 releases are deployed (Pages limit is 25 MiB per file; a release is
about 11 MB). Older ones stay in the local mirror `~/.walter/releases-mirror`.
`_headers` sets `nosniff`, `X-Frame-Options: DENY`, a sandboxing CSP and CORS limited
to `https://wallter.app`. A real `404.html` stops Pages from answering unknown paths with
HTML. New installs:

    curl -fsSL https://releases.wallter.app/install.sh | sudo bash -s -- --invite CODE

The installer defaults to this channel only in bootstrap mode; re-running
`deploy/install.sh` from a checkout never adds a channel (the owner's own box keeps its
`/etc/walter/update.conf` as is).

**USB installer.** `iso/build-iso.sh` writes `UPDATE_CHANNEL` (default `default`) into the
stick, and its first boot runs `deploy/install.sh --channel default`, which writes the same
root-owned `WALTER_UPDATE_URL=https://releases.wallter.app` line the bootstrap does (same
nightly timer, same sha256 + signature check). `WALTER_ISO_CHANNEL=none` (or an https URL)
at build time changes that. A stick built from a clean commit tagged `vX.Y.Z` installs as
that release (release-managed, "up to date" on the channel). A stick built from any other
commit installs as a plain git copy (VERSION = commit id) and moves onto the newest signed
release at its first update check (about 15 minutes after boot; its git copy is kept as
`legacy-...` for `walterctl update rollback`). That can be OLDER code than the stick, so
publish a release first and build sticks from its tag.

## Cutting and publishing a release
1. Commit everything (make-release refuses a dirty tree), run the test suite.
2. `tools/release/publish-release.sh` (next patch after the newest `vX.Y.Z` tag) or
   `tools/release/publish-release.sh 0.2.0`. `--dry-run` builds and assembles without
   deploying. `--setup` (first time only) creates the Pages project, attaches
   `releases.wallter.app` and its CNAME; it never touches other DNS records.
3. The script: refuses a version <= what the channel (or the local mirror) already has;
   builds with `make-release.sh` (refuses private words, signs); verifies the signature
   with the committed `deploy/release-public.pem`; assembles the site; deploys with
   wrangler (token read from `~/.walter/secrets/cloudflare.token` into the environment,
   never printed); then downloads `latest.json`, the tarball, `install.sh` and all
   signatures over https and checks sha256 + signatures with the updater's own code
   (`walter_release.read_latest` / `verify`); finally tags `vX.Y.Z` locally.
4. Devices pick it up at their nightly check (~3:40 AM) or with `walterctl update`.
   Nothing is pushed to GitHub by any of this.

## Rollback
- **One device**: `walterctl update rollback` (switches back to the previous version).
- **Whole channel**: versions only go forward, so roll *forward*: revert the bad commits,
  then publish a new, higher version. Devices that already took the bad one move to it
  that night. For an emergency, point `latest.json` back by redeploying an older site:
  in the Cloudflare dashboard, Pages > walter-releases > Deployments > a previous
  deployment > "Rollback to this deployment". Devices that installed the bad version
  will NOT downgrade (the updater only moves to newer versions); they need the
  roll-forward release.

## Key custody
- Private key: `~/.walter/secrets/release-signing.key` on the release box only. Never on
  GitHub, Cloudflare, in CI, in a worker brief, or in a backup that leaves the house
  unencrypted. Offline copy: an encrypted USB stick kept by the owner (to do).
- Public key: `deploy/release-public.pem`, embedded in `deploy/install.sh` (a test keeps
  them equal) and installed on devices at `/usr/local/share/walter/release-public.pem`.
- Cloudflare account ID: `CLOUDFLARE_ACCOUNT_ID` or `~/.walter/secrets/cloudflare.account` (never in the repo).
- Cloudflare token: `~/.walter/secrets/cloudflare.token`; it can deploy the site but cannot
  sign, so a stolen token can only serve files devices will refuse.

## Key rotation (planned, yearly or when the box changes)
1. `walter_release.py genkey` a new key (new key id = the new file's fingerprint).
2. Ship a release **signed with the old key** whose updater trusts both keys
   (`release-public.pem` + `release-public-next.pem`) and whose installer embeds the new one.
3. Once most devices run it, sign releases with the new key only; then a later release
   drops the old public key.
(Steps 2 and 3 need multi-key support in `deploy/update.sh`; not built yet.)

## If the signing key leaks
Anyone holding it can sign releases that devices will accept, so act the same day:
1. Generate a new key on a clean machine.
2. Immediately publish an emergency release, signed with the old key (it is the only key
   devices trust), that installs the new public key, removes the old one, and sets a
   signed **minimum version** (`min_version` in latest.json and the manifest, checked by
   the updater) equal to itself, so nothing older or signed by the old key is accepted
   afterwards. Rotate the Cloudflare token too and check the Pages deployment history.
3. Tell beta households (email) what happened; devices that were offline should re-run
   the installer one-liner, which embeds the new key.
The `min_version` check and multi-key trust are on the backlog; until they exist, step 2
means an updater change shipped in that same release.
