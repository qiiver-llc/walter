# Walter troubleshooting (beta)

The 25 problems beta households are most likely to hit, drawn from what actually went wrong
while building Walter. Each has the **symptom**, the usual **cause** and the **fix**, with
exact commands. Commands run on Walter's laptop as the `walter` user (keyboard, or SSH), unless
they start with `sudo`.

Before anything else, two commands tell you a lot:

    walterctl status            # is he awake, muted, talking, what's running
    walterctl diagnostics       # redacted technical bundle (nothing is sent)

Logs: `journalctl --user -u walter -n 200` (as walter) or
`sudo -iu walter journalctl --user -u walter -n 200`.

**Restarting Walter** (`systemctl --user restart walter`, or app > Settings > Advanced > Restart)
takes about 5 seconds, but his ears are off for about 10 seconds. Background workers survive it.
Avoid stacking many restarts: most settings apply live with `walterctl config reload`.

---

## Sound and voice

### 1. No sound at all
- **Symptom:** captions appear but nothing is heard.
- **Cause:** volume at zero or muted, wrong output device, or speakers unplugged after boot.
- **Fix:** `walterctl volume unmute` then `walterctl volume set 70`. Check `walterctl say "testing"`.
  If still silent, rerun the mic-and-speaker step: app > Settings > Setup > Revisit setup steps >
  Mic and speaker, and pick the right speaker. A USB speaker plugged in after boot may need
  `systemctl --user restart walter`.

### 2. Garbled or choppy voice (especially the first sentence)
- **Symptom:** words dropped, robot stutter, mostly at the start of a reply.
- **Cause:** audio dropouts: the laptop's CPU is saturated and the sound server has no
  priority. On our reference box this was the cause every time, not the voice itself.
- **Fix:** check load with `uptime` and `walterctl tasks --priority`. Let busy workers finish or
  lower the number at once (app > Settings > Workers at once). Give the sound server realtime
  priority (asks for an admin yes): `sudo usermod -aG pipewire walter` then reboot, or
  `sudo apt install rtkit`. Undo: `sudo gpasswd -d walter pipewire`.

### 3. Crackling or echo while Walter talks
- **Symptom:** crackle, or Walter interrupting himself.
- **Cause:** "barge-in" (listening while talking) makes the echo canceller fight a USB mic and
  laptop speakers running on different clocks.
- **Fix:** turn interrupting off (fresh installs default to `auto`, i.e. on):
  `walterctl config set ears.barge_in off`, then restart Walter once (`wake` = only his name
  interrupts). A single USB speakerphone (mic + speaker in one) works best if you want it on.
  Back to the default: `walterctl config set ears.barge_in auto` and restart.

### 4. Voice too fast, slow, high or low
- **Fix:** `walterctl config set speech.tts_pitch 0.95` (pitch) and check
  `walterctl config get voice` for speed keys; changes apply live. Cloud voice instead:
  `walterctl voice openai` (needs your own OpenAI key; falls back to local on any error).

### 5. Walter went silent mid-reply on cloud voice
- **Cause:** the daily cloud speech cap was hit.
- **Fix:** `walterctl speech status` shows usage and caps. Go local: `walterctl voice local`
  and `walterctl ears local`.

## Hearing

### 6. Walter doesn't hear me
- **Symptom:** you speak, nothing happens; no "Listening" tag.
- **Cause:** mic muted (Space bar, app, or the USB mic's own button), asleep, or wrong mic.
- **Fix:** `walterctl status` (look for muted / hw_muted / asleep). `walterctl unmute`, or press
  Space. If the USB mic's own mute light is on, press its button. Then rerun the mic test:
  app > Settings > Setup > Revisit setup steps > Mic and speaker.

### 7. USB speakerphone mute button
- **Symptom:** Walter is "unmuted" but hears nothing.
- **Cause:** many USB speakerphones mute **in their own chip**: the computer gets silence, no
  signal. Walter detects the digital silence and shows the mic as hardware-muted.
- **Fix:** press the mute button on the device again. Volume buttons on supported devices
  (e.g. JLab GO Talk) are mapped by `bin/walter-micbuttons` (a user service); check it with
  `systemctl --user status walter-micbuttons`.

### 8. "Did you hear me?" (he answers some things but not others)
- **Cause:** you spoke without his name after the follow-up window closed, or the speech was
  judged to be for someone else (TV, guests). He ignores unknown voices unless his name is used.
- **Fix:** start with his name. Look at what was ignored and why:
  `~/walter/skills/self-review.sh` (as walter) or the "ignored/rejected" lines in the log.

### 9. Walter mishears names and words
- **Fix:** speak a little closer, one request at a time. Cloud ears are more accurate:
  `walterctl ears openai` (own key). Say "change it" right after a wrong list add.

### 10. Walter answers the TV
- **Fix:** keep the camera presence and voice recognition on if you're comfortable (both opt-in),
  enrol yourself with `walterctl enroll voice NAME`, and he'll stay quiet for unknown speakers
  unless his name is said.

## Screen

### 11. Face frozen
- **Symptom:** eyes stop moving, clock stuck, but Walter still talks.
- **Cause:** on some Intel graphics the kiosk compositor stops drawing new frames under load.
- **Fix:** `walterctl reload-face` first. If still frozen, restart the kiosk: `pkill -x cage`
  (screen goes dark for a few seconds and comes back), or reboot.

### 12. Black screen
- **Cause:** the kiosk didn't start, the screen is asleep, or Walter's service is down.
- **Fix:** press Space. Then `systemctl --user status walter` and `tail ~/.walter/kiosk.log`.
  Restart: `systemctl --user restart walter`, then reboot if still black.

### 13. Screen is sideways or upside down
- **Fix (instant, no restart):** `walterctl display css-rotate 90` (or 270, or 0 to undo).
- **Fix (real rotation):** `walterctl display portrait|portrait-flipped|landscape`, applied at the
  next kiosk start (needs `wlr-randr`: `sudo apt install wlr-randr`). Don't use both at once, or
  it turns twice. `walterctl display status` shows what's set.

### 14. Text too small or too big
- **Fix:** `walterctl display text-scale 0.9` (0.5 to 1.5), live in about 2 seconds.

### 15. Background picture makes Walter look upset
- **Fix:** `~/walter/bin/walter-background auto` (follows time of day and season) or
  `walter-background set NAME` (`walter-background list`).

## Phone app and remote access

### 16. App won't pair
- **Symptom:** code rejected, or the page doesn't open.
- **Cause:** code expired (5 minutes), 5 wrong tries burned it, or the phone isn't on the same
  Wi-Fi.
- **Fix:** `walterctl pair --cancel` then `walterctl pair` for a fresh code. Phone on the home
  Wi-Fi; open `http://WALTER-IP:8765/app` directly if the QR fails. Too many wrong tries
  blocks that phone for 15 minutes.

### 17. App says "unauthorised" or stopped working
- **Cause:** device was revoked, or unused longer than the idle-expiry setting.
- **Fix:** `walterctl devices` to check; pair again. `walterctl devices idle-expiry 0` disables
  idle expiry.

### 18. Phone mic or notifications don't work in the app
- **Cause:** browsers only allow the mic and Web Push over https; iPhone also needs the app on
  the Home Screen.
- **Fix:** use the Cloud Link (or your own Tailscale) https address, set as `[app] public_url`
  in `~/.walter/config.toml`, then `walterctl config reload`. Then Walter tab > Settings >
  Turn on notifications.

### 19. Cloud Link (tunnel) offline
- **Symptom:** app works at home, not away; https address times out.
- **Cause:** Walter's laptop is off or offline, the tunnel connector stopped, or the link isn't
  switched on yet (beta: the team turns it on).
- **Fix:** check Walter is online at home. `walterctl security status` shows the tunnel state.
  Restart the connector if your install has one (`systemctl --user list-units 'walter-tunnel*'`;
  then `systemctl --user restart walter-tunnel`). Still down:
  send feedback with diagnostics; the team checks the tunnel from their side. (Self-service
  tunnel repair: **planned**.)

## Brain and services

### 20. Claude sign-in expired / Walter says he can't think
- **Symptom:** every request fails, or "not logged in" in the log.
- **Cause:** Claude's sign-in lapsed, the subscription ended, or the API key was revoked.
- **Fix:** app > Settings > Setup > Revisit setup steps > **Connect Walter's brain**: Claude's
  own sign-in window opens on Walter's screen (keyboard needed). Or, at Walter's keyboard as
  walter: `claude auth login`; check with `claude auth status`. Walter never sees your Claude
  password. Rate limit or Anthropic outage: wait and try again (no friendly wall message yet:
  **planned**).

### 21. Calendar on the board is out of date
- **Cause:** the calendar is pushed to the board on Walter's regular check-ins, not live; a
  long quiet stretch, sleep or an expired Google connection leaves it stale.
- **Fix:** ask "What's on my calendar today?" (refreshes it), or `walterctl dash refresh`.
  If Google errors, see the next item.

### 22. Google disconnects every 7 days
- **Cause:** while the Google project is in "Testing" mode, Google expires the sign-in after
  7 days. This is a Google rule.
- **Fix:** reconnect: `walterctl google connect` (QR, tap Allow). `walterctl google accounts`
  shows who's connected. Permanent fix for self-hosted Google clients: Google Cloud console >
  Audience > **Publish app** (no review needed for personal use).

### 23. Walter is slow / high CPU / fan always on
- **Cause:** too many workers at once, live captions transcription, or a 2-core laptop.
- **Fix:** `uptime`, `walterctl tasks --live`, `walterctl tasks --priority`. Cancel a stuck
  worker: `walterctl cancel-task N`. Lower workers at once in the app. Lighter ears:
  `walterctl config set ears.live_partials false` or
  `walterctl config set ears.whisper_model tiny.en`. 8 GB RAM and 4 threads are the
  comfortable minimum.

## Updates

### 24. Update failed, or Walter broke after an update
- **Fix:** `walterctl update status`, then `walterctl update rollback` (refuses while workers
  run; add `--force` if you must). Logs: `/var/log/walter-update.log`. Updates also wait while
  workers are busy; that's normal.

### 25. Walter changed his own code and won't start
- **Cause:** Walter can improve himself; a bad self-edit can stop the service.
- **Fix:** as walter: `~/walter/deploy/rescue.sh` lists recent changes; `rescue.sh COMMIT`
  rolls his body back to that commit and restarts. On release installs, `walterctl update
  rollback` also works.

---

## Still stuck?
Send feedback from the app (Settings > Feedback, type **bug**, tick "attach diagnostics" after
previewing it), or reply to your invite email with the output of `walterctl diagnostics`.
Never send passwords, keys or tokens.
