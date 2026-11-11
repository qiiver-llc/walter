# Security policy

Walter is an AI agent that lives in people's homes, with a microphone, a shell and the
network, so we take security reports seriously and are grateful for them.

## Reporting a vulnerability

**Please don't open a public issue, discussion or pull request for a security problem.**

- Email **hello@wallter.app** with "SECURITY" in the subject, or use GitHub's
  "Report a vulnerability" (private security advisory) on this repository if it's enabled.
- Include what you found, how to reproduce it, which version (`cat VERSION` or
  `walterctl version`) and what an attacker could do with it.
- Please don't include real personal data, and don't test against households other
  than your own.

We aim to acknowledge reports within 3 business days and to keep you posted until it's
fixed. Fixes ship as a signed release; we're happy to credit you once it's out (tell us
if you'd rather stay anonymous).

## Supported versions

Walter is in beta. Only the **latest release** on releases.wallter.app (and the current
`main` branch) gets security fixes; devices update themselves to it.

## Scope

In scope:
- The device code in this repository: the service (`walter/`), face, companion app and
  its server, pairing and auth, the installer and updater (`deploy/`), release
  verification, the desktop face and "hands".
- Prompt injection that makes Walter act (run commands, send, reveal secrets, change
  settings) from untrusted text such as web pages, email, calendar invites or feeds,
  beyond what docs/SECURITY.md already documents as accepted risk.
- Ways to reach Walter's local API or the app without pairing, from the LAN or through
  the Cloud Link tunnel.

Out of scope:
- Anthropic's Claude, OpenAI, Google, Cloudflare and other third-party services
  themselves (please report those to their owners).
- Someone with physical access to the unlocked laptop or the owner's own accounts.
- Installs the owner deliberately configured unsafely (e.g. API bound to `0.0.0.0`,
  `bypassPermissions` with admin rights) where the docs warn about it.
- Spoken instructions from people in the room: the wake word is a convenience, not
  authentication (documented in docs/SECURITY.md).

The threat model and mitigations are in [docs/SECURITY.md](docs/SECURITY.md).
