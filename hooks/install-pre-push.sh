#!/bin/sh
# Install the pre-push guard (hooks/pre-push) into this clone's .git/hooks.
# Run once after a fresh clone: sh hooks/install-pre-push.sh
# .git/hooks is not distributed with a clone, so the guard must be
# re-installed per clone (noted by Yanayana 2026-11-14).
# Real names for the name check go in .git/hooks/pre-push-names (one per
# line, untracked, never pushed). The hook file itself must not contain
# real names.
set -e
cd "$(dirname "$0")/.."
git_dir=$(git rev-parse --git-dir)
cp hooks/pre-push "$git_dir/hooks/pre-push"
chmod +x "$git_dir/hooks/pre-push"
echo "installed pre-push guard to $git_dir/hooks/pre-push"
if [ ! -f "$git_dir/hooks/pre-push-names" ]; then
  echo "note: $git_dir/hooks/pre-push-names does not exist yet; the real-name check stays inactive until you create it (one name per line)." >&2
fi
