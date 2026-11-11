#!/usr/bin/env bash
# Launch Walter's multi-account Google Workspace MCP server (taylorwilsdon/google_workspace_mcp,
# PyPI "workspace-mcp") over stdio. Registered with:
#   claude mcp add --scope user google-accounts -- ~/walter/tools/google-mcp/run.sh
#
# - Gmail: read + labels + DRAFTS (the owner said yes 5 Nov 12:48); sending stays OFF. Calendar and Drive
#   full (the owner yes 5 Nov 12:49: write access to calendar and Drive). Tools beyond these scopes are not registered.
# - Multi-account: every tool takes user_google_email; each account has its own token file in
#   $CREDS_DIR/<email>.json. --single-user mode never falls back to another account's token.
# - Secrets: the OAuth client lives in ~/.walter/secrets/google-oauth-client.json (mode 600),
#   dropped by the owner via tools/google-mcp/oauth-drop.py. Nothing secret is in this file or in git.
# Install / upgrade: tools/google-mcp/install.sh
set -euo pipefail
W="$HOME/.walter"
VENV="${GOOGLE_MCP_VENV:-$W/google-mcp/venv}"
CLIENT="${GOOGLE_MCP_CLIENT_FILE:-$W/secrets/google-oauth-client.json}"
export WORKSPACE_MCP_CREDENTIALS_DIR="${WORKSPACE_MCP_CREDENTIALS_DIR:-$W/google-mcp/credentials}"
export WORKSPACE_MCP_LOG_DIR="${WORKSPACE_MCP_LOG_DIR:-$W/google-mcp/logs}"
export WORKSPACE_ATTACHMENT_DIR="${WORKSPACE_ATTACHMENT_DIR:-$W/google-mcp/attachments}"
# OAuth loopback callback (Desktop-app clients accept any localhost port). 8000 is too common.
export WORKSPACE_MCP_PORT="${WORKSPACE_MCP_PORT:-8771}"
export WORKSPACE_MCP_BASE_URI="http://localhost"
export GOOGLE_OAUTH_REDIRECT_URI="http://localhost:${WORKSPACE_MCP_PORT}/oauth2callback"
export OAUTHLIB_INSECURE_TRANSPORT=1   # loopback http only, never leaves this machine
export MCP_ENABLE_OAUTH21=false
export GOOGLE_CLIENT_SECRET_PATH="$CLIENT"
unset USER_GOOGLE_EMAIL                # several accounts: the caller names one per call
umask 077
mkdir -p "$WORKSPACE_MCP_CREDENTIALS_DIR" "$WORKSPACE_MCP_LOG_DIR" "$WORKSPACE_ATTACHMENT_DIR"
chmod 700 "$W/google-mcp" "$WORKSPACE_MCP_CREDENTIALS_DIR" 2>/dev/null || true
if [ ! -x "$VENV/bin/workspace-mcp" ]; then
  echo "google-mcp: not installed; run ~/walter/tools/google-mcp/install.sh" >&2; exit 1
fi
if [ ! -s "$CLIENT" ]; then
  echo "google-mcp: no OAuth client yet ($CLIENT). The owner drops it with tools/google-mcp/oauth-drop.py" >&2
fi
exec "$VENV/bin/workspace-mcp" --transport stdio --single-user \
  --permissions gmail:drafts calendar:full drive:full --tool-tier extended "$@"
