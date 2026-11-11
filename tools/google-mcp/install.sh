#!/usr/bin/env bash
# Install (or upgrade) the Google Workspace MCP server into a private venv under ~/.walter.
# Not system-wide. Usage: install.sh [VERSION]   (default: the pinned one below)
set -euo pipefail
VERSION="${1:-2.3.0}"
VENV="$HOME/.walter/google-mcp/venv"
[ -x "$VENV/bin/python" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q "workspace-mcp==$VERSION"
echo "workspace-mcp $VERSION installed in $VENV"
