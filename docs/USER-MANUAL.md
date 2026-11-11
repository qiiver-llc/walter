# Walter user manual (beta)

Walter is a friendly assistant that lives on a screen on your wall: a glowing blob with two big
eyes. You talk to him out loud and he talks back. He can answer questions, keep your list,
read your calendar, play podcasts and YouTube, set reminders, and run longer jobs in the
background while he keeps chatting with you.

He's a product of Qiiver LLC. This is an invite-only beta: things will break, and your
feedback (Settings > Feedback in the phone app) is the point.

Things marked **(planned)** are not built yet.

---

## 1. Talking to Walter

- **Wake him** by saying his name: "Hey Walter, what's the weather?" (or the name you gave
  him in setup). After he answers you can keep talking for a few seconds without the name.
- **Talk normally.** One request at a time works best. If he mishears, just say it again or
  say "change it" right after he adds something to a list.
- **Interrupting.** On a fresh install you can talk over Walter to stop him (his echo
  canceller tells your voice from his). If his voice crackles or he keeps cutting himself
  off, turn it off: `walterctl config set ears.barge_in off` and restart Walter once; then
  his mic is deaf while he talks (our own test unit runs this way, for the cleanest voice).
  `ears.barge_in wake` is the middle ground: only his name or "stop" interrupts.
  Any time, you can stop him with **Space** on the keyboard, **Stop talking** in the phone
  app, or `walterctl stop`.
- **Mute the mic**: press **Space** on Walter's keyboard (press again to unmute), or tap
  **Wall mic** in the app. A USB speakerphone's own mute button also works: Walter notices
  the mic went silent and shows it.
- **Volume**: "Walter, louder" / "quieter", the **Up/Down arrow** keys, or the app's slider.
- **Sleep**: press **Z** or say "Walter, go to sleep". He shrinks into a corner, the mic and
  camera turn off and the screen becomes a dashboard (clock, weather, calendar, your list).
  **Space** wakes him. He also sleeps by himself during quiet hours (10 PM to 8 AM by default).
- **Captions**: what he says appears as subtitles. `walterctl captions off` hides them.

Things to try:
- "What's on my calendar tomorrow?"
- "Add milk to the shopping list."
- "Remind me in 20 minutes to take the bread out."
- "Play the latest episode of [podcast]."
- "Look into the best robot vacuums under 300 dollars." (a long job; he'll tell you when it's done)

## 2. The board (what's on the screen)

On a tall (portrait) screen Walter sits small at the top and an info board fills the rest.
On a wide screen he's big and the extras appear around him.

| Part | What it shows |
|---|---|
| Clock and weather | Time, today's weather and alerts (refreshes every 15 minutes) |
| Calendar | Today's and tomorrow's events (once Google is connected) |
| Your list | Tasks, shopping, notes, parked ideas |
| Scores | Your teams' games and results (pick teams in setup) |
| Now playing | The podcast or video on the speakers, with cover art and what's next |
| While you were away | Things Walter couldn't say out loud (muted, asleep, nobody home) |
| Loop chip | "Loop: ..." what Walter is working on by himself; an amber dot means he's waiting on you |
| Workers card | Background jobs running right now and what each is doing |
| Usage meter | How much of your Claude allowance has been used (lower left) |

Change text size: `walterctl display text-scale 0.9` (0.5 to 1.5). Layout:
`walterctl display layout auto|compact|large`.

## 3. The phone app

A small web app for your phone or computer, served by Walter himself (no app store).

**Pair it:** say "Walter, pair my phone" (or run `walterctl pair`). A 6-digit code and a QR
code appear for 5 minutes. Scan the QR with your phone camera, type your name and a device
name, tap **Pair**. On iPhone, then use Share > **Add to Home Screen**.

Four tabs at the bottom:
- **Chat**: type, or hold the mic button and talk. Replies come to the phone only, not the
  wall, unless you say "on the wall" or turn on the speaker button. The **paperclip** sends
  up to 4 photos ("what plant is this?"); photos are deleted from Walter after 7 days.
- **Today**: "While you were away" and your calendar.
- **List**: your list; tick things off, add new ones.
- **Walter**: wall mic on/off, awake/asleep, stop talking, volume, the **Player** (pause,
  skip, speed for podcasts and YouTube), the **Remote** for the wall browser, workers and
  reminders, and **Settings**:
  - speech, notifications ("Turn on notifications"; needs the https address), wall text size,
    how many workers at once, this device, version, unpair
  - **Setup > Revisit setup steps** (rerun any part of the first-run wizard)
  - **Feedback**: send a bug, idea or praise, optionally with a photo and a diagnostics
    bundle you can preview first
  - **Advanced > Restart Walter**

Lost phone? On Walter: `walterctl devices`, then `walterctl revoke "Device name"` (or
`walterctl revoke-all`). It stops working within seconds.

## 4. Podcasts, YouTube and radio

**Podcasts** (free public feeds, played on Walter's speakers):
- "Follow [show name]" / "Unfollow [show]" / "What podcasts do I follow?"
- "Play [show]" (resumes where you left off, or the newest unplayed episode)
- "Pause", "Resume", "Skip 30 seconds", "Go back 15", "Play it at 1.25 speed", "Stop"
- "What's playing?"

**YouTube** (on the wall, in a panel beside Walter):
- "Play [something] on YouTube", then "pause", "skip", "volume up", "full screen", "stop".

Podcasts and videos turn down while you talk to Walter and pause while he answers, then
carry on by themselves.

**Not available:** Amazon Music, Spotify and other DRM services can't play on Walter's
browser. **Radio stations: (planned).**

## 5. Google calendar and mail

Connect in setup or later: "Walter, connect my Google account" (or `walterctl google connect`).
A QR code appears; on your phone pick the account and tap **Allow**. You can connect several.

- **Calendar**: "What's on today?", "When is the next game?", "Add dentist Tuesday at 3 to my
  calendar" (he'll check with you before creating anything).
- **Mail**: "Any important email this morning?", "Draft a reply saying I'll be late."
  Walter can **read** and **write drafts**; he **never sends or deletes** mail. You send drafts
  yourself from Gmail.
- Calendar on the board refreshes on Walter's regular check-ins (about every 15 to 30 minutes).
- During the beta, Google may ask you to reconnect every 7 days (see Troubleshooting).

Email and calendar text is treated as untrusted: Walter will tell you what an email says, but
never follows instructions written in one.

## 6. Reminders, routines, lists and focus

- **Reminders**: "Remind me at 6:30 PM to call Grandma", "Remind me in an hour to move the car."
- **Do something later**: "In two hours, check whether it's going to freeze tonight and tell me."
- **Routines**: "Every weekday at 7:30 give me a morning briefing." He'll only set routines up
  after you say yes. "What reminders do I have?" / "Cancel reminder 3."
- **Lists**: "Add eggs to the shopping list", "Note that the filter is size 16 by 25",
  "Idea: a treehouse." You can also add from the dashboard's QR code or the app.
- **Inbox review**: things you mention in passing ("I need to call the vet") go to an inbox.
  Say "Let's do the inbox" and answer *do / defer / delegate / drop* for each.
- **Focus mode**: "Focus on taxes for 25 minutes." A ring fills as time passes; you get a
  5-minute warning. Then "I'm done", "Five more minutes", "Park it" or "What's next?"
- **Evening wrap-up** (8:30 PM): a short card with what got done and tomorrow's first three.

## 7. Long jobs (workers)

Ask for anything that takes a while (research, comparisons, writing) and Walter hands it to a
background **worker** so he stays free to talk. He tells you when it's done. "What are you
working on?" lists them; the Workers card shows them live. The number at once is set in the
app (Settings > Workers at once).

## 8. Privacy and permissions

| | Where it happens |
|---|---|
| Wake word, speech recognition, presence, face/voice recognition | **On the laptop.** No recordings or images are kept or sent. |
| Walter's voice | On the laptop (local). Cloud voice (OpenAI) only if you turn it on with your own key. |
| Your requests and his answers | Sent to **Anthropic (Claude)** to be answered, under your own Claude account or API key. |
| Weather | Your town's rough location goes to the weather service. |
| Podcasts, YouTube, web lookups, sports scores | Fetched from those services when you ask or the board refreshes. |
| Google | Only if you connect it; stays between Walter and Google. |
| Phone notifications | Only if you turn them on (via Apple/Google push). |
| Cloud Link | Only if switched on (see below). |

Defaults on a fresh install: **camera off**, **cloud speech off**, **face and voice recognition
off with nobody enrolled**, **admin rights off**.
- Camera: lets Walter notice when someone walks in. `walterctl camera on|off`.
- Recognition: each person must agree out loud first, then "Walter, learn my voice"
  (`walterctl enroll voice NAME`, read 5 sentences) or `walterctl enroll face NAME`. Only
  numeric fingerprints are stored, never photos or audio. `walterctl people` shows who;
  `walterctl forget NAME` deletes them. Recognition is a hint, never a password.
- Walter never asks you to say a password, key or code out loud. When he needs one, he shows a
  QR code that opens a private page on your phone.
- He never sends messages, buys anything or accepts terms without your spoken yes.
- `walterctl security status` checks his safety settings.

## 9. Cloud Link (remote access)

Cloud Link lets the phone app work away from home (chat, list, controls, notifications) over a
private, encrypted tunnel at an address only you have. In the beta you **request** it in setup
(step 7); the Walter team switches it on. Walter itself makes no calls to set it up.

What it exposes: **only** the phone app (`/app` and its API). Every request needs a paired
device's token; wrong tokens are rate-limited and blocked. The face, settings pages and the rest
of Walter are never reachable through it. Unpair a device and it's locked out at once.
Free during the beta; afterwards $5/month or $49/year.

Status: the per-household tunnel service is in testing **(planned for general beta use)**.
Alternative today: your own Tailscale network.

## 10. Updates and rollback

Walter checks for a new, signed version every night (about 3:40 AM). It waits if a worker is
busy. Each version is kept separately and the previous one stays installed.

    walterctl update status       # which version is running, and the previous one
    walterctl update              # check for an update now
    walterctl update rollback     # go back to the previous version (restarts Walter)

Turn automatic updates off: put `WALTER_UPDATE=off` in `/etc/walter/update.conf`.

## 11. Help

- See **TROUBLESHOOTING.md** for the 25 most common problems.
- Send feedback from the app (Settings > Feedback); preview exactly what's attached first.
- Or reply to your invite email. Never send passwords, keys or tokens.
