#!/usr/bin/env python3
"""Backfill missed Slack messages from channel history into the inbox.

The Socket Mode listener writes every live message to inbox.jsonl, but
anything posted while the listener is down is lost: the poller only
reads the inbox. This script closes that gap. It runs after the
watchdog (best effort, never fails the watchdog) and:

- For each (workspace, channel) that already has events in the inbox,
  fetches the recent channel history via slackcli.
- Appends any message newer than the newest inbox event for that
  channel, using the same event shape the listener writes, plus
  "backfilled": true.
- Applies the same filters as the listener: neiro's own posts are
  skipped, as are message_changed / message_deleted / channel_join /
  channel_leave subtypes.

Safety:
- Channels with no inbox baseline are skipped, so a first run never
  floods the inbox with ancient history.
- Only messages strictly newer than the channel's newest inbox event
  are added, so repeated runs are idempotent.
- The poller's state machine dedupes by event id, so even a duplicate
  append is harmless.
- v1 covers top-level channel messages only; thread replies posted
  while the listener was down are not backfilled yet.

Set BACKFILL_DRY_RUN=1 to print what would be appended without writing.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
SLACKCLI_STORE = "/home/hatch/.config/slackcli/workspaces.json"
SLACKCLI = "/home/hatch/workspace/bin/slackcli"

SKIP_SUBTYPES = {"message_changed", "message_deleted", "channel_join", "channel_leave"}
DRY_RUN = os.environ.get("BACKFILL_DRY_RUN") == "1"


def load_json(path):
    with open(path) as f:
        return json.load(f)


def log(msg):
    print(f"[backfill] {msg}", flush=True)


def read_inbox(path):
    events = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return events


def fetch_history(workspace_key, channel, limit=20):
    """Return the channel's recent top-level messages via slackcli."""
    cmd = [SLACKCLI, "conversations", "read", channel,
           "--workspace", workspace_key, "--limit", str(limit), "--json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (subprocess.SubprocessError, OSError) as e:
        log(f"history fetch failed for {channel}: {e}")
        return []
    out = proc.stdout
    start = out.find("{")
    if start < 0:
        log(f"history fetch failed for {channel}: no JSON in output "
            f"(rc={proc.returncode}, stderr={proc.stderr.strip()[:200]})")
        return []
    try:
        data = json.loads(out[start:])
    except json.JSONDecodeError as e:
        log(f"history fetch failed for {channel}: bad JSON: {e}")
        return []
    return data.get("messages", [])


def build_permalink(domain, channel, ts, thread_ts):
    p = f"https://{domain}/archives/{channel}/p{ts.replace('.', '')}"
    if thread_ts:
        p += f"?thread_ts={thread_ts}&cid={channel}"
    return p


def main():
    config = load_json(CONFIG_PATH)
    store = load_json(SLACKCLI_STORE)
    inbox_path = config["inbox_path"]

    # Own bot user id per workspace (to skip neiro's own posts).
    own_user = {}
    for ws in config.get("workspaces", []):
        entry = store.get("workspaces", {}).get(ws.get("slackcli_key", ""), {})
        if entry.get("user_id"):
            own_user[ws["workspace_id"]] = entry["user_id"]

    inbox = read_inbox(inbox_path)
    seen = {(e.get("workspace_id"), e.get("channel"), e.get("ts")) for e in inbox}

    # Baseline: newest inbox ts per (workspace, channel), plus a domain
    # for permalink building and the workspace name.
    baseline = {}
    domain = {}
    ws_name = {}
    for ws in config.get("workspaces", []):
        ws_name[ws["workspace_id"]] = ws.get("workspace_name", "")
    for e in inbox:
        key = (e.get("workspace_id"), e.get("channel"))
        ts = e.get("ts", "")
        if ts and (key not in baseline or ts > baseline[key]):
            baseline[key] = ts
        if e.get("permalink"):
            try:
                domain[e.get("workspace_id")] = urlparse(e["permalink"]).netloc
            except Exception:
                pass

    added = 0
    checked = 0
    for ws in config.get("workspaces", []):
        wid = ws["workspace_id"]
        channels = sorted({ch for (w, ch) in baseline if w == wid})
        for channel in channels:
            checked += 1
            newest = baseline[(wid, channel)]
            messages = fetch_history(ws.get("slackcli_key", ""), channel)
            new_events = []
            for m in messages:
                ts = m.get("ts", "")
                if not ts or ts <= newest:
                    continue
                if (wid, channel, ts) in seen:
                    continue
                if m.get("subtype") in SKIP_SUBTYPES:
                    continue
                if m.get("user") in own_user.values():
                    continue
                ev = {
                    "workspace_id": wid,
                    "workspace_name": ws_name.get(wid, ""),
                    "channel": channel,
                    "channel_type": "channel",
                    "user": m.get("user", ""),
                    "bot_id": m.get("bot_id", ""),
                    "text": m.get("text", ""),
                    "ts": ts,
                    "thread_ts": m.get("thread_ts", ""),
                    "permalink": build_permalink(
                        domain.get(wid, ""), channel, ts, m.get("thread_ts", "")),
                    "received_at": datetime.now(timezone.utc).isoformat(),
                    "backfilled": True,
                }
                new_events.append(ev)
            new_events.sort(key=lambda e: e["ts"])
            if not new_events:
                continue
            if DRY_RUN:
                for ev in new_events:
                    log(f"DRY-RUN would add [{ev['workspace_id']}] "
                        f"{ev['channel']} {ev['ts']} {ev['user']}: "
                        f"{ev['text'][:60]}")
            else:
                with open(inbox_path, "a", encoding="utf-8") as f:
                    for ev in new_events:
                        f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                        seen.add((ev["workspace_id"], ev["channel"], ev["ts"]))
                for ev in new_events:
                    log(f"added [{ev['workspace_id']}] {ev['channel']} "
                        f"{ev['ts']} {ev['user']}: {ev['text'][:60]}")
            added += len(new_events)

    log(f"done: checked {checked} channels, added {added} missed messages"
        + (" (dry run)" if DRY_RUN else ""))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(0)  # never fail the watchdog that calls us
