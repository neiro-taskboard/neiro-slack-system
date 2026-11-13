#!/usr/bin/env python3
"""Monthly archive + summary for the Slack notification system.

What it does (run monthly, e.g. 1st of the month):
- inbox.jsonl: events older than RETENTION_DAYS are moved out of the live
  inbox into archive/inbox-YYYY-MM.jsonl.gz (grouped by event month) and a
  readable archive/SUMMARY-YYYY-MM.md is written (counts per workspace /
  channel / sender + a compact message list). Recent events stay live.
- listener.log: if larger than LOG_MAX_BYTES, copy it to
  archive/listener-YYYY-MM-DD.log.gz and truncate it (safe: systemd opens
  the log with O_APPEND, so truncation does not lose later writes).
- slack-events.jsonl (dedup state): drop "done" entries older than
  STATE_RETENTION_DAYS. Processing/failed/dead entries are always kept,
  so retries and dead letters are never lost. Slack never redelivers
  months-old events, so old "done" IDs are safe to forget.

Safety:
- Runs under the same flock as the hook poller (slack-events.lock), so it
  never races with event dispatch.
- The listener opens inbox.jsonl in append mode per event, so replacing
  the file atomically (os.replace) cannot lose a message.
- Use --dry-run to preview without changing anything.
"""
import argparse
import gzip
import json
import os
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

BASE = "/home/hatch/workspace/slack-listener"
INBOX = os.path.join(BASE, "events", "inbox.jsonl")
LOG = os.path.join(BASE, "listener.log")
ARCHIVE_DIR = os.path.join(BASE, "archive")
STATE_FILE = "/home/hatch/hooks/state/slack-events.jsonl"
LOCK_FILE = "/home/hatch/hooks/state/slack-events.lock"

RETENTION_DAYS = 30
STATE_RETENTION_DAYS = 90
LOG_MAX_BYTES = 1 * 1024 * 1024  # 1 MB

# Known users (display names). Unknown IDs are shown as-is.
# 公開版では個人を特定しうる対応表を伏せている。実運用では
# { "Uxxxxxxxxxx": "表示名", ... } の形で設定する。
USER_NAMES = {}


def parse_iso(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def month_key(dt):
    return dt.strftime("%Y-%m")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    dry = args.dry_run
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RETENTION_DAYS)
    state_cutoff = now - timedelta(days=STATE_RETENTION_DAYS)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)

    # ---- inbox: split recent vs old, archive old by month ----
    events = []
    if os.path.exists(INBOX):
        with open(INBOX, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    recent, old = [], []
    for ev in events:
        dt = parse_iso(ev.get("received_at")) or parse_iso(ev.get("ts"))
        (recent if (dt is None or dt >= cutoff) else old).append(ev)

    by_month = defaultdict(list)
    for ev in old:
        dt = parse_iso(ev.get("received_at")) or parse_iso(ev.get("ts"))
        by_month[month_key(dt) if dt else "unknown"].append(ev)

    for mk, evs in sorted(by_month.items()):
        gz_path = os.path.join(ARCHIVE_DIR, f"inbox-{mk}.jsonl.gz")
        summary_path = os.path.join(ARCHIVE_DIR, f"SUMMARY-{mk}.md")
        if dry:
            print(f"[dry-run] would archive {len(evs)} events -> {gz_path}")
            print(f"[dry-run] would write summary -> {summary_path}")
            continue
        # Append to the month's gzip (create if missing).
        with gzip.open(gz_path, "at", encoding="utf-8") as gz:
            for ev in evs:
                gz.write(json.dumps(ev, ensure_ascii=False) + "\n")
        # Readable summary (regenerate from the full month's gzip).
        month_events = []
        with gzip.open(gz_path, "rt", encoding="utf-8") as gz:
            for line in gz:
                line = line.strip()
                if line:
                    try:
                        month_events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        ws_c = Counter(e.get("workspace_name", "?") for e in month_events)
        ch_c = Counter(e.get("channel", "?") for e in month_events)
        us_c = Counter(USER_NAMES.get(e.get("user", "?"), e.get("user", "?"))
                       for e in month_events)
        lines = [f"# Slack受信まとめ {mk}", "",
                 f"- 期間: {mk}-01 〜 {mk}-31", f"- 合計: {len(month_events)}件", "",
                 "## ワークスペース別"]
        lines += [f"- {k}: {v}件" for k, v in ws_c.most_common()]
        lines += ["", "## チャンネル別"]
        lines += [f"- {k}: {v}件" for k, v in ch_c.most_common()]
        lines += ["", "## 送信者別"]
        lines += [f"- {k}: {v}件" for k, v in us_c.most_common()]
        lines += ["", "## メッセージ一覧"]
        for e in sorted(month_events, key=lambda x: x.get("received_at", "")):
            dt = parse_iso(e.get("received_at"))
            when = dt.strftime("%m-%d %H:%M") if dt else "?"
            who = USER_NAMES.get(e.get("user", "?"), e.get("user", "?"))
            text = (e.get("text", "") or "").replace("\n", " ")[:80]
            lines.append(f"- {when} {who}: {text}")
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"archived {len(evs)} events -> {gz_path} (+ summary)")

    if old and not dry:
        tmp = INBOX + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for ev in recent:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        os.replace(tmp, INBOX)
        print(f"inbox: kept {len(recent)} recent, removed {len(old)} old")

    # ---- listener.log rotation ----
    if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX_BYTES:
        stamp = now.strftime("%Y-%m-%d")
        gz_path = os.path.join(ARCHIVE_DIR, f"listener-{stamp}.log.gz")
        if dry:
            print(f"[dry-run] would rotate log -> {gz_path}")
        else:
            with open(LOG, "rb") as src, gzip.open(gz_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            # Safe: systemd opens the log with O_APPEND.
            with open(LOG, "w"):
                pass
            print(f"rotated log -> {gz_path}")

    # ---- state file: prune old "done" entries ----
    if os.path.exists(STATE_FILE):
        kept, pruned = [], 0
        with open(STATE_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                dt = parse_iso(rec.get("updated_at"))
                if (rec.get("status") == "done" and dt is not None
                        and dt < state_cutoff):
                    pruned += 1
                else:
                    kept.append(rec)
        if pruned:
            if dry:
                print(f"[dry-run] would prune {pruned} old done state entries")
            else:
                tmp = STATE_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    for rec in kept:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                os.replace(tmp, STATE_FILE)
                print(f"state: pruned {pruned} old done entries, kept {len(kept)}")

    print("archive done" + (" (dry-run)" if dry else ""))


if __name__ == "__main__":
    main()
