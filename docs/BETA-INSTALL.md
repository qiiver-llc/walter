# Walter beta: install guide

Thanks for testing Walter. This is a private, invite-only beta: things will break, and
your feedback is the point.

## What you need
- A spare laptop: 64-bit Intel/AMD, about 2015 or newer, **8 GB RAM** (4 GB works, slowly),
  **32 GB free disk**, built-in screen, webcam optional. Typical closet laptops are fine.
- A microphone and speaker (built-in works; a USB speakerphone is better).
- Wi-Fi or Ethernet, and a phone on the same network for setup.
- A fresh **Debian 12 or 13** install (the minimal "netinst" with no desktop is ideal).
  Everything on that laptop is dedicated to Walter.
- A Claude subscription or an Anthropic API key. You sign in on Walter's own screen with
  Claude's official login; Walter never sees your Claude password or login code.

## Install
On the laptop, as a user with sudo (keyboard attached), run the line from your invite
email. It looks like this:

    curl -fsSL https://releases.wallter.app/install.sh | sudo bash -s -- --invite YOUR-CODE

Updates come from the same place (https://releases.wallter.app, Walter's signed release
channel; never GitHub). It downloads the newest Walter release, checks its fingerprint (sha256) **and** its
digital signature, refuses anything that doesn't match, and installs. Takes 15 to 40
minutes. Then reboot: the first-run wizard appears on the screen; scan its QR code with
your phone and follow along.

## First-run wizard (about 15 minutes)
Shown big on Walter's screen; your phone drives it (same Wi-Fi). Every step can be skipped
and revisited later from the app: **Settings > Setup > Revisit setup steps**.

1. **Welcome**: name your Walter (he'll answer to that name and to "Walter") and your
   household name. Both stay on the box.
2. **Network, time and place**: internet check, your phone's time zone, and your town
   (searched via Open-Meteo; only the typed town name leaves the box) for weather.
3. **Mic and speaker**: play a test sound and set the volume, then a 3-second mic test
   ("Hey Walter") with a level meter and a clear pass/fail; pick other devices if needed.
4. **Connect Walter's brain**: Claude's own sign-in window on Walter's screen (keyboard
   needed), or your own Anthropic API key. Walter never sees your Claude password or code.
5. **Privacy choices**: camera, cloud speech, face and voice recognition and full admin
   rights are all **off** unless you tick them; helpers' permission mode defaults to
   `acceptEdits` (recommended for the beta).
6. **Extras**: Google calendar (phone approval), podcasts to follow, your teams. (More, like a verse of the day, are add-ons: app Settings > Add-ons.)
7. **Cloud Link**: ask for remote access. In the beta this only saves a request (with your
   invite code) on the box, in `/etc/walter/firstrun/cloudlink-request.json`; the Walter team
   switches it on. Your Walter makes no Cloudflare calls itself.
8. **Pair your phone**: a 6-digit code + QR for the companion app.
9. **Say hello**: say "Hey Walter, hello" and get a short tour.

Progress is saved in `/etc/walter/firstrun/state.json`, so a reboot picks up where you left
off. Time from the start to the first words Walter hears, and to the finish, is logged only
on the box in `~/.walter/firstrun-metrics.jsonl` (beta goal: under 20 minutes).

The wizard runs only on a fresh install: the installer writes `/etc/walter/firstrun/pending`
when it creates a new Walter. It's finished (and never shows again) once
`/etc/walter/firstrun/done` or `/etc/walter/firstrun-done` exists; existing installs get
`/etc/walter/firstrun-done` from the installer. To run it again: `sudo rm
/etc/walter/firstrun/done /etc/walter/firstrun/state.json; sudo -u walter touch
/etc/walter/firstrun/pending`, then restart Walter.

Optional, safer brain mode (Walter can't run arbitrary commands or reprogram himself):
add `--permission-mode acceptEdits` after the invite code.

## Safe defaults on a fresh install
| Setting | Default | Change it |
|---|---|---|
| Admin rights (passwordless sudo) | **off** | opt-in box on the setup page; later only with a USB stick (docs/admin-access.md) |
| Camera (presence) | **off** | `camera = true` in ~/.walter/config.toml |
| Cloud speech (OpenAI) | **off**, all speech is local | `walterctl voice openai` with your own key |
| Face / voice recognition | **off**, nobody enrolled | each person says yes, then `walterctl enroll face NAME` |
| Brain permission mode | `acceptEdits` if you keep the wizard's recommendation (step 5); otherwise `bypassPermissions` (full power as the walter user) | `--permission-mode acceptEdits` at install, or `permission_mode` in config |

**About `bypassPermissions`:** it lets Walter write code, fix himself and run tools without
asking. The risk is prompt injection: text he reads (web pages, emails, calendar invites)
could try to talk him into running commands. Keep admin rights off unless you understand
that; choose `acceptEdits` if you'd rather he couldn't.

## What leaves the device
- **Your conversations** go to Anthropic (Claude) to be answered, under your own account.
- **Updates**: Walter downloads signed releases from the beta channel. No usage data is sent.
- **Weather** (your rough location), podcasts/YouTube you ask for, and web pages he looks up.
- **Only if you turn them on**: cloud speech (OpenAI), Google/calendar connections,
  phone notifications (Apple/Google push), remote access through a tunnel.
- **Never**: camera images, microphone recordings, or recognition data. Speech recognition,
  wake word, presence and recognition all run on the laptop.

## Updates and rollback
Walter checks for a new signed release nightly (about 3:40 AM). Each version lives in its
own folder and the switch is atomic; the previous one is kept:

    walterctl update status          # which version is running
    walterctl update                 # check now
    walterctl update rollback        # back to the previous version (restarts Walter)

Updates wait while Walter has background jobs running. Turn them off with
`WALTER_UPDATE=off` in /etc/walter/update.conf.

## Getting help
Reply to your invite email, or message the beta contact in it. Useful to include:
`walterctl status`, `walterctl update status`, and the last lines of
`/var/log/walter-update.log`. Logs: `sudo -iu walter journalctl --user -u walter -n 200`.
Never send passwords, API keys or tokens.

## Clean-machine test checklist (for each beta release)
1. Fresh Debian 13 netinst in a VM (2 CPU, 4 GB, 32 GB disk) or a wiped laptop; note the release version.
2. Run the bootstrap line with a test invite code; it prints "verified (sha256 + signature)".
3. Tamper test: serve a tarball with one byte changed; the installer must print "REFUSED" and install nothing.
4. `/etc/walter/invite` holds the code (mode 640); `/etc/walter/update.conf` has `WALTER_UPDATE_URL=https://releases.wallter.app`.
5. `~walter/walter` is a symlink to `.walter-releases/current` -> the version.
6. `~walter/.walter/config.toml`: `camera = false`, `recognise_faces = false`, `recognise_voices = false`,
   `tts_provider = "local"`, `stt_provider = "local"`; `/etc/sudoers.d/90-walter` does **not** exist.
7. Reboot: QR code shows, phone setup works, Claude sign-in on the wall works, "Hey Walter" gets an answer.
7a. First-run wizard: all 9 steps work from the phone; reboot mid-way resumes at the same step; skipping
   works; finishing writes `/etc/walter/firstrun/done` and a `finished` line (with `first_talk_s`) in
   `~/.walter/firstrun-metrics.jsonl`; time it with a stopwatch (target: under 20 minutes).
8. Publish a newer test release to the channel; `sudo walter-update --now` installs it, Walter restarts,
   `walterctl update status` shows current = new, previous = old.
9. `walterctl update rollback`: Walter comes back on the old version.
10. Start a worker (`walterctl task ...`), run `sudo walter-update --now`: it refuses and logs why.
11. `grep -ri` the install for the release machine's personal words: nothing.
