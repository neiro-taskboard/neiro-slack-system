#!/usr/bin/env bash
# Watchdog for the Slack Socket Mode listener.
# - Reinstalls the systemd unit from the workspace copy if /etc lost it
#   (e.g. after a VM replacement), or if the installed copy is stale
#   (hash differs from the workspace copy — Yanayana's suggestion,
#   2026-11-12: a unit file can survive a rebuild while being outdated).
# - Starts the service if it is not running; restarts it when the unit
#   was updated so the new unit actually takes effect (daemon-reload
#   alone does not change a running service).
# - Backfills messages missed while the listener was down, from Slack
#   channel history (best effort; never fails the watchdog).
# Safe to run repeatedly; exits 0 in all expected cases.
set -u

UNIT_SRC="/home/hatch/workspace/slack-listener/slack-listener.service"
UNIT_DST="/etc/systemd/system/slack-listener.service"
SERVICE="slack-listener.service"

# Yanayana's finding (2026-11-13): after a VM restart timers.target itself
# can stay inactive, so enabled timers never fire. Our watchdog runs on a
# 5-minute scheduled job rather than a systemd timer, so it is not directly
# hit — but wake the target here anyway, so the VM's other timers
# (apt-daily etc.) do not stay asleep after a restart. Best effort: a
# failure here must not break the watchdog.
if ! systemctl is-active --quiet timers.target; then
  if systemctl start timers.target 2>/dev/null; then
    echo "timers.target started"
  else
    echo "timers.target start failed" >&2
  fi
fi

UNIT_CHANGED=0
if [ ! -f "$UNIT_DST" ] || ! cmp -s "$UNIT_SRC" "$UNIT_DST"; then
  # Reinstall with up to 3 attempts and exponential backoff (1s, 2s, 4s).
  # Yama's suggestion (2026-11-12): cap retries so a transient failure
  # cannot loop forever. A failed run is retried by the next cron run.
  ATTEMPT=1
  BACKOFF=1
  while [ "$ATTEMPT" -le 3 ]; do
    if cp "$UNIT_SRC" "$UNIT_DST" && systemctl daemon-reload; then
      UNIT_CHANGED=1
      echo "unit reinstalled (attempt $ATTEMPT)"
      break
    fi
    echo "unit reinstall attempt $ATTEMPT failed, retrying in ${BACKOFF}s" >&2
    sleep "$BACKOFF"
    BACKOFF=$((BACKOFF * 2))
    ATTEMPT=$((ATTEMPT + 1))
  done
  if [ "$UNIT_CHANGED" = "0" ]; then
    echo "unit reinstall failed after 3 attempts" >&2
  fi
fi

if ! systemctl is-active --quiet "$SERVICE"; then
  systemctl enable --now "$SERVICE" >/dev/null 2>&1 || systemctl start "$SERVICE"
  echo "service started"
elif [ "$UNIT_CHANGED" = "1" ]; then
  systemctl restart "$SERVICE"
  echo "service restarted with updated unit"
else
  echo "service running"
fi

# Catch up on messages posted while the listener was down. Best effort:
# a failure here must not break the watchdog itself.
python3 /home/hatch/workspace/slack-listener/backfill.py 2>&1 || true
