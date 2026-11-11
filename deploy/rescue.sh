#!/usr/bin/env bash
# If Walter edited his own code and now won't start, run this (as walter):
#     ~/walter/deploy/rescue.sh            # show what changed recently
#     ~/walter/deploy/rescue.sh <commit>   # roll back to <commit> and restart
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $# -eq 0 ]]; then
  git log --oneline -15
  git status --short
  echo; echo "Roll back with: $0 <commit-from-the-list-above>"
  exit 0
fi
git stash push -u -m "rescue $(date -Is)" || true
git checkout "$1" -- face walter bin
git commit -am "rescue: roll back body to $1" || true
systemctl --user reset-failed walter.service || true
systemctl --user restart walter.service
echo "Walter restored to $1 and restarted."
