#!/usr/bin/env bash
# Reconcile Slack event state: clear stale last_error from done entries.
# Yama's suggestion (2026-11-12): periodically match DONE entries that
# still carry an error, since DONE means resolved and a leftover error
# reads as "still failing".
# Usage: slack-reconcile-done.sh [--state-file PATH] [--dry-run]
# Default state file is the production one. Use --dry-run to only report.
# Records themselves are never deleted; only last_error is cleared from
# done entries. The dead-letter file is untouched.
set -euo pipefail

STATE_FILE="/home/hatch/hooks/state/slack-events.jsonl"
LOCK_FILE="/home/hatch/hooks/state/slack-events.lock"
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --state-file) STATE_FILE="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "usage: $0 [--state-file PATH] [--dry-run]" >&2; exit 2 ;;
  esac
done

mkdir -p "$(dirname "$STATE_FILE")"

exec 9>"$LOCK_FILE"
flock 9

python3 - "$STATE_FILE" "$DRY_RUN" <<'PYEOF'
import json, os, sys
state_file, dry_run = sys.argv[1], sys.argv[2] == "1"
records = []
cleared = 0
if os.path.exists(state_file):
    with open(state_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
for rec in records:
    if rec.get("status") == "done" and rec.get("last_error"):
        rec.pop("last_error", None)
        cleared += 1
if cleared and not dry_run:
    tmp = state_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, state_file)
print(f"done entries with stale last_error cleared: {cleared} (dry_run={dry_run})")
PYEOF
