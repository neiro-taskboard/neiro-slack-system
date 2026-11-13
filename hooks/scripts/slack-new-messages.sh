#!/usr/bin/env bash
# Hook: watch the Slack Socket Mode listener's inbox for new messages.
# State-machine version: events are tracked per event_id (workspace:channel:ts)
# with statuses pending/processing/done/failed/dead, so a crash or a retry
# never loses or double-processes a message. Failures past MAX_ATTEMPTS go
# to a dead-letter file and are reported for the owner to see.
# A flock prevents two polls from dispatching the same event at once.
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

LOCK_FILE="/home/hatch/hooks/state/slack-events.lock"
POLL_PY="/home/hatch/hooks/scripts/slack-poll.py"
mkdir -p "$(dirname "$LOCK_FILE")"

# Serialize polls (and state updates from slack-event-state.sh).
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  silent "slack poll already running"
fi

# In dry-run mode the poller computes the decision without persisting state.
if [ "${HATCH_HOOK_DRY_RUN:-0}" = "1" ]; then
  export SLACK_POLL_DRY_RUN=1
fi
OUT=$(python3 "$POLL_PY")

DECISION=$(printf '%s' "$OUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('decision','silent'))")
REASON=$(printf '%s' "$OUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('reason',''))")
PAYLOAD=$(printf '%s' "$OUT" | python3 -c "import sys,json; print(json.dumps(json.load(sys.stdin).get('payload',{}), ensure_ascii=False))")

# Periodic ambiguous reconciliation (slack-reconcile-ambiguous.py).
# The script self-gates to at most once an hour, so calling it on every
# poll is cheap (a gated no-op most of the time). Best-effort: it never
# breaks the poll (|| true, 120s cap) and is skipped in dry-run mode.
# It must run here — after the decision, before wake/silent — because
# wake/silent exit the hook immediately.
if [ "${HATCH_HOOK_DRY_RUN:-0}" != "1" ]; then
  RECONCILE_PY="/home/hatch/hooks/scripts/slack-reconcile-ambiguous.py"
  RECONCILE_OUT="$(timeout 120 python3 "$RECONCILE_PY" 2>&1 || true)"
  if [ -n "$RECONCILE_OUT" ] && ! printf '%s' "$RECONCILE_OUT" | grep -q '"gated": true'; then
    printf '%s\n' "$RECONCILE_OUT" >> "/home/hatch/hooks/state/slack-reconcile-ambiguous.log"
  fi
fi

if [ "$DECISION" = "wake" ]; then
  log "new slack messages" "$PAYLOAD"
  wake "$REASON" "$PAYLOAD"
else
  silent "$REASON"
fi
