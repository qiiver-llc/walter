#!/usr/bin/env bash
# BROWSER for Claude Code's own `claude auth login` during first-time setup:
# opens Anthropic's sign-in page in a throwaway Chromium window on Walter's
# screen. The profile is deleted when the window closes, so no Claude session
# cookies stay behind. Walter never reads this window.
set -u
PROFILE="$(mktemp -d "${TMPDIR:-/tmp}/walter-claude-signin.XXXXXX")"
trap 'rm -rf "$PROFILE"' EXIT
CHROMIUM="$(command -v chromium || command -v chromium-browser)"
"$CHROMIUM" --ozone-platform=wayland --no-first-run --user-data-dir="$PROFILE" --new-window "$1"
