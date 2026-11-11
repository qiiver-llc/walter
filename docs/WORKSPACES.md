# Workspaces

One workspace per project or company: for example Home, Acme (a business) and Vault (walled). A new install starts with just Home. Each has its own
repos, notes, backlog, Google account, calendars, goals, share of the usage budget and a privacy level.
Code: `walter/workspaces.py`. Tests: `tests/test_workspaces*.py`.

## Files

| Path | What |
|---|---|
| `~/.walter/workspaces.json` | the workspaces (seeded on first `walterctl workspace ...`) |
| `~/.walter/workspace-state.json` | the current workspace **per face**: `{"faces": {"wall": {"workspace": "acme"}}}` |
| `~/.walter/home/workspaces/<id>/` | `notes.md` and `backlog.md` for each workspace (walled ones are mode 700) |
| `~/.walter/task-workspaces.json` | worker id -> workspace tag (written by `walterctl task`) |
| `face/dashboard-workspace.json` | the wall's current workspace, for the face |

If `~/.walter/workspaces.json` does not exist, nothing changes: one backlog, Walter's repos on the
build view, no tags, no turn header.

## Fields

`id`, `name`, `aliases` (for voice: "acme corp", "the vault"...), `repos` (paths), `notes_dir`,
`backlog` (any path; defaults to the workspace folder), `google_account`
(email or null: the owner sets it), `calendars`, `goals`, `budget_share` (0..1), `privacy`
(`normal` | `walled`), optional `journal`.

## Commands

```
walterctl workspace list | current | switch NAME | add NAME | set NAME KEY VALUE | show [NAME] [--face ID] [--json]
walterctl workspace brief [NAME]     the text put in front of a worker's instructions
walterctl workspace pick             which workspace is most behind its budget share (heartbeats)
walterctl task [--workspace NAME] "title" "instructions"
```

`set` takes lists as `a,b,c`, `+item` to append or `-item` to remove; `budget 30%`; `default` makes a
workspace the default for faces without one. Example:
`walterctl workspace set vault google_account <the owner's work address>`.

Voice (fast path, no brain round trip): "switch to Acme", "work on the vault", "go back to home",
"which workspace am I in", "list my workspaces". Only exact workspace names/aliases match; anything else
("work on the garage") goes to the brain. Every brain turn starts with `[workspace: Acme]`
(`(walled)` added for walled ones).

## What follows the workspace

- **Workers**: `walterctl task` tags the worker with the face's current workspace (or `--workspace`), and
  prepends a brief: the repos, notes and backlog paths, the Google account, and the privacy rule with the
  folders that are off limits. `walterctl tasks` shows `[workspace]` per worker.
- **Build view**: the task board shows the workspace's backlog and only its workers; the git lanes show
  only its repos. Workers from before workspaces (untagged) show everywhere except in walled workspaces.
- **`walterctl board`** and `board show T12`: the current workspace's backlog only.
- **Loop chip**: `<workspace>: <focus>` (the workspace current when the focus was set).

## Faces

`default_workspace_for_face(face_id)` reads `default_workspace` from `walter/faces.py` (`FaceStore`) when
that module exists, else the store default (`home`). Switching changes only that face's entry in
`workspace-state.json`. The CLI uses `--face ID` or `WALTER_FACE`, else `wall`; the conductor speaks for
the wall for now.

## Privacy: walled workspaces (read this honestly)

A walled workspace (e.g. Vault) never shares notes, files or worker context with the others:

- `path_allowed(path, workspace)` says no when a path belongs to another workspace and either side is
  walled (or when any walled workspace claims it). The cockpit and board use it before reading a backlog
  or repo; a walled workspace's repos never show in another workspace's lanes.
- Workers get the rule and the off-limits folders in their brief.

**Worker isolation is by instruction, not a sandbox.** A worker runs as the same user with Walter's normal
file access; it is told not to read other workspaces' folders and not to copy walled content anywhere, but
nothing physically stops it. Keep anything truly sensitive (and never any patient data) out of this
machine's reach. A real sandbox (separate user or container per walled workspace) is future work.

## Heartbeats

Initiative step 0 picks work per workspace using the budget shares: `walterctl workspace pick` names the
workspace most behind its share over the last dozen workers; start the next worker there with
`walterctl task --workspace NAME ...`.

## In the build cockpit (11 Nov)

- **Pill bar** across the top of the cockpit: one pill per workspace, the current one lit, a lock on walled
  ones, badges for running workers and ready tasks, and "All projects". A tap switches this face's
  workspace (`POST /api/workspace/switch {"id": ...}`: this computer or the home network, never via a proxy
  or the tunnel; JSON from the same origin; exact workspace ids only). Paired faces under `/face/` can't tap.
- **"projects" zoom** ("show all projects", `walterctl display build zoom projects`, the app's mode sheet):
  a card per workspace with focus, running workers, last commit, ready / done-today counts and a usage bar
  (share of the last dozen workers, with the budget share marked).
- Voice on the face: "next project", "previous project", "show all projects", "back". Names
  ("switch to Acme") stay on the server's fast path.
- Data: `cockpit.workspaces` in `face/dashboard-build.json` (`walter/cockpit.py workspaces_overview`).
  **A walled workspace that is not the current one carries only its name and numbers**: no task titles,
  focus text, commit subjects or repo names, not even in the data file. The current workspace, walled or
  not, shows its own details.
- The diff, shipped list, git lanes, task board, activity stream (workers tagged in
  `face/dashboard-workers.json`), tests (`last-test.json` records the repo it ran in) and the release /
  site lights all follow the current workspace.

## In the companion app (11 Nov)
Mode sheet > **Projects** (beside Rotate), or Settings > Projects: tap a project to put the wall on it, "+ Add
project" (name + Walled off), "Edit" (name, goals, share of time, walled). Opening up a walled project asks first.
No delete from the app. Endpoints: `GET/POST /api/app/workspaces` (walter/app_workspaces.py); walled projects
only ever send name + counts to the phone.
