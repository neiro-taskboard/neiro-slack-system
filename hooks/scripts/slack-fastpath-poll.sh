#!/usr/bin/env bash
# Fast-path poll for the Slack cron sweeper (2026-11-14).
#
# The slack-new-messages hook's wake can take a long time to start a
# worker (observed: hours), while scheduled agent runs start within
# ~1-2 minutes. This wrapper lets a cron agent reuse the exact same
# machinery as the hook — same flock, same slack-poll.py state machine,
# same periodic reconcile — but instead of waking, it prints the
# decision JSON so the already-running cron agent can process the
# payload itself, immediately.
#
# Concurrency: the flock serializes against hook polls and state
# updates; events the poller marks "processing" here are skipped by
# later hook polls until they go stale, and slack-send-once.sh's
# pre-send checks remain the last line of defence against duplicates.
set -euo pipefail

LOCK_FILE="/home/hatch/hooks/state/slack-events.lock"
POLL_PY="/home/hatch/hooks/scripts/slack-poll.py"
mkdir -p "$(dirname "$LOCK_FILE")"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo '{"decision":"silent","reason":"slack poll already running","payload":{}}'
  exit 0
fi

OUT=$(SLACK_POLL_FASTPATH=1 python3 "$POLL_PY")

# Periodic ambiguous reconciliation, same as the hook wrapper
# (self-gated to at most once an hour, best-effort, never fatal).
RECONCILE_PY="/home/hatch/hooks/scripts/slack-reconcile-ambiguous.py"
RECONCILE_OUT="$(timeout 120 python3 "$RECONCILE_PY" 2>&1 || true)"
if [ -n "$RECONCILE_OUT" ] && ! printf '%s' "$RECONCILE_OUT" | grep -q '"gated": true'; then
  printf '%s\n' "$RECONCILE_OUT" >> "/home/hatch/hooks/state/slack-reconcile-ambiguous.log"
fi

printf '%s\n' "$OUT"
