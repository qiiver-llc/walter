# Contributing to Walter

Thanks for helping! Walter is a product of Qiiver LLC, developed in the open. Issues and
pull requests are welcome. Nothing merged here reaches anyone's home until a maintainer
cuts and signs a release.

## How to submit a change
1. Fork the repository and make a branch. (Nobody outside the maintainers has push
   access; every change comes in as a pull request.)
2. Keep changes small and focused; add or update tests in `tests/`.
3. Run `python -m pytest -q` and `python -m py_compile walter/*.py` locally (and
   `node --test tests/*.test.js` if you touched the face or the app).
4. Open a pull request describing what changes for a person living with Walter, and how
   you tested it.
5. **Never include personal data** in code, tests, comments or sample data: no real
   names, addresses, emails, phone numbers, Wi-Fi names, recordings, photos or keys. Use
   "Alex", "the owner", `example.com` and obviously fake keys.

For bigger changes (new features, new integrations, anything touching security), please
open an issue first so we can agree on the approach.

## Review
- A Qiiver maintainer reviews every pull request. Changes to `deploy/`, `tools/release/`,
  `walter/security.py`, the app server, auth, the updater or anything that runs as root
  get extra care and a second look.
- No new network destinations, telemetry or dependencies without discussion. New
  dependencies must be pinned and come from PyPI or Debian.
- Anything a web page, email or feed says stays data, never instructions (see
  docs/SECURITY.md).

## What contributors can and can't touch
- Contributors have no access to releases, signing keys, the update channel
  (releases.wallter.app), Cloudflare, the Cloud Link service or any household.
- CI on pull requests runs tests only, with read-only permissions and no secrets.

## Signed releases only
Devices install only tarballs signed by Qiiver's release key, verified (sha256 + Ed25519)
before unpacking. Releases are built from a maintainer's reviewed tree, checked for
personal data, signed offline and published to releases.wallter.app (see
docs/RELEASES.md). Builds from a fork, a PR artifact or GitHub itself are never trusted
by devices.

## Licence
By contributing you agree that your contribution is licensed under the Apache License
2.0 (see LICENSE), as section 5 of that licence describes.

## Conduct and security
Please follow our [Code of Conduct](CODE_OF_CONDUCT.md). Security problems: don't open
a public issue; see [SECURITY.md](SECURITY.md).
