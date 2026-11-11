# Cloud Link: the Walter app from anywhere

Cloud Link gives your Walter a private web address, like `https://bcdfg2345678.wallter.app`,
so the companion app on your phone works when you're away from home (and gets the
https address phones need for the microphone and notifications).

**Free during the beta.** You need an invite code from the Walter team.

## Privacy: what goes through it

- **Only the app.** The link carries `/app`, `/app/*` and `/api/app/*`. Everything else
  (the wall's face, settings pages, first-run pages, the rest of Walter) answers "not found"
  from outside. Every app request still needs a paired device's token, exactly as at home.
- **No names in the address.** The address is 12 random letters and digits (no vowels, so it
  can never spell a word or a name). Your household name is never sent.
- **What the Walter team sees:** your address, when your Walter last checked in (every 6
  hours: Walter's version and "the link is running", nothing else), and that's it. Your
  conversations, lists and calendar don't go through the team; the link is an encrypted
  tunnel straight to your Walter (run by Cloudflare's `cloudflared`).
- **Your Walter's own key.** On first use Walter makes an install ID and a private key
  (`~/.walter/secrets/cloud-link.key`, readable only by Walter). Every request to the Cloud
  Link service is signed with it, so nobody else can rotate or release your link.
- The tunnel token lives in `~/.walter/secrets/cloud-link.tunnel` (mode 600) and is handed to
  cloudflared through systemd's `EnvironmentFile`, never on a command line.

## Turn it on

Any one of these:

- **First-run wizard**: the "Reach Walter from anywhere" step. Type your invite code on your
  phone (or scan it from a photo), or leave the box empty to use the code you installed with.
- **Companion app**: Settings > Cloud Link > type the code > **Turn on with code**.
- **On Walter**: `walterctl cloud-link enable CODE`

It takes up to a minute. Walter claims the code, starts the link (systemd user unit
`walter-cloud-link.service`), sets the app's address (`[app] public_url`) and checks the
address answers. Then open the app through the new address on your phone.

Already have your own tunnel or Tailscale address for the app? Walter won't take over
unless you say so (`--replace`, or confirm in the app). Your own tunnel is left running
and untouched; Cloud Link only changes which address the app and pairing codes use.

## Check it, rotate it, turn it off

- `walterctl cloud-link status`: on or off, the address, whether the connector is running,
  what the service says, the last check-in.
- `walterctl cloud-link rotate`: get a fresh tunnel token (same address). Also in the app.
- `walterctl cloud-link disable`: releases the address, stops and removes the connector,
  deletes the token and puts your old app address back. In the app, **Turn off** asks first:
  if you're connected through Cloud Link at that moment, you'll lose remote access and the
  app only works on your home network until you turn it on again.

Your Walter's install key is kept after turning off, so turning on again later is the same
Walter (a code is still needed for a new address).

## Settings

```toml
[cloud_link]
api_base = "https://api.wallter.app"   # the Cloud Link service
heartbeat_h = 6.0                      # check-in interval while on
health_timeout_s = 60.0                # how long "enable" waits for the address to answer
```

`cloudflared` comes from Cloudflare's signed apt repository; the installer adds it
(set `WALTER_CLOUDFLARED=0` to skip). It only runs while Cloud Link is on.

## Troubleshooting

- "isn't valid": the code is mistyped, already used, or expired. Codes ignore case, spaces
  and dashes.
- "isn't answering yet": the first connection can take a minute or two; check again with
  `walterctl cloud-link status`. Still down: `systemctl --user status walter-cloud-link`.
- "the Walter team switched this link off" (status `revoked`): contact the team.

## For developers

`walter/cloud_link.py` (identity, signed API calls, unit, config), `walter/app_cloud_link.py`
(`GET/POST /api/app/cloud-link`), the wizard step in `walter/firstrun.py`, tests in
`tests/test_cloud_link.py` (a fake service that checks the signatures).

Signed request headers (contract v1): `X-Walter-Install` (install ID), `X-Walter-Ts` (unix
seconds, within 5 minutes), `X-Walter-Sig` = base64url Ed25519 signature over
`METHOD\nPATH\nTS\nsha256-hex-of-body`. Endpoints: `POST /v1/claim`, `GET /v1/status`,
`POST /v1/heartbeat`, `POST /v1/rotate`, `POST /v1/release`.
