#!/usr/bin/env python3
"""Auto-join public Slack channels the bot was left out of.

Background (2026-11-18): a new public channel was created in a
workspace the bot already serves, the bot's user account was a
member from the start, but the bot itself was not — so the listener
saw nothing in that channel until someone invited the bot by hand.
The owner said the bot does not need permission to join, so this
script joins automatically instead of waiting for an invitation.

Rules (all must hold for a channel to be joined):
- public channel (private channels still need a real invitation;
  a bot cannot self-join those),
- not archived,
- the bot is not a member yet,
- the bot's own user account is already a member (a strong signal
  the channel is meant for this side, not an unrelated channel),
- at least MIN_MEMBERS members (excludes default empty channels
  where only the user account sits alone).

Seed on join (2026-11-18, Yama's point): the watchdog only runs
every 5 minutes, so up to ~5 minutes of posts can land in a channel
before the bot joins — and the listener cannot see a channel it has
not joined yet. The backfill cannot recover those posts either: it
only covers channels that already have an inbox baseline, and only
messages newer than that baseline. So right after joining, this
script seeds the channel's recent top-level history (last
SEED_WINDOW_SECS, default 15 minutes) into the inbox as backfilled
events, so they flow through the normal pipeline. The same seeding
also runs for channels the bot is already a member of but which have
no inbox baseline yet (e.g. joined by hand, or a join whose seed
failed last time) — it self-heals instead of leaving the gap.

Seeding is bounded: only messages inside the window, never the bot's/
user account's own posts, never channel_join/leave or edited/deleted
subtypes, and never anything already in the inbox (dedup by event
id). Thread replies in the gap are covered too (2026-11-18,
Yanayana's thread-following backfill, same approach as backfill.py):
parents with reply_count are re-read thread by thread, and a thread
is only re-read when the inbox holds fewer of its replies than
reply_count says it has. slackcli strips the subtype field from
history reads, so join/leave messages are also caught by matching
their rendered text.

Run from watchdog.sh (best effort, never fails the watchdog).
Set AUTO_JOIN_DRY_RUN=1 to print what would be joined/seeded.
Set AUTO_JOIN_SEED_TEST_CHANNEL=<id> (with DRY_RUN=1) to simulate
seeding one channel the bot is already in, without joining anything.
Set AUTO_JOIN_SEED_WINDOW_SECS=<secs> to override the seed window.
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
SLACKCLI_STORE = "/home/hatch/.config/slackcli/workspaces.json"
SLACKCLI = "/home/hatch/workspace/bin/slackcli"
MIN_MEMBERS = 2
DRY_RUN = os.environ.get("AUTO_JOIN_DRY_RUN") == "1"
SEED_WINDOW_SECS = int(os.environ.get("AUTO_JOIN_SEED_WINDOW_SECS", "900"))
SEED_LIMIT = 20
SEED_TEST_CHANNEL = os.environ.get("AUTO_JOIN_SEED_TEST_CHANNEL", "")
SKIP_SUBTYPES = {"message_changed", "message_deleted", "channel_join", "channel_leave"}
# slackcli's `conversations read --json` strips the subtype field, so a
# channel_join / channel_leave message arrives looking like an ordinary
# message. Match its rendered text as a fallback, or the seed would
# inject "Xさんがチャンネルに参加しました" noise into the inbox.
SYSTEM_TEXT_RE = re.compile(
    r"^<@U[A-Z0-9]+>さんがチャンネルに参加しました$"
    r"|^<@U[A-Z0-9]+>さんがチャンネルから退出しました$"
    r"|^<@U[A-Z0-9]+> has (joined|left) the channel$"
)


def load_json(path):
    with open(path) as f:
        return json.load(f)


def log(msg):
    print(f"[auto-join] {msg}", flush=True)


def run_slackcli(args):
    """Run slackcli, return parsed JSON dict or None on failure."""
    try:
        proc = subprocess.run(
            [SLACKCLI] + args, capture_output=True, text=True, timeout=120
        )
    except (subprocess.SubprocessError, OSError) as e:
        log(f"slackcli failed {args[:2]}: {e}")
        return None
    out = proc.stdout
    start = out.find("{")
    if start < 0:
        log(f"slackcli no JSON {args[:2]}: rc={proc.returncode}")
        return None
    try:
        return json.loads(out[start:])
    except json.JSONDecodeError as e:
        log(f"slackcli bad JSON {args[:2]}: {e}")
        return None


def is_candidate(channel, members, user_id):
    """Pure decision: should the bot join this channel?"""
    if channel.get("is_private"):
        return False
    if channel.get("is_archived"):
        return False
    if channel.get("is_member"):
        return False
    if (channel.get("num_members") or 0) < MIN_MEMBERS:
        return False
    if not user_id or user_id not in (members or []):
        return False
    return True


def load_inbox_state(inbox_path):
    """Return (seen event ids, channels with a baseline, domains,
    replies per thread already in the inbox)."""
    seen = set()
    baselines = set()
    domains = {}
    thread_counts = {}
    if not os.path.exists(inbox_path):
        return seen, baselines, domains, thread_counts
    with open(inbox_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            wid = e.get("workspace_id", "")
            ch = e.get("channel", "")
            ts = e.get("ts", "")
            if wid and ch:
                baselines.add((wid, ch))
            if wid and ch and ts:
                seen.add((wid, ch, ts))
            tts = e.get("thread_ts", "")
            if wid and ch and tts and ts != tts:
                key = (wid, ch, tts)
                thread_counts[key] = thread_counts.get(key, 0) + 1
            if e.get("permalink"):
                try:
                    domains[wid] = urlparse(e["permalink"]).netloc
                except Exception:
                    pass
    return seen, baselines, domains, thread_counts


def build_permalink(domain, channel, ts, thread_ts):
    if not domain:
        return ""
    p = f"https://{domain}/archives/{channel}/p{ts.replace('.', '')}"
    if thread_ts:
        p += f"?thread_ts={thread_ts}&cid={channel}"
    return p


def seed_event_for(m, ws, channel_id, cutoff, seen, own_ids, domains,
                   allow_thread_reply=False):
    """Build the backfilled seed event for one message, or None when it
    must not be seeded (outside the window, already seen, filtered)."""
    wid = ws.get("workspace_id", "")
    ts = m.get("ts", "")
    if not ts:
        return None
    try:
        if float(ts) <= cutoff:
            return None
    except ValueError:
        return None
    if (wid, channel_id, ts) in seen:
        return None
    if m.get("subtype") in SKIP_SUBTYPES:
        return None
    if SYSTEM_TEXT_RE.match(m.get("text", "")):
        return None
    if m.get("user") in own_ids:
        return None
    thread_ts = m.get("thread_ts", "")
    if thread_ts and thread_ts != ts and not allow_thread_reply:
        return None  # thread reply; only admitted via its own thread
    return {
        "workspace_id": wid,
        "workspace_name": ws.get("workspace_name", ""),
        "identity": "bot",
        "channel": channel_id,
        "channel_type": "channel",
        "user": m.get("user", ""),
        "bot_id": m.get("bot_id", ""),
        "text": m.get("text", ""),
        "ts": ts,
        "thread_ts": thread_ts,
        "permalink": build_permalink(
            domains.get(wid, ""), channel_id, ts, thread_ts),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "backfilled": True,
    }


def seed_channel(ws, channel_id, bot_key, own_ids, seen, domains,
                  thread_counts, inbox_path, dry=False):
    """Seed a just-joined channel's recent history.

    Returns the number of events added (or that would be added).
    Never raises: a seed failure must not break the watchdog run.
    """
    wid = ws.get("workspace_id", "")
    try:
        data = run_slackcli(
            [
                "conversations", "read", channel_id,
                "--workspace", bot_key, "--limit", str(SEED_LIMIT), "--json",
            ]
        )
        if not data:
            return 0
        cutoff = time.time() - SEED_WINDOW_SECS
        messages = data.get("messages", [])
        # Thread replies (2026-11-18, Yanayana's approach, same as
        # backfill.py): re-read each parent with replies and seed its
        # replies through the same checks. Only fetch a thread when
        # the inbox holds fewer of its replies than reply_count says.
        parents = []
        for m in messages:
            try:
                rc = int(m.get("reply_count") or 0)
            except (TypeError, ValueError):
                rc = 0
            if rc <= 0:
                continue
            pts = m.get("ts", "")
            if not pts:
                continue
            if thread_counts.get((wid, channel_id, pts), 0) >= rc:
                continue
            parents.append(pts)
        thread_messages = []
        for pts in parents:
            tdata = run_slackcli(
                [
                    "conversations", "read", channel_id,
                    "--thread-ts", pts,
                    "--oldest", f"{cutoff:.6f}",
                    "--workspace", bot_key, "--limit", "100", "--json",
                ]
            )
            if not tdata:
                continue
            if tdata.get("has_more"):
                log(f"thread {channel_id} {pts} has more than 100 "
                    f"replies in the window; only the first page is "
                    f"seeded")
            for m in tdata.get("messages", []):
                if m.get("ts", "") == pts:
                    continue  # the parent itself; handled above
                thread_messages.append(m)
        new_events = []
        for m in messages:
            ev = seed_event_for(m, ws, channel_id, cutoff, seen,
                                own_ids, domains)
            if ev:
                new_events.append(ev)
        for m in thread_messages:
            ev = seed_event_for(m, ws, channel_id, cutoff, seen,
                                own_ids, domains, allow_thread_reply=True)
            if ev:
                new_events.append(ev)
        new_events.sort(key=lambda e: e["ts"])
        if not new_events:
            return 0
        if dry:
            for ev in new_events:
                log(f"DRY-RUN would seed [{ws.get('workspace_name', '')}] "
                    f"{channel_id} {ev['ts']} {ev['user']}: "
                    f"{ev['text'][:60]}")
        else:
            with open(inbox_path, "a", encoding="utf-8") as f:
                for ev in new_events:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
                    seen.add((wid, channel_id, ev["ts"]))
            for ev in new_events:
                log(f"seeded [{ws.get('workspace_name', '')}] "
                    f"{channel_id} {ev['ts']} {ev['user']}: "
                    f"{ev['text'][:60]}")
        return len(new_events)
    except Exception as e:  # noqa: BLE001 - best effort, never fail watchdog
        log(f"seed failed for {channel_id}: {e}")
        return 0


def main():
    config = load_json(CONFIG_PATH)
    store = load_json(SLACKCLI_STORE)
    inbox_path = config.get("inbox_path", "")
    seen, baselines, domains, thread_counts = load_inbox_state(inbox_path)
    # Own ids (bot user id and user-account id per workspace) — neiro's
    # own posts must never be seeded as "missed" messages.
    own_ids = set()
    for ws in config.get("workspaces", []):
        for key in (ws.get("slackcli_key", ""), ws.get("slackcli_user_key", "")):
            entry = store.get("workspaces", {}).get(key, {})
            if entry.get("user_id"):
                own_ids.add(entry["user_id"])
    joined = 0
    seeded = 0
    checked = 0
    for ws in config.get("workspaces", []):
        bot_key = ws.get("slackcli_key", "")
        user_key = ws.get("slackcli_user_key", "")
        user_entry = store.get("workspaces", {}).get(user_key, {})
        user_id = user_entry.get("user_id", "")
        data = run_slackcli(
            [
                "conversations", "list",
                "--types", "public_channel",
                "--limit", "100",
                "--workspace", bot_key,
                "--json",
                "--fields", "id,name,is_member,is_private,is_archived,num_members",
            ]
        )
        if not data:
            continue
        for ch in data.get("conversations", []):
            checked += 1
            if ch.get("is_member"):
                # Already a member but no inbox baseline yet (joined by
                # hand, or a previous seed failed): seed the recent
                # history so the join gap is closed retroactively.
                if (ws.get("workspace_id", ""), ch["id"]) not in baselines:
                    seeded += seed_channel(
                        ws, ch["id"], bot_key, own_ids, seen, domains,
                        thread_counts, inbox_path, dry=DRY_RUN)
                continue
            if ch.get("is_private") or ch.get("is_archived"):
                continue
            if (ch.get("num_members") or 0) < MIN_MEMBERS:
                continue
            mdata = run_slackcli(
                [
                    "conversations", "members", "list", ch["id"],
                    "--workspace", bot_key, "--json",
                ]
            )
            members = (mdata or {}).get("members", [])
            if not is_candidate(ch, members, user_id):
                continue
            if DRY_RUN:
                log(f"DRY-RUN would join [{ws.get('workspace_name', '')}] "
                    f"#{ch.get('name', '')} ({ch['id']})")
                joined += 1
                continue
            result = run_slackcli(
                [
                    "conversations", "join", ch["id"],
                    "--workspace", bot_key, "--json",
                ]
            )
            if result and result.get("channel"):
                log(f"joined [{ws.get('workspace_name', '')}] "
                    f"#{ch.get('name', '')} ({ch['id']})")
                joined += 1
                seeded += seed_channel(
                    ws, ch["id"], bot_key, own_ids, seen, domains,
                    thread_counts, inbox_path, dry=False)
            else:
                log(f"join failed for {ch['id']}")
    if DRY_RUN and SEED_TEST_CHANNEL:
        for ws in config.get("workspaces", []):
            if ws.get("workspace_id") == "T0CGV6V7161":
                seeded += seed_channel(
                    ws, SEED_TEST_CHANNEL, ws.get("slackcli_key", ""),
                    own_ids, seen, domains, thread_counts, inbox_path,
                    dry=True)
    log(f"done: checked {checked} channels, joined {joined}, "
        f"seeded {seeded} messages"
        + (" (dry run)" if DRY_RUN else ""))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(0)  # never fail the watchdog that calls us
