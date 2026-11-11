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

[[routines]]                             # optional (declared only; see "Not yet" below)
when = "weekdays 07:30"
prompt = "..."
```

Unknown permissions, bad ids, script paths outside the folder and so on make the add-on invalid: it
won't install (and a hand-copied one shows as "broken").

### What an add-on's code gets

`[cli]` scripts and the MCP server run from the add-on's folder with:

- `WALTER_ADDON_ID`, `WALTER_ADDON_DIR`
- `WALTER_ADDON_SETTINGS`: the settings as JSON
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
```

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
- **Not yet**: `[[routines]]` are validated and shown but not scheduled; use `walterctl every` from
  SKILL.md instructions instead for now.

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
~/.walter/config.toml [addons]         catalog = "<index.json URL>", repo = "owner/repo", branch = "main"
```
