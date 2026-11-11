# Add-ons

Add-ons are small, self-contained extras the owner switches on from the phone app (Settings > Add-ons)
or with `walterctl addon`. Each one says up front what it needs (permissions), is **off until turned on**,
is **never updated by itself**, and can be switched off with everything else in one go.

- Core: `walter/addons.py` (loader, installs, state), `walter/app_addons.py` (app API),
  `walter/addon_secret_drop.py` (private phone page for secrets), `app/addons.js` (the screen).
- Catalog: the public repo [qiiver-llc/walter-addons](https://github.com/qiiver-llc/walter-addons)
  (`index.json` + `addons/<id>/`). Its `tools/pack.py` and `tools/validate.py` follow the same format.

## What an add-on is

One folder `<id>/` (id: lowercase `a-z0-9-`, 2-32 characters):

| File | Required | What it is |
|---|---|---|
| `addon.toml` | yes | the manifest (below) |
| `SKILL.md` | yes | plain instructions for Walter's brain, read **only while the add-on is ON** (first 4 KB) |
| `README.md` | for the catalog | human docs |
| scripts | no | anything listed under `[cli]` becomes `walterctl addon run <id> <cmd> ...` |

### addon.toml (format v1)

```toml
id = "example"
name = "Example add-on"
version = "0.1.0"
description = "One line: what it does."
author = "Your name"
homepage = "https://example.com"
min_walter = "0.1.0"                     # optional
permissions = ["network", "run_commands"]
# known: network, run_commands, code_leaves_machine, read_email, read_calendar,
#        home_control, hands, microphone, camera

[[settings]]                             # optional; the app renders a form
key = "default_on"
label = "Use it by default"
type = "bool"                            # bool | string | choice | number
default = false
# choice: choices = ["a", "b"]; number: optional min / max; any: help = "..."

[[secrets]]                              # optional; handed over on a private phone page
key = "token"
label = "Service token"
required = true

[cli]                                    # optional: walterctl addon run example status
status = "bin/status.sh"

[mcp]                                    # optional: an MCP server while ON
command = "bin/server.py"                # relative paths are inside the add-on folder
args = ["--dir", "${ADDON_DIR}"]

[[routines]]                             # optional: scheduled while the add-on is ON
when = "daily ${setting.time}"           # daily | weekdays | weekends | mon,wed,fri + HH:MM (24-hour)
prompt = "... ${setting.translation} ..." # ${setting.KEY} = the owner's value of that setting
```

```toml
[workers]                                # optional: cloud jobs shown as background workers (needs run_commands)
list = "bin/jobs.py list --json"         # required: prints a JSON array of jobs (below)
fetch = "bin/jobs.py fetch {id} --json"  # optional: prints {"report": "...", ...} for one finished job
```

Unknown permissions, bad ids, script paths outside the folder and so on make the add-on invalid: it
won't install (and a hand-copied one shows as "broken").

### What an add-on's code gets

`[cli]` scripts and the MCP server run from the add-on's folder with:

- `WALTER_ADDON_ID`, `WALTER_ADDON_DIR`
- `WALTER_ADDON_SETTINGS`: the settings as JSON
- `WALTER_ADDON_DATA`: a folder for the add-on's own files (`~/.walter/addons-data/<id>/`; the script
  creates it when it first needs it; kept when the add-on is removed)
- `WALTER_ADDON_SECRETS`: the folder holding its secrets, one file per key (`~/.walter/addons-secrets/<id>/<key>`,
  mode 600). Read them from there; never print or log them.

A script that isn't executable runs with `python3` (`.py`) or `bash` (`.sh`).

## Using them

```
walterctl addon list                      installed add-ons, on/off, permissions
walterctl addon catalog                   the catalog (Install / update available)
walterctl addon install ID                from the catalog: pinned to the catalog's current commit,
                                          sha256 checked against index.json
walterctl addon add URL [--yes]           any public GitHub repo (root, or /tree/BRANCH/folder);
                                          without --yes it only shows name + permissions (exit 3)
walterctl addon on ID [--yes]             exit 3 without --yes when it asks for permissions
walterctl addon off ID | off --all        --all is the kill switch (settings are kept)
walterctl addon settings ID [KEY VALUE]
walterctl addon secret ID KEY             private phone page + QR on the wall (never pass secrets in argv)
walterctl addon updates | update ID | remove ID
walterctl addon run ID CMD [ARGS...]      only while ON
walterctl addon pack DIR                  the sha256 the catalog lists for a folder
walterctl addon migrate-verse [--from DIR] [--used FILE] [--yes]
                                          one-off: the old hand-made verse routine + [dashboard] verse_line
                                          -> the verse-of-the-day add-on ON at the same time (below)
```

### The verse of the day (migration)

The verse line under the dashboard clock used to be a hand-made routine plus `[dashboard] verse_line = true`.
It is now the `verse-of-the-day` add-on; the dashboard shows the verse while that add-on is ON (or while the
old `verse_line` is true, for older installs). `walterctl addon migrate-verse` prints the plan and changes
nothing; with `--yes` it installs the add-on (from a local `~/walter-addons` checkout, sha256 checked against
its index.json, else from the catalog), sets its time to the old routine's, turns it ON, imports
`~/.walter/home/journal/verses-used.txt` (kept), removes the old routine (its text is printed), sets
`verse_line = false` and refreshes the dashboard. Undo: `walterctl addon off verse-of-the-day`,
`walterctl config set dashboard.verse_line true` and recreate the routine with `walterctl every`.

Exit code 3 means "needs a yes": Walter asks the owner out loud and repeats the command with `--yes`.

### In the app

Settings > Add-ons: **Installed** (switch per add-on; permissions are shown on a sheet before it turns
on, "Sends code or files off this computer" in red; tap a card for settings, "Set secret", update,
remove), **Catalog** (Install / Update available) and **Add from a repo** (paste a GitHub address,
see its name, permissions and a "Not made by Qiiver" warning, then Install). "Turn every add-on off"
is the kill switch.

## How Walter uses them

- **SKILL.md**: while an add-on is ON, its SKILL.md (cut at 4 KB; 16 KB for all add-ons together) goes in
  front of each brain turn. Turning one on or off counts from the next turn, no restart.
- **Walled workspaces** never get add-ons with `code_leaves_machine`: their SKILL text is held back and
  `walterctl addon run` refuses.
- **MCP servers** of the add-ons that are ON are written to `~/.walter/addons-mcp.json` and passed to
  Claude Code with `--mcp-config` when the brain process starts: they arrive after
  `walterctl new-session` (or a restart).
- **Routines**: while an add-on is ON, each `[[routines]]` entry is an ordinary `walterctl every` routine
  (kind "do") in `~/.walter/reminders.json`, tagged `"addon": "<id>", "addon_routine": <n>` and listed by
  `walterctl reminders` as `[add-on <id>]`. `Addons.sync_routines()` runs after every on / off / `off --all`
  / settings change / update / remove: it adds the missing ones, removes the ones whose add-on is off or
  whose text/time changed, keeps matching ones as they are (a paused one stays paused) and never touches
  an item without the `addon` tag (the owner's own routines). The running Walter re-reads the file within
  5 s; no restart. A `when` that doesn't parse after `${setting.KEY}` is filled in (a bad time setting) is
  skipped with a log line. Routines have no workspace (global).

## Cloud workers (`[workers]`)

An add-on that runs jobs somewhere else (the Teak add-on: Claude Code in teak.sh cloud boxes) can make them
look and behave like Walter's own background workers (`walter/cloudworkers.py`).

- `list`: the first word is a script inside the add-on, the rest are its arguments (split like a shell, no
  shell runs). It prints a JSON array, one object per job:
  `{"id", "title", "state", "started", "workspace", "step", "box"}`. `id`: letters, digits, `._:-`, up to
  80. `state`: `queued | running | done | failed | stopped` (a few aliases like `completed`, `error`,
  `cancelled` are understood; anything else is skipped). `started`: epoch seconds or ISO time. `workspace`:
  the Walter workspace id the job belongs to (project boards filter on it). `step`: one short line of what
  it's doing. Optional `finished`.
- `fetch`: `{id}` (a word of its own) is replaced by the job id. Prints JSON with a `report` string (the
  worker's final message plus anything Walter should know, such as where the results are).
- Both run from the add-on's folder with the usual environment (`WALTER_ADDON_DATA`, ...), only while the
  add-on is ON.

What the core does while such an add-on is ON:

- **Polls `list` at most once a minute**, never on the voice loop: a 20 s limit, a shared cache in
  `~/.walter/cloud-workers.json` (a file lock keeps two processes from running it at once). Bad JSON, a
  non-zero exit or a timeout is logged and the last good list kept. Everything it prints is untrusted data:
  titles and steps are cleaned and capped, odd ids and states dropped.
- **Shows the jobs** in `walterctl tasks` (`--json` gives `{"tasks", "cloud", "cloud_errors"}`),
  `walterctl tasks --live`, the Workers card on the face (`face/dashboard-workers.json`, a small cloud mark
  before the title; cloud jobs don't take a local worker slot: `running` counts local ones, `cloud_running`
  the cloud ones), a cloud-blue rounded-diamond satellite orbiting the face per running cloud job, and the app's workers list (a "cloud" badge
  with the add-on's name). Each row carries its `workspace`.
- **Reports finished jobs**: when a job turns `done` or `failed`, the running Walter runs its `fetch` once
  (up to 5 min, in a thread; tried 3 times, then reported without it) and hands the report over as an
  ordinary `[background task finished ...]` turn ("Cloud task <addon>:<id> (from the <name> add-on) ...
  Worker's report: ..."), the report inside `<untrusted>` tags, deferred like any worker's report.
  Delivered jobs are remembered in `~/.walter/cloud-workers-state.json`, so a restart never repeats one.
  `stopped` jobs aren't reported. The first time an add-on is seen, jobs that had already ended are
  recorded without a report.

## Safety rules (core)

- Installs are pinned to one commit: catalog installs fetch `index.json` and the add-on folder at the
  catalog's commit and refuse when the folder's sha256 (deterministic pack, see below) differs or the
  permissions don't match the index. Repo installs are pinned to the commit shown in the preview.
- Unpacking refuses links, devices, absolute paths, `..`, more than 400 files, files over 2 MB and add-ons
  over 5 MB (archive downloads are capped at 50 MB).
- Updates are never automatic. If an update asks for more permissions, the add-on is turned off until
  the owner says yes again.
- Secrets live in `~/.walter/addons-secrets/<id>/` (folder 700, files 600), arrive only through the private
  phone page, and are never listed, logged or shown (the app only sees "set" / "not set").

### The pack (catalog sha256)

The sha256 in `index.json` is of an uncompressed PAX tar of the add-on folder: every regular file
(skipping `.git`, `__pycache__`, `.DS_Store`, `*.pyc` and a top-level `.source.json`), named by its
`/`-separated relative path, sorted by UTF-8 bytes, mode 0755 if any execute bit else 0644,
uid/gid 0, empty user/group names, mtime 0; symlinks are an error. `walter.addons.pack()` and the
catalog's `tools/pack.py` implement the same rules.

## Files

```
~/.walter/addons/<id>/                 installed add-ons (+ .source.json: repo, path, commit, sha256, installed_at)
~/.walter/addons.json                  {"enabled": {"<id>": {"settings": {...}}}, "off": {...}}
~/.walter/addons-secrets/<id>/<key>    secrets
~/.walter/addons-mcp.json              MCP servers of the add-ons that are ON
~/.walter/addons-data/<id>/            an add-on's own files (WALTER_ADDON_DATA)
~/.walter/cloud-workers.json           cached [workers] lists (<= 60 s old)
~/.walter/cloud-workers-state.json     cloud jobs already reported (+ fetch retries)
~/.walter/reminders.json               add-on routines live here too, tagged "addon": "<id>"
~/.walter/config.toml [addons]         catalog = "<index.json URL>", repo = "owner/repo", branch = "main"
```
