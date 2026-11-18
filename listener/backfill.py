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
- Thread replies (2026-11-18, Yanayana's approach): after the channel
  history, parents with reply_count are re-read thread by thread and
  their replies go through the same admit as top-level posts, so a
  reply posted while the listener was down is recovered too. A thread
  is only re-read when the inbox holds fewer of its replies than
  reply_count says it has, so in steady state (listener up) no extra
  call is made; threads neiro herself replied in are re-read once
  per run while the parent stays in the recent history, because her
  own posts are never in the inbox — harmless, just one extra call.
  Thread reads pass --oldest=<channel baseline>, so the 100-message
  page covers every reply the floor admits even in a long thread.

Set BACKFILL_DRY_RUN=1 to print what would be appended without writing.

Inbox .bak (2026-11-18, Yanayana's seen_state.py approach applied to
inbox.jsonl): the inbox is append-only and backfill only covers channels
already in it, so losing the whole inbox also loses the per-channel
baseline (newest ts) and backfill stops recovering anything. The .bak
protects that baseline; re-fetching newer messages is still backfill's
job (its floor is the newest inbox ts per channel).
- snapshot_inbox(): called on every backfill run; copies the inbox to
  inbox.jsonl.bak only when every non-empty line parses as JSON and at
  least one event exists. A broken or empty body never overwrites a
  good .bak — the same guard as seen_state.py. The bytes copied are
  the exact bytes validated, so a torn line appended mid-copy cannot
  sneak into the snapshot.
- load_inbox(): reads the body first, then the .bak when the body is
  missing or cannot be opened at all. A body that opens but has broken
  lines is used as-is (bad lines are skipped, as before).
- On a .bak recovery, main() rewrites the body from the recovered
  events (atomic tmp + os.replace; the listener opens the inbox in
  append mode per event, so the replace cannot lose a message), then
  backfill runs as usual and re-fetches anything newer than the .bak.
DRY_RUN skips both the snapshot and the restore (read-only).
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
SLACKCLI_STORE = "/home/hatch/.config/slackcli/workspaces.json"
SLACKCLI = "/home/hatch/workspace/bin/slackcli"

SKIP_SUBTYPES = {"message_changed", "message_deleted", "channel_join", "channel_leave"}
# slackcli's `conversations read --json` strips the subtype field, so a
# channel_join / channel_leave message arrives looking like an ordinary
# message. Match its rendered text as a fallback (2026-11-18: found
# while adding the auto-join seed — the same hole exists here).
SYSTEM_TEXT_RE = re.compile(
    r"^<@U[A-Z0-9]+>さんがチャンネルに参加しました$"
    r"|^<@U[A-Z0-9]+>さんがチャンネルから退出しました$"
    r"|^<@U[A-Z0-9]+> has (joined|left) the channel$"
)
DRY_RUN = os.environ.get("BACKFILL_DRY_RUN") == "1"


def load_json(path):
    with open(path) as f:
        return json.load(f)


def log(msg):
    print(f"[backfill] {msg}", flush=True)


def _read_lines(path):
    """Parse a JSONL file, returning (events, bad_line_count)."""
    events = []
    bad = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    return events, bad


def read_inbox(path):
    events = []
    if os.path.exists(path):
        events, _bad = _read_lines(path)
    return events


BAK_SUFFIX = ".bak"


def load_inbox(path):
    """Read the inbox, falling back to path + '.bak' when the body is
    missing or cannot be opened at all. Returns (events, from_bak)."""
    bak = path + BAK_SUFFIX
    if os.path.exists(path):
        try:
            return read_inbox(path), False
        except OSError as e:
            log(f"inbox unreadable ({e}); trying .bak")
    elif os.path.exists(bak):
        log("inbox missing; trying .bak")
    if os.path.exists(bak):
        try:
            return read_inbox(bak), True
        except OSError as e:
            log(f"inbox .bak unreadable too ({e}); starting empty")
    return [], False


def snapshot_inbox(path):
    """Copy the inbox to path + '.bak', but only when the exact bytes
    being copied parse fully clean (every non-empty line valid JSON,
    at least one event). A broken or empty body never overwrites a
    good .bak. Returns True when the snapshot was written."""
    bak = path + BAK_SUFFIX
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as e:
        log(f"inbox snapshot skipped: unreadable ({e})")
        return False
    events = 0
    bad = 0
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            json.loads(line)
            events += 1
        except (json.JSONDecodeError, ValueError):
            bad += 1
    if bad:
        log(f"inbox snapshot skipped: {bad} broken line(s); "
            f"keeping good .bak")
        return False
    if not events:
        log("inbox snapshot skipped: empty inbox; keeping good .bak")
        return False
    tmp = bak + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(raw)
        os.replace(tmp, bak)
    except OSError as e:
        log(f"inbox snapshot failed ({e})")
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def restore_inbox(path, events):
    """Atomically rewrite the inbox body from recovered events (after a
    .bak fallback), so later appends and reads use the body again."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def fetch_history(workspace_key, channel, limit=20, quiet=False):
    """Return the channel's recent top-level messages via slackcli."""
    cmd = [SLACKCLI, "conversations", "read", channel,
           "--workspace", workspace_key, "--limit", str(limit), "--json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (subprocess.SubprocessError, OSError) as e:
        if not quiet:
            log(f"history fetch failed for {channel}: {e}")
        return []
    out = proc.stdout
    start = out.find("{")
    if start < 0:
        if not quiet:
            log(f"history fetch failed for {channel}: no JSON in output "
                f"(rc={proc.returncode}, stderr={proc.stderr.strip()[:200]})")
        return []
    try:
        data = json.loads(out[start:])
    except json.JSONDecodeError as e:
        if not quiet:
            log(f"history fetch failed for {channel}: bad JSON: {e}")
        return []
    return data.get("messages", [])


def fetch_thread(workspace_key, channel, thread_ts, oldest=None,
                 quiet=False):
    """Return one thread's messages via slackcli (parent + replies).

    With oldest set, only messages after it are returned, so the
    100-message page covers every reply the floor admits instead of
    the thread's oldest page.
    """
    cmd = [SLACKCLI, "conversations", "read", channel,
           "--thread-ts", thread_ts,
           "--workspace", workspace_key, "--limit", "100", "--json"]
    if oldest is not None:
        cmd += ["--oldest", str(oldest)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (subprocess.SubprocessError, OSError) as e:
        if not quiet:
            log(f"thread fetch failed for {channel} {thread_ts}: {e}")
        return []
    out = proc.stdout
    start = out.find("{")
    if start < 0:
        if not quiet:
            log(f"thread fetch failed for {channel} {thread_ts}: no JSON "
                f"in output (rc={proc.returncode}, "
                f"stderr={proc.stderr.strip()[:200]})")
        return []
    try:
        data = json.loads(out[start:])
    except json.JSONDecodeError as e:
        if not quiet:
            log(f"thread fetch failed for {channel} {thread_ts}: bad JSON: {e}")
        return []
    if data.get("has_more"):
        log(f"thread {channel} {thread_ts} has more than 100 messages "
            f"after {oldest}; only the first page is backfilled")
    return data.get("messages", [])


def fetch_thread_messages(ws, channel, thread_ts, oldest=None):
    """Thread messages for one parent, both keys for DMs (same as history)."""
    if channel.startswith("D") and ws.get("slackcli_user_key"):
        by_ts = {}
        for key, ident in ((ws.get("slackcli_key", ""), "bot"),
                           (ws["slackcli_user_key"], "user")):
            for m in fetch_thread(key, channel, thread_ts,
                                  oldest=oldest, quiet=True):
                ts = m.get("ts", "")
                if ts and ts not in by_ts:
                    m = dict(m)
                    m["_identity"] = ident
                    by_ts[ts] = m
        return list(by_ts.values())
    return fetch_thread(ws.get("slackcli_key", ""), channel, thread_ts,
                        oldest=oldest)


def admit_message(m, wid, channel, newest, seen, own_ids):
    """Shared admit for top-level posts and thread replies: the floor
    (strictly newer than the channel's newest inbox event), the seen
    dedup, and the listener's filters (subtypes, join/leave text, own
    posts). Returns True when the message should be backfilled."""
    ts = m.get("ts", "")
    if not ts or ts <= newest:
        return False
    if (wid, channel, ts) in seen:
        return False
    if m.get("subtype") in SKIP_SUBTYPES:
        return False
    if SYSTEM_TEXT_RE.match(m.get("text", "")):
        return False
    if m.get("user") in own_ids:
        return False
    return True


def build_event(m, wid, channel, ws_name, domain, identity):
    return {
        "workspace_id": wid,
        "workspace_name": ws_name,
        "identity": identity,
        "channel": channel,
        "channel_type": "im" if channel.startswith("D") else "channel",
        "user": m.get("user", ""),
        "bot_id": m.get("bot_id", ""),
        "text": m.get("text", ""),
        "ts": m.get("ts", ""),
        "thread_ts": m.get("thread_ts", ""),
        "permalink": build_permalink(
            domain, channel, m.get("ts", ""), m.get("thread_ts", "")),
        "received_at": datetime.now(timezone.utc).isoformat(),
        "backfilled": True,
    }


def build_permalink(domain, channel, ts, thread_ts):
    p = f"https://{domain}/archives/{channel}/p{ts.replace('.', '')}"
    if thread_ts:
        p += f"?thread_ts={thread_ts}&cid={channel}"
    return p


def main():
    config = load_json(CONFIG_PATH)
    store = load_json(SLACKCLI_STORE)
    inbox_path = config["inbox_path"]

    # Own ids (bot user id and user-account id per workspace) — neiro's
    # own posts must never be backfilled as "missed" messages.
    own_ids = set()
    for ws in config.get("workspaces", []):
        for key in (ws.get("slackcli_key", ""), ws.get("slackcli_user_key", "")):
            entry = store.get("workspaces", {}).get(key, {})
            if entry.get("user_id"):
                own_ids.add(entry["user_id"])

    inbox, from_bak = load_inbox(inbox_path)
    if from_bak:
        log(f"inbox recovered from .bak ({len(inbox)} events)")
        if not DRY_RUN:
            try:
                restore_inbox(inbox_path, inbox)
                log("inbox body rewritten from .bak")
            except OSError as e:
                log(f"inbox restore failed ({e}); "
                    f"continuing with .bak contents")
    if not DRY_RUN:
        snapshot_inbox(inbox_path)
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

    # Replies per thread already in the inbox, for the thread-fetch
    # pruning below: (workspace, channel, parent ts) -> reply count.
    # The parent itself (ts == thread_ts) is not a reply.
    thread_counts = {}
    for e in inbox:
        tts = e.get("thread_ts", "")
        if tts and e.get("ts", "") != tts:
            key = (e.get("workspace_id"), e.get("channel"), tts)
            thread_counts[key] = thread_counts.get(key, 0) + 1

    added = 0
    checked = 0
    for ws in config.get("workspaces", []):
        wid = ws["workspace_id"]
        channels = sorted({ch for (w, ch) in baseline if w == wid})
        for channel in channels:
            checked += 1
            newest = baseline[(wid, channel)]
            # DM channels (D...): bot DMs are visible only to the bot
            # token, user DMs only to the user token. Fetch with both
            # keys and merge by ts (added 2026-11-15: bot-key-only fetch
            # missed all user DMs with channel_not_found).
            by_ts = {}
            if channel.startswith("D") and ws.get("slackcli_user_key"):
                got_any = False
                for key, ident in ((ws.get("slackcli_key", ""), "bot"),
                                   (ws["slackcli_user_key"], "user")):
                    msgs = fetch_history(key, channel, quiet=True)
                    if msgs:
                        got_any = True
                    for m in msgs:
                        ts = m.get("ts", "")
                        if ts and ts not in by_ts:
                            m = dict(m)
                            m["_identity"] = ident
                            by_ts[ts] = m
                if not got_any:
                    log(f"history fetch failed for {channel}: "
                        f"neither bot nor user key can read it")
                messages = list(by_ts.values())
            else:
                messages = fetch_history(ws.get("slackcli_key", ""), channel)
            # Thread replies (2026-11-18, Yanayana's approach): re-read
            # each parent with replies and admit its replies through
            # the same checks as top-level posts. Only fetch a thread
            # when the inbox holds fewer of its replies than
            # reply_count says it has.
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
                if thread_counts.get((wid, channel, pts), 0) >= rc:
                    continue
                parents.append(pts)
            for pts in parents:
                for m in fetch_thread_messages(ws, channel, pts,
                                               oldest=newest):
                    if m.get("ts", "") == pts:
                        continue  # the parent itself; handled above
                    messages.append(m)
            new_events = []
            for m in messages:
                if not admit_message(m, wid, channel, newest, seen, own_ids):
                    continue
                ev = build_event(
                    m, wid, channel, ws_name.get(wid, ""),
                    domain.get(wid, ""),
                    m.get("_identity", "bot")
                    if channel.startswith("D") else "bot")
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
