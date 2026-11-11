# Walter companion app

A small installable web app (PWA) for phone and computer: chat with Walter, talk to
him (push-to-talk), the person's list, today's and tomorrow's calendar, Walter's
workers, reminders and to-do, wall controls (mute, sleep, wake, volume), "show on
wall" and a Remote for the wall browser. No app store: it's served by Walter's own server at `/app`.

## Files
| File | What |
|---|---|
| `index.html`, `app.css`, `app.js` | the app (no build step, no libraries) |
| `remote.js` | the Remote screen: live preview + touch/trackpad/keyboard for the wall browser |
| `player.js` | the Player: mini bar + sheet for podcast / YouTube on the wall |
| `modes.js` | the floating wall-mode switcher (Board, Face, Build, Conversation, Chat, Focus, Sleep), Rotate, and Projects (switch / add / edit workspaces; `walter/app_workspaces.py`) |
| `schedule.js` | Schedule: Settings > Schedule and a sheet beside Rotate/Projects. Reminders, do-later jobs, routines (edit, pause, delete with confirm, add), Loops (heartbeat 5-120 min, sleep-time learning), system timers read-only (`walter/app_schedule.py`) |
| `face-mini.js` | the small live Walter in the header (mood colour, state, talking level) |
| `pcm-worklet.js` | microphone -> 16 kHz int16 chunks for Walter's ears |
| `sw.js` | service worker: offline shell, cache named after the build |
| `manifest.webmanifest`, `icons/` | install metadata; icons drawn by `deploy/make-app-icons.py` |
| `VERSION` | the version shown in the app (Walter > Settings) |

## Navigation (thumb-first)
Four bottom tabs, each an icon plus a label, ordered by how often they're used:
**Chat** (home: talk and type), **Today** (while you were away + calendar; a yellow badge
counts new away items), **List**, **Walter** (the wall and his work). Every tab cell is
the full 64px-tall touch target, the active tab has a highlighted pill behind its icon,
and the bar sits above the phone's home indicator (safe-area padding).
- **Walter tab**: big toggles that follow the wall's real state (Wall mic on/muted,
  Awake/Asleep, Stop talking), volume, then two rows that open sub-screens:
  **Remote** (the wall browser) and **Settings** (speech, notifications, wall text size,
  workers at once, this device, version, unpair, Advanced > Restart Walter). "Show something on the wall" folds open; workers, reminders and
  Walter's to-do follow.
- **Remote is also contextual**: while the wall browser is open, a "Wall browser" pill
  appears in the header on every screen; tap it to jump straight to the remote
  (checked every 15 s while the app is in front, cheap state call, no frames).
- **Talk from anywhere**: on Today, List and Walter a round mic button floats above the
  tabs (hidden while the keyboard is up). Hold to talk, or tap and tap again, exactly
  like Chat's mic; when you let go the app switches to Chat to show the answer.
  In Chat, the send arrow appears only once you've typed something; otherwise the mic.
- **Back works like a phone app**: the phone's back gesture/button closes a sub-screen,
  then returns from any tab to Chat, then leaves the app. Sub-screens also have a back arrow.
  Tapping the tab you're already on scrolls it to the top.
- The app remembers the last tab (sub-screens reopen on their tab). A light haptic tick
  on tab taps where the phone allows it (Android), a short fade/slide between screens
  (off with the system's reduce-motion setting). Pinch and double-tap zoom stay blocked.

Server side: `walter/app_server.py` (routes, websocket, reply route), `walter/devices.py`
(pairing, tokens, rate limits, proxy detection), conductor source `"app"`.

## Pairing a device
1. `walterctl pair` (or ask Walter): a 6-digit code + QR code appear on Walter's face for
   5 minutes (5 wrong tries burn the code).
2. Scan the QR with the phone camera (the code is filled in), or open
   `http://<walter-ip>:8765/app` and type it. Enter your name and a device name, tap Pair.
3. The phone gets a long random token (kept in the browser's storage). Only its SHA-256
   is stored, in `~/.walter/devices.json` (mode 600, never in git).

`walterctl devices` lists devices, `walterctl revoke NAME` (or `all`) unpairs at once
(open apps are disconnected within seconds). On Walter's own computer
`http://localhost:8765/app` needs no pairing.

## Security
- Every `/api/app/*` call and the `/app/ws` websocket need a device token
  (`Authorization: Bearer`, or the first websocket message). Loopback (the kiosk) is exempt.
- Wrong tokens: 8 per 10 min per address, then 15 min blocked; wrong pairing codes: 5 per
  address (plus global caps).
- Reverse proxies (Tailscale serve, cloudflared) connect from 127.0.0.1. A request with
  forwarding headers, or whose Host isn't localhost/127.0.0.1, is treated as remote: it never
  counts as the kiosk and can reach only `/app`, `/api/app/*` and `/app/ws`.
  X-Forwarded-For / CF-Connecting-IP are believed only from loopback.
- Directly from the network: only home-network (private) and Tailscale (100.64/10)
  addresses, and only the app paths (plus the existing dashboard phone page).
  `[app] allow_lan = false` turns direct LAN access off (proxy/localhost only).

## Chat, talk and where replies go
App messages enter the conductor as source `app` with the device name and owner. The reply
goes back to that device only (streamed sentences, then the final text with formatting; a
coloured dot shows Walter's mood), **not** to the wall speaker, unless the message says "on
the wall" (or the speaker button next to the text box is on); then it goes to both.
Talk: hold the mic button (release sends) or tap once and tap again to send. Audio streams
over the websocket as 16 kHz PCM and is transcribed with the same ears setting as the wall
(OpenAI if `walterctl ears openai` and a key, else local whisper; caps apply), tagged
`speaker: <owner> (app)`. Replies come back as text plus the audio Walter synthesises in
his current voice (browser speech if no voice is installed), with captions.
Toasts show reminders and finished workers while the app is open.

## Photos in the chat
The paperclip next to the text box opens the phone's camera/photo picker (up to 4 per
message; paste or drag-and-drop works on a computer). Before sending, the app shows
thumbnails with an x to remove, then downscales each to 1600px on the long edge as JPEG 0.85
(small GIFs keep their animation; HEIC works where the browser can decode it, e.g. Safari)
and uploads them with progress. Sent photos appear in the bubble; tap to enlarge (fetched
with the device token). History keeps small thumbnails (the newest 30 messages with photos).
- Server: `POST /api/app/upload` (multipart, `image` parts; paired device only), `GET
  /api/app/upload/<id>` (paired device only). JPEG/PNG/WebP/GIF by magic bytes, 8 MB each,
  4 per message, 200 images / 300 MB per day. Stored in `~/.walter/app-uploads/YYYY-MM-DD/`
  with random names, mode 600, deleted after 7 days (hourly sweep). Code: `walter/app_uploads.py`.
- Brain: the chat message carries the image ids; the conductor passes the files to the brain
  as base64 image content blocks in the stream-json user message (`brain.user_content`), and
  the frame names their paths (so Walter can Read them again) and says text inside images is
  untrusted data. Replies route to the app exactly like other app turns.
- The attach button is dimmed ("Needs one restart to enable") until the server reports
  `features: ["images"]` in the websocket hello, i.e. after the first restart with this code.

## While you were away + notifications
Anything Walter couldn't say out loud (mic muted, asleep, or the camera sees an empty room):
reminders, finished workers, routine results, requests that finished while muted, lands in
`~/.walter/away.json` (mode 600, last 50) via `walter.dashboard.add_away(title, body)`. It shows
on the wall dashboard, as a card on the wall when the owner comes back ("While you were away: 3
things"), and in the app's **Today** tab (badge on the tab and the home-screen icon; opening
the tab marks them seen; Clear empties it). `walterctl away` lists, `walterctl away clear`.

**Web Push** (Walter > Settings, "Turn on notifications"): reminders always, and the away items,
reach the phone even with the app closed. Needs https (`[app] public_url`, Tailscale): on plain
http the button explains why it's off and the feed + toasts still work. iPhone: add the app to
the Home Screen first (iOS 16.4+), then turn notifications on from there. Server side:
`walter/webpush.py` (pywebpush), VAPID key in `~/.walter/secrets/vapid_private.pem`,
subscriptions per paired device in `~/.walter/push_subs.json` (dropped on revoke or when the
push service says they're gone). Routes: `GET/POST /api/app/away`, `GET/POST /api/app/push`.

## Restart Walter (Settings > Advanced)
A warning-yellow outline button. Tap -> a bottom sheet "Restart Walter? He'll be offline for
about 10 seconds." (Cancel / Restart). The server (`walter/app_restart.py`) then does what
Walter does by hand:
1. `py_compile` of every `walter/*.py` with the service's Python; if anything fails it refuses
   and the sheet shows the compiler's error (nothing changes).
2. Running background workers (the `walterctl tasks` list): the sheet lists them and needs a
   second tap, "Restart anyway" (`force: true`). Workers survive restarts; it's only a warning.
3. One restart per 30 s (429 otherwise), remembered in `~/.walter/app-restart.json` across the
   restart itself. Every attempt is logged to the journal and `~/.walter/app-restarts.log`.
4. `systemctl --user restart walter` in its own transient unit (`systemd-run --user`, after
   1.5 s, so the HTTP answer arrives first; a detached shell if systemd-run is missing).
The app then shows "Restarting..." and polls `GET /api/app/version` (its `started` field changes
with every new process); network errors during the gap are expected and stay silent. When the
new Walter answers it shows "Back online" and reconnects the websocket at once. After 45 s it
says it's taking longer (and can be closed); it keeps checking for 5 minutes.
A Walter that predates this answers 404 on `GET /api/app/restart`: the button then reads
"Needs one restart to enable" and stays disabled. Paired devices only (same auth as the wall
controls; the kiosk on localhost too). Routes: `GET /api/app/restart` -> `{available, workers,
retry_after}`; `POST /api/app/restart {force?}` -> 200 `{restarting}` | 409 `{reason: compile|workers}`
| 429 `{reason: too_soon, retry_after}`.

## Wall text size (Settings > Wall)
A slider from 50 to 150 percent (steps of 5) with the value shown live next to it, and
"Reset to 80%" (the default; 100% = the face's original size). The label follows the finger;
the new value is sent once, when the finger lifts (the `change` event, debounced 350 ms so
arrow-key nudges coalesce), and the wall changes within ~2 s. Opening Settings shows the
stored value. It's the same setting as `walterctl display text-scale 0.5..1.5`:
`walter/display_settings.py` validates it (a number 0.5-1.5, rounded to 0.01; bools, NaN and
text are refused with 400) and merges `text_scale` into `face/display.json`, which the face
polls (`face/textscale.js`); rotation and layout in that file are kept. No shell-out.
Paired devices only (same auth as the wall controls; the kiosk on localhost too).
A Walter that predates it answers 404: the slider is disabled and reads "Needs one restart
to enable". Routes: `GET /api/app/display` -> `{text_scale, default, min, max}`;
`POST /api/app/display {text_scale}` -> 200 (same shape) | 400 `{error}`.
Tests: `tests/test_app_display.py`; headless UI check
`~/.walter/browser-venv/bin/python tests/face/app_textsize_smoke.py [shot.png]`.

## Workers at once (Settings > Workers)
A stepper (minus / value / plus, 1 to 8) for `[brain] max_tasks`: how many background
workers (`walterctl task`) run side by side. Under it: "N running, M queued" (queued =
`QUEUED [` reminders from `skills/queue-job.sh`) and a hint "More workers use more CPU: this
computer has N threads, load now X" (`os.cpu_count()`, 1-minute load average). Taps are
debounced 600 ms, then sent once; a toast confirms. Applied LIVE: `max_tasks` is the one hot
key in `[brain]` (`walter/live.py` HOT_KEYS; `tasks.py` reads it at every spawn, so a lower
limit never stops running workers, it only holds new ones) and persisted to
`~/.walter/config.toml` with comments kept, through `Live.set` (the same code as
`walterctl config set brain.max_tasks N`; `live.LIMITS` refuses values outside 1-8 there
too). The route accepts only a JSON integer 1-8 (bools, floats, strings -> 400). No shell-out.
Paired devices only (the kiosk on localhost too). A Walter that predates it answers 404:
the stepper is disabled and reads "Needs one restart to enable".
Routes: `GET /api/app/workers` -> `{max_tasks, min, max, running, queued, threads, load}`;
`POST /api/app/workers {max_tasks}` -> 200 (same shape) | 400/503 `{error, ...}`.
Tests: `tests/test_app_workers.py`.

## Remote (the wall browser from the phone)
The Remote screen (Walter tab > Remote, or the header's "Wall browser" pill) drives the watchable browser on the wall (`walter-browse show`, the panel on
the face). Server: `walter/app_remote.py`; it talks to the live browser daemon
(`home/skills/browse_live.py`) over its private unix socket and its localhost screencast
port, so no new port is opened and only paired devices can use it.
- **Preview**: the wall page as a picture, about 3 fps at most, fetched only while the Remote
  screen is open and the app is in front (`GET /api/app/remote/frame`, long-poll, JPEG).
- **Tap mode**: tap = click there, drag = scroll, long press = right click.
  **Trackpad mode**: drag moves an orange pointer, tap clicks at it, two fingers scroll.
- **Keyboard**: "Type into the page" types into the box selected on the wall (shown under
  the picture); "Type password" is a masked box and only types into a real password box.
  Enter / Tab / Shift+Tab / Backspace / Esc / arrow buttons. Typed text is never logged,
  stored, shown, spoken or put in the chat history; the box empties as soon as it's sent.
- **Address bar** (address or search), Back / Forward / Reload, Open on wall / Close.
- **Taking over from Walter**: while the phone (or the wall's own mouse/keyboard) is in use,
  Walter's own browser commands wait until ~10 s after the last input; the wall panel shows
  a "Phone in control" pill. The phone's own commands never wait.
- Addresses on Walter's own computer (localhost) are refused.
Routes: `GET /api/app/remote/state[?peek=1]`, `GET .../frame?after=SEQ`, `POST .../input`
`{ev: [...]}`, `.../key` `{key}`, `.../type` `{text, kind: text|password, enter}`,
`.../nav` `{action: open|goto|back|forward|reload|close, url}`. It supersedes the one-shot
QR typing page (`walter-browse typing-page`) for paired phones; that page stays for anyone else.

## Player (podcast + YouTube)

`player.js` + `walter/app_player.py`. While a podcast (mpv on the wall speakers) or a
YouTube video (the wall browser) plays, a mini player sits above the tab bar (cover art,
title, play/pause, a thin progress line); tap it, or Walter tab > Player, for the full
sheet: big art (YouTube thumbnail for videos), show + title, a progress bar you drag to
seek (elapsed / remaining), back 15, play/pause, forward 30, Next (YouTube only), speed
chips 0.75x to 2x, a volume slider, Stop, and the Up next list (tap to play; the resume
episode has a Play button when nothing is playing).

- `GET /api/app/player`: `{kind: podcast|youtube|none, title, show, art, pos, dur, paused,
  speed, volume, ducked, can_next, upnext: [{guid, show, title, dur, pos, resume, new, art}],
  resume}`. Art is `/api/app/player/art/<16 hex>.jpg` (face/podcast-art, token required; the
  app fetches it as a blob).
- `POST /api/app/player/<action>` `{value?, guid?, kind?}`: actions toggle, pause, resume,
  back, forward, seek (0..86400 s), speed (0.5..2), volume (0..100), stop, next (YouTube),
  play (optional guid, must be in the current Up next). Each maps to a fixed argv for
  `python -m walter.podcast|walter.media` (no shell); new podcast commands `seekto` and
  `play-guid` back the seek bar and Up next.
- Polls every 1.5 s only while the app is in front and the player is visible (mini bar,
  sheet or Walter tab), 15 s otherwise, nothing in the background.
- A Walter from before the restart that added the routes answers 404: the Player row says
  "Needs one restart to enable".

## Wall mode switcher (every tab)

`modes.js` + `walter/app_mode.py`. A small "Mode" pill floats bottom-left on every tab (above
the tab bar, the mini player or the chat box; it shows the wall's current mode). Tap it for a
sheet of big buttons: Board (me small over the info board, layout large), Face (just me,
layout compact), Build (sticky build cockpit, plus Plan/Build phase and zoom: diff, tests,
board, stream, graph, plan, back), Conversation (me + captions now), Chat (the chat-bubble
view; only offered when the wall has `face/chat.js`), Focus (task + minutes, then the box
starts) and Sleep. The current mode is lit; the sheet polls every 3 s while open (the pill's
label once a minute while the app is in front). Any mode but Sleep wakes him first. A running
focus box is never ended silently: switching away asks Park it / Mark done / Cancel.

- `GET /api/app/mode`: `{mode, modes, capabilities, sleeping, layout, chat_view, focus:
  {task, end, status, left_min}|null, build: {on, phase, zoom}, zooms, phases}`. Mode
  priority: sleep > focus > build (build_mode on) > chat (chat_view chat) > conversation
  (opened from the app in the last minute) > face (layout compact) > board.
- `POST /api/app/mode` `{mode, phase?, zoom?, task?, minutes?, end_focus?}`: strict whitelist
  (focus minutes whole 1..240, task 1..80 chars, end_focus done|park|stop); 409
  `{code: "focus_running"}` when a box would be left without end_focus. Writes the same
  face/display.json keys as `walterctl display build|conversation|layout`, the same focus store
  as `walterctl focus`, and calls the conductor's sleep / wake like /api/app/control.
  Conversation/Chat send the face `{t: "convo", open}` (face/converse.js), like saying
  "conversation mode" / "close the chat".

## What needs HTTPS
Plain `http://` on the home Wi-Fi: everything except the microphone and offline/install
(browsers allow the mic and service workers only on https or localhost). For the mic and
access from outside the house use Tailscale (see
`~/.walter/home/journal/2026-11-05-app-access-options.md`), then set
`[app] public_url = "https://<name>.<tailnet>.ts.net"` in `~/.walter/config.toml`
so pairing QR codes point there.

## Updating the app (Walter does this himself)
1. Edit files in `~/walter/app/` (no restart needed: files are served live).
2. `walterctl app-bump` (or `walterctl app-bump 1.2.0`): bumps `VERSION` and tells open apps.
3. Installed copies see "Update available" (the service worker's build is VERSION plus a
   hash of the files, so even an un-bumped edit reaches them); tapping it reloads.
4. Commit: `git add app && git commit -m "App: ... [skip ci]"`.
Server-side changes (`walter/app_server.py`, `walter/devices.py`) need a Walter restart.
Icons: edit and run `.venv/bin/python deploy/make-app-icons.py`.

## Wire format
Server -> app: JSON frames (`auth`, `state`, `mute`, `cloud`, `emotion`, `jobs`, `queued`,
`typing`, `say`, `mood`, `reply`, `noaudio`, `transcribing`, `heard`, `notify`,
`app_version`, `revoked`) and binary audio: `"WA"` + 4-byte big-endian header length +
JSON header + WAV. App -> server: `auth`, `chat`, `talk_start`, binary `0x01` + PCM,
`talk_end`, `talk_cancel`, `stop`, `ping`.

## Faces (other Walter screens) are not app devices
A kitchen tablet or office laptop that should *be* Walter (his face, mic and speaker,
sharing this brain) is paired as a **face**, not as an app device: `walterctl face pair NAME`
(docs/FACES.md). Faces have their own tokens (`~/.walter/faces.json`), their own `/face/` page
and `/api/face/*` routes, home network only. The companion app is unchanged by them;
`walterctl devices` lists app devices, `walterctl faces` lists faces.
