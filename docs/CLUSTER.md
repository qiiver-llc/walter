# Cluster: several full Walter installs as one brain

A **face** (docs/FACES.md) is a light screen/mic that borrows one brain. A **cluster** is several
**full** installs in one house (the wall, a mini PC, a spare laptop) that act as one brain:

- they share memory, the backlog, the list, reminders and routines, faces and workspaces;
- one of them is the **lead**; the others are **followers**;
- heavy jobs (workers) can run on whichever install has room;
- if the lead goes off, another takes over within about 10 seconds, so faces keep a brain.

With no other installs paired, nothing changes: the install is **solo**, sends nothing on the
network and behaves exactly as before. `walter/cluster.py`, settings in `[cluster]`.

## Add a second install

1. Install Walter on the second machine as usual (it works on its own first).
2. On the install that should lead (normally the wall): `walterctl cluster pair`.
   A 6-digit code shows on the wall for 5 minutes, with the command to run. Single use,
   burned after 5 wrong tries.
3. On the new install: `walterctl cluster join http://<lead-ip>:8765 123456`
   (add `--url http://<this-ip>:8765` if it can't work out its own address, e.g. on a tailnet).
4. Within ~15 s the new install pulls the shared files. Anything it had that was different
   (its own memory.md, list, reminders) is **kept** as a copy in `~/.walter/cluster-conflicts/`
   (`walterctl cluster conflicts`), then replaced by the lead's.

A third install joins the same way (always pair on the lead). The lead hands every member a
key for every other member through its signed heartbeat answers, so all pairs can talk.

Both installs need the home-network API open: `[app] enabled = true` and `allow_lan = true`
(the defaults). Python changes need one restart of Walter after updating.

## Commands

| Command | What |
|---|---|
| `walterctl cluster status` / `peers` | role, lead, term, last sync, every install with load, free RAM, workers |
| `walterctl cluster pair [--cancel]` | on the lead: open a join code |
| `walterctl cluster join URL CODE [--url MY_URL]` | on the new install |
| `walterctl cluster leave` | this install leaves: keys deleted here and on the others; files stay |
| `walterctl cluster remove NAME` | on the lead: throw an install out (its keys are dropped everywhere) |
| `walterctl cluster promote NAME` | hand the lead to another install now |
| `walterctl cluster sync` | sync now instead of waiting up to 15 s |
| `walterctl cluster conflicts` | what lost a last-writer-wins race, and where the copy is |
| `walterctl cluster jobs` | workers sent to other installs |
| `walterctl cluster assign NAME WS\|none` | on the lead: share a walled workspace with one install |
| `walterctl cluster discover` | installs advertising on mDNS (needs `pip install zeroconf`) |
| `walterctl task --on NAME\|best "title" "brief"` | run a worker on another install |

## Who leads (election)

- Every install sends a signed heartbeat to every other one every 3 s (`heartbeat_s`).
- A lead stays lead while it is alive (sticky: no flapping).
- After 3 missed heartbeats (`missed`, ~9 s), the live installs bump the **term** and pick the
  highest `[cluster] priority` (0-100, default 50), then the lowest node id. They all compute
  the same answer from the same view.
- A higher term always wins. An old lead that comes back (restart or network) hears the new
  term and **steps down as a follower**, even if its priority is higher. A restarted install
  listens for 3 heartbeats before claiming anything.
- `walterctl cluster promote NAME` moves the lead on purpose.

**Limits (honest):** there is no quorum. If the network splits, each side can have its own
lead for a while. When it heals, the higher term (or, in the same term, the better rank) wins.
Edits the losing lead made during the split are **not** pushed over the winner's: they go to
`~/.walter/cluster-conflicts/` and the log. Election needs roughly synced clocks (signed
requests older than 60 s are refused anyway; keep NTP on).

## What is shared

| Shared | File(s) |
|---|---|
| Memory | `home/memory.md` |
| Backlog | `home/backlog.md` |
| The list (dashboard) | `~/.walter/dashboard.json` |
| Reminders and routines | `~/.walter/reminders.json` |
| Faces (names, rooms, token **hashes**) | `~/.walter/faces.json` |
| Workspaces | `~/.walter/workspaces.json` |
| Workspace notes + backlogs | `home/workspaces/<id>/notes.md`, `backlog.md` (normal workspaces only) |

**Never shared:** secrets (`~/.walter/secrets/`), `config.toml`, app device tokens
(`devices.json`), `cluster.json` keys, browser profiles, people embeddings, journals, speech
usage, the companion app's push keys, and any **walled** workspace's folder (e.g. Vault)
unless the lead owner ran `walterctl cluster assign NAME vault` for that one install.
Walled files are filtered on the lead before sending, refused on writes, and a follower never
pushes walled files it isn't assigned. Keys are logical names mapped onto each install's own
folders; a key from a peer is never used as a path.

## How sync works (eventually consistent)

- The lead keeps a version + hash for every shared file (`~/.walter/cluster-state.json`) and
  notices its own edits on every check.
- Followers pull every 15 s (`sync_s`) and at once when the lead pokes them after a change.
- A follower's **writes are proxied to the lead**: anything changed locally (by walterctl, the
  brain, the phone page) is pushed to the lead first, the lead bumps the version, everyone
  else pulls it. Followers don't keep their own versions.
- **Last writer wins** per whole file. If the lead's copy changed since the follower last saw
  it, the follower's (later) edit still wins and the lead's copy is saved as a conflict.
- Expect up to ~15 s before an edit shows everywhere. Two people adding to the list on two
  installs in the same few seconds can lose one item to the conflict folder (it's never
  deleted). Reminder ids can clash the same way.
- Files over 1 MB and deletions are not replicated.

## Reminders, routines and who speaks

- Reminders and routines fire **on the lead only** (followers keep the file in sync and reload
  it when it changes). If the lead goes away, the new lead fires them.
- Each install still listens on its own mic and answers with its own Claude session, using the
  shared memory. It is "one brain" in what it knows and does, not one process.

## Jobs on other installs

- Every heartbeat carries capacity: cores, load, free RAM, worker slots (`[brain] max_tasks`),
  running workers, and whether local speech models are installed.
- `walterctl task --on best` (or `--on Office`): the **lead** picks the node (most free CPU,
  then free RAM; a free worker slot is required). A follower asks the lead to place it.
- When every local slot is full, `walterctl task` overflows to the best other install instead
  of failing.
- Only the lead may start a job on another install (`/api/cluster/job` refuses anyone else),
  and only as a normal worker (`walter/tasks.py`, same low-priority systemd unit, same rules).
  Walled-workspace jobs only go to installs assigned that workspace.
- The origin polls the worker's state every 5 s and gets its report like a local worker's.
  If the job can't be placed it runs locally; if the other install is lost for 3 minutes the
  job is re-run locally.
- A remote worker edits **that install's** files and repo clone. Use it for research,
  drafting and tests; code it commits stays on that install until pushed (ask first, as always).

## Speech offload

An install without speech models can borrow them:
- Signed `POST /api/cluster/transcribe` (16 kHz mono s16le PCM, up to 60 s) returns
  `{"text"}`; signed `POST /api/cluster/speak {"text"}` returns a WAV. Both reuse the face hub's
  transcribe/synth (the same path a face agent's `/api/face/audio-turn` uses).
- Simplest today: a model-less machine runs `walter-face` (docs/FACES.md) against the lead
  instead of a full install; it sends each utterance's audio to the brain.

## Faces and failover

- Face token hashes live in the shared `faces.json`, so **every install accepts every face**.
- The face agent's hello (and pairing) answer now carries
  `{"brains": [lead first, then the others], "lead": URL, "role": ...}` (nothing extra on a
  single install). A face should keep that list and, when its brain stops answering, try the
  next URL; a follower's answer tells it where the lead is. (The agent side of the switch is in
  `walter/face_agent.py`, owned by the desktop face work.)
- A browser-only face page (tablet) stays on the address it was opened with; reopen it on the
  new lead if the old one is gone for good.

## Security

- Nothing on `/api/cluster/*` answers without a signature, except `/join`, which needs the
  single-use code. Signature = HMAC-SHA256(pair key, method + path + timestamp + nonce +
  sha256(body)); timestamps outside 60 s and repeated nonces are refused; bad signatures are
  rate-limited per address.
- Each pair of installs has its own 256-bit key, made by the lead; keys are stored only in
  `~/.walter/cluster.json` (mode 600), never logged, printed or replicated. `leave`/`remove`
  deletes them on every install.
- Home network and tailnet only: refused from the internet and through the public tunnel or
  any reverse proxy (`server.gate_ok`), and off when `[cluster] enabled = false` or
  `[app] allow_lan = false`.
- Peers can only: heartbeat, read the shared snapshot, write shared files, poke a sync, ask the
  lead to place a job, run a lead-authorised worker, read a job's status, leave, and use the
  speech endpoints. No other commands, no config changes, no secrets.
- Plain http on the LAN, like faces and the app: someone sniffing your Wi-Fi can read synced
  files and the join exchange. Use a tailnet between installs if that matters.

## Settings (`[cluster]` in config.toml)

| Key | Default | What |
|---|---|---|
| `enabled` | true | the cluster API and loop (idle with no peers) |
| `name` | "" | this install's name ("" = `[faces] wall_name`) |
| `priority` | 50 | 0-100, preferred as lead at election time |
| `url` | "" | how peers reach me ("" = `http://<lan ip>:<port>`) |
| `heartbeat_s` | 3 | heartbeat interval |
| `missed` | 3 | missed heartbeats before a new election |
| `sync_s` | 15 | follower pull interval |
| `jobs` | true | run workers for other installs |
| `mdns` | true | advertise `_walter-cluster._tcp` (needs python zeroconf) |

Changes need a restart.

## Failure modes

| What happens | Result |
|---|---|
| Lead powered off | ~9 s later the best live install leads; faces move to it; routines fire there |
| Lead comes back | it follows the new lead (higher term); no flapping |
| Network split | two leads for a while; on healing the loser's edits go to the conflict folder |
| Follower offline | it catches up on its next pull; its offline edits are pushed then (LWW) |
| Same file edited on two installs within ~15 s | later edit wins; other copy in `cluster-conflicts/` |
| Install removed while off | its keys are gone everywhere; it must pair again |
| Clocks more than 60 s apart | signed requests refused ("stale"): fix NTP |
| Remote worker's install dies | job re-run here after 3 minutes |
