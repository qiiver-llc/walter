# Faces: one Walter brain, many faces

The wall Walter is the **brain**: memory, list, calendar, reminders and workers live there.
A **face** is another screen (and optionally a mic and speaker) that shares that brain: a
kitchen tablet, an office laptop, a mini PC in the garage. Each face has its own name, room,
look (theme, text size, layout) and mute, and can have a default workspace.

With no faces paired, nothing changes: the wall works exactly as before.

| Piece | Where |
|---|---|
| Registry + pairing + best-mic logic | `walter/faces.py` (`~/.walter/faces.json`, mode 600) |
| Brain side: routes, events, replies, handoff | `walter/face_server.py` |
| Face page for any screen | the normal face, `/face/?face=<id>` (`face/faceid.js`) |
| Mic + speaker agent for a face | `bin/walter-face` (`walter/face_agent.py`) |
| Settings | `[faces]` in `~/.walter/config.toml` |

## Add a face

1. On the wall: `walterctl face pair Kitchen` (or ask Walter "add a face called Kitchen").
   A 6-digit code and a QR code show on the wall for 5 minutes. The code works once.
2. **A tablet or any browser (screen only):** scan the QR. The pairing page asks for the name,
   room and kind, then opens Walter's face. The face's key is kept in a private browser cookie;
   it never shows on screen.
3. **A laptop or mini PC that should also listen and speak:** with a copy of the Walter folder
   on it, run `bin/walter-face pair http://<wall-ip>:8765 123456 --name Kitchen --room kitchen`,
   then `bin/walter-face run --screen` (`--screen` opens the face page in its browser too).
   Its key is saved in `~/.walter-face/face.json` (mode 600).

`walterctl faces` lists every face (the wall is always face `wall`), with room, kind, online,
mic/speaker/screen and its last mic score.

## Names and rooms

- `walterctl face rename Kitchen "Cook Top"`, `face room Kitchen kitchen`,
  `face kind Kitchen tablet|laptop|other`, `face workspace Office Acme` (or `none`),
  `face forget Kitchen` (it must pair again; its page and agent are cut off within seconds).
- The wall's own name is `[faces] wall_name` (default "Walter"), or `walterctl face rename wall "Living Room"`.
- **"Hey Kitchen, ..."** or **"Kitchen, ..."** talks to that face, whichever mic heard it.
  Just mentioning a room ("it's in the kitchen") doesn't count.
- **Plain "Walter"** is answered by the face with the best mic (below).

## Best mic wins

When the same sentence arrives from several faces (and the wall) within `window_s` (1.2 s)
and the words match (`match`, 0.6 fuzzy), it's ONE turn:
- a face called by name answers (even if a different mic heard it better);
- otherwise the face whose mic heard it best; on a tie, the first to hear it.

The mic score (0 to 1) is the same on every device: the ears' speech level (the louder half
of the utterance) plus the signal-to-noise ratio against the room's background
(`faces.mic_score`). The wall only waits for the window while a face with a mic is online
(`ears_timeout_s`); with none, wall turns go straight through as before.

After a reply, that face listens without its name for `followup_s` (10 s) plus the reply's
length, like the wall's follow-up window.

The brain sees where it is talking with a header, in the same style as companion-app turns:

    [heard on face Kitchen (room: kitchen) at ...; best mic, score 0.81; also heard by Walter;
     transcription may have small errors ... You are talking in that room: your reply is
     spoken on that face's speaker, NOT on the wall ...]

The reply plays on the winning face: sentences and TTS audio go to its agent, captions and
mood to its page. The wall stays quiet. "Stop" said on that face stops the reply.

## Per-face display

`face/display.json` stays the wall's. Every other face has `face/display-<id>.json`
(started from the wall's theme, text size and calendar style when it pairs). Use the same
commands with `--face`:

    walterctl display theme ocean --face Kitchen
    walterctl display text-scale 1.2 --face Kitchen
    walterctl display portrait --face Kitchen      # on a face this is the software (css) rotation

A face's page asks for `display.json` and gets its own file; cards sent to it are in
`face/card-<id>.json`.

## Handoff

    walterctl face send Kitchen text "The tea's ready"      # said there (or a card if it has no speaker)
    walterctl face send Kitchen card "Shopping" "eggs, milk" 300
    walterctl face send Garage media                         # move the podcast/radio playing on the wall
    walterctl face send Office reminder 12                   # reminder #12 goes off in the office
    walterctl face send Garage stop                          # stop talking/playing there

"Move this to the kitchen", "play it in the garage" and "send the timer to the office" map to
these. Media handoff needs `walter-face` running there with `mpv` installed; the wall stops
only after the face has taken it. Live radio starts at the live point; podcasts resume at the
same spot (a couple of seconds back).

## Settings (`[faces]` in config.toml)

| Key | Default | What |
|---|---|---|
| `enabled` | true | the face routes and the arbiter |
| `wall_name` | "Walter" | the wall's own face name |
| `window_s` | 1.2 | same utterance within this = one turn |
| `match` | 0.6 | how alike two transcripts must be |
| `followup_s` | 10 | no-name window on a face after it replied |
| `ears_timeout_s` | 60 | a face with a mic counts as online this long after its last hello |
| `mdns` | true | advertise `_walter._tcp` (needs `pip install zeroconf`; else a log line, and faces use `--brain URL`) |

Changes need a restart of Walter.

## Security

- Faces are authenticated like paired app devices: a 6-digit single-use code with a 5-minute
  life (burned after 5 wrong tries), then a 256-bit token stored only as its SHA-256. Wrong
  tokens and codes are rate-limited per address and overall. Tokens are never logged or printed.
- Home network and tailnet only: `/face/` and `/api/face/*` are refused through the public
  tunnel (any reverse proxy), from the internet, and when `[app] enabled` or `allow_lan` is false.
  Plain http on the LAN, like the app.
- A face can only change its **own** settings (volume, muted, captions, ears, and its display's
  theme, text size, calendar style, layout). It can't change brain config, run commands, read
  another face's files, or read the wall browser's remote-control file. Handoff (`/api/faces/*`)
  works only from the wall machine itself (walterctl).
- A face page is a viewer: typed text, mute and sleep from it are ignored.
- What a face hears is untrusted text, exactly like the wall mic. Faces without whisper send
  each utterance's audio to the brain to transcribe; turns without "Walter", a face name or an
  open follow-up are dropped there (logged as "ignored", like the wall).
- Voice matches from a face are hints, never proof (same rule as the wall).

## Troubleshooting

- `walterctl face status`: who is online, with what, and who answered the last turn.
- A face's page says "pair this face first": its cookie was cleared; open the pairing QR again
  (`walterctl face pair`), or `walter-face screen` on a paired laptop.
- `walter-face status` on the face checks the key and the connection.

## Several full installs (cluster)

Several full Walter installs can form one brain (docs/CLUSTER.md): they share faces.json, so
every install accepts every face, and the face hello/pairing answer carries
`{"brains": [...lead first], "lead": URL}` so a face agent can switch to the new lead when its
brain goes off. With a single install those keys are absent and nothing changes.
