#!/usr/bin/env python3
"""Atomic append-if-absent for inbox.jsonl.

2026-11-21, Yanayana's burst fix applied to our side: the socket
listener, backfill and auto-join all append to the same inbox.jsonl,
and each used to "check seen, then append" without a shared lock, so
the same post could land twice when a burst arrived during a backfill
run. This module holds an flock on <inbox>.lock across the
read-check-append step, so the check and the append are atomic across
processes (and across the listener's handler threads).

The poller already skips duplicate inbox lines by event id, so a
duplicate line never caused a double wake; this removes the duplicate
lines themselves. Broken lines are ignored when reading keys (they
can never match an event key).
"""
import fcntl
import json
import os


def event_key(ev):
    return (ev.get("workspace_id"), ev.get("channel"), ev.get("ts"))


def _read_keys(inbox_path):
    keys = set()
    try:
        with open(inbox_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    keys.add(event_key(json.loads(line)))
                except (ValueError, AttributeError):
                    continue
    except FileNotFoundError:
        pass
    return keys


def append_events(inbox_path, events):
    """Append the events not already in the inbox.

    Returns (added_events, skipped_count). Creates the inbox
    directory if needed. Raises OSError on I/O failure (callers keep
    their previous behaviour of surfacing write errors).
    """
    events = list(events)
    if not events:
        return ([], 0)
    os.makedirs(os.path.dirname(inbox_path) or ".", exist_ok=True)
    lock_path = inbox_path + ".lock"
    with open(lock_path, "a") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            seen = _read_keys(inbox_path)
            fresh = []
            for ev in events:
                key = event_key(ev)
                if key not in seen:
                    seen.add(key)
                    fresh.append(ev)
            if fresh:
                with open(inbox_path, "a", encoding="utf-8") as f:
                    for ev in fresh:
                        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                        seen.add(event_key(ev))
            return (fresh, len(events) - len(fresh))
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)
