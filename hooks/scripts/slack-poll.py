#!/usr/bin/env python3
"""Slack inbox poller: state-machine based event dispatch.

Reads the Socket Mode listener's inbox.jsonl and a per-event state file,
then decides which events are due for processing.

State file (JSONL, one object per line):
  {"event_id": "<workspace_id>:<channel>:<ts>", "status": "pending|processing|done|failed|dead|waiting",
   "attempts": <int>, "updated_at": "<ISO8601 UTC>", "last_error": "<str>"}

Rules:
- New event (not in state)            -> mark processing, attempts=1, wake it.
- processing older than TIMEOUT_SECS  -> retry if attempts < MAX_ATTEMPTS
                                          (mark processing, attempts+1, wake it);
                                          else mark dead, append to dead-letter file.
- failed (worker reported failure)     -> same retry/dead handling as stale processing.
- done / dead                          -> skip.
- waiting (pre-check failure only)     -> DLQ再送の改訂案（2026-11-13、Yama・
  Yanayana・Neiroで合意）。送信前の履歴取得が読めなかった分は投稿の失敗
  ではなく API 待ちなので、attempts（retry_count）に数えない。1時間ごとに
  起こし直し、24時間以内に履歴が読めるようになれば通常通り返信する。
  24時間を過ぎても読めなければ諦めとして dead にし、デッドレターに記録する。
  起こし直しのペイロードには resend（最初の試行の時刻つき・接頭辞つき）を付け、ワーカーが
  再送前に答え済み確認をしてから「遅れての再送」の接頭辞つきで送る
  （2026-11-14、Yanayana の reconcile_ambiguous.py を参考に取り込み）。
  接頭辞の文言は 2026-11-14 の3者合意で「遅れての再送（最初の試行：…）」に揃えた。
- Newly dead events are reported in the payload so the owner can be notified.

Output: JSON {"decision": "wake"|"silent", "reason": str, "payload": {...}}
The calling bash script translates this into the hook's silent/wake call.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

INBOX = os.environ.get("SLACK_POLLER_INBOX",
                       "/home/hatch/workspace/slack-listener/events/inbox.jsonl")
STATE_FILE = os.environ.get("SLACK_POLLER_STATE_FILE",
                            "/home/hatch/hooks/state/slack-events.jsonl")
DEAD_FILE = os.environ.get("SLACK_POLLER_DEAD_FILE",
                           "/home/hatch/hooks/state/slack-dead-letter.jsonl")
STATS_FILE = os.environ.get("SLACK_POLLER_STATS_FILE",
                            "/home/hatch/hooks/state/slack-poll-stats.json")
# Skipped-line counts, snapshot style (Yama's proposal 2026-11-12, Yanayana's
# snapshot approach): each poll recomputes how many lines in the inbox/state
# files were skipped as blank or unparseable, and records only the counts —
# never line contents. A cumulative counter would double-count on re-reads
# because the whole inbox is read every poll. If the writer ever starts
# emitting broken lines, the count grows and the problem becomes visible
# instead of being silently dropped forever.
# Paths are overridable via env vars only so the test suite can run the real
# script against isolated temp files; defaults are the live paths, so live
# behaviour is unchanged.
MAX_ATTEMPTS = 3
TIMEOUT_SECS = 600  # 10 minutes: a "processing" event older than this is retried
# DLQ再送の改訂案（2026-11-13、Yama・Yanayana・Neiroで合意）。
WAIT_WINDOW_SECS = 24 * 3600  # pre-check失敗から再送を諦めるまでの窓（起点は最初の失敗時刻）
WAIT_WAKE_INTERVAL_SECS = 3600  # waiting の再 wake 間隔（毎時の定期照合に合わせる）


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def event_id(ev):
    return f"{ev.get('workspace_id','')}:{ev.get('channel','')}:{ev.get('ts','')}"


def format_jst(dt):
    """datetime -> '2026-11-13 23:42 JST'。

    Yanayana の reconcile_ambiguous.py の format_failed_at_jst と同じ形。
    再送文の接頭辞（resend_prefix）に入れる。
    zoneinfo が使えなければ ISO 時刻をそのまま返す。
    """
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M JST")
    except Exception:
        return dt.isoformat()


def resend_prefix(first_wait_jst):
    """再送文の先頭に付ける1行を返す（コード側で固定）。

    2026-11-14 の3者合意で「遅れての再送（最初の試行：YYYY-MM-DD HH:MM JST）」
    に揃えた。年・JSTあり＋「最初の試行」の組み合わせが3者のいいとこ取り。
    「元の失敗時刻」だとリトライ後に最後の失敗と読める余地があるため
    「最初の試行」に寄せた。Yama さんの指摘で、文言と判定はプロンプト側でなく
    コード側に置く（Yanayana の resend_prefix と同じ考え方）。
    なお旧文言で送られた再送はまだ1件もない（送信台帳で確認済み）ため、
    旧文言を認識対象に残す必要はない。
    """
    return f"遅れての再送（最初の試行：{first_wait_jst}）"


def load_state():
    state = {}
    skipped = 0
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    skipped += 1
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue
                state[rec["event_id"]] = rec
    return state, skipped


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for eid in sorted(state):
            f.write(json.dumps(state[eid], ensure_ascii=False) + "\n")
    os.replace(tmp, STATE_FILE)


def write_stats(inbox_skipped, state_skipped):
    """Write the skipped-line snapshot; only the counts, never contents.

    Written only when the counts changed, so the file's mtime marks the last
    change. A failure here must never break dispatch, so errors are warned
    about and swallowed.
    """
    stats = {"inbox_skipped_lines": inbox_skipped,
             "state_skipped_lines": state_skipped,
             "updated_at": now_iso()}
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, encoding="utf-8") as f:
                prev = json.load(f)
            if (prev.get("inbox_skipped_lines") == inbox_skipped
                    and prev.get("state_skipped_lines") == state_skipped):
                return
    except (OSError, ValueError):
        pass
    tmp = STATS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(stats, ensure_ascii=False) + "\n")
        os.replace(tmp, STATS_FILE)
    except OSError as e:
        print(f"warning: could not write stats file: {e}", file=sys.stderr)


def append_dead(ev, rec, dry_run):
    """Append a newly dead event to the dead-letter file (unless dry-run)."""
    if dry_run:
        return
    with open(DEAD_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"event": ev, "record": rec},
                           ensure_ascii=False) + "\n")


def main():
    dry_run = os.environ.get("SLACK_POLL_DRY_RUN") == "1"
    inbox = []
    inbox_skipped = 0
    if os.path.exists(INBOX):
        with open(INBOX, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    inbox_skipped += 1
                    continue
                try:
                    inbox.append(json.loads(line))
                except json.JSONDecodeError:
                    inbox_skipped += 1
                    continue

    state, state_skipped = load_state()
    if not dry_run:
        write_stats(inbox_skipped, state_skipped)
    now = datetime.now(timezone.utc)
    due = []
    newly_dead = []
    changed = False

    for ev in inbox:
        eid = event_id(ev)
        rec = state.get(eid)
        if rec is None:
            # New event: first attempt.
            rec = {"event_id": eid, "status": "processing",
                   "attempts": 1, "updated_at": now_iso(), "last_error": ""}
            state[eid] = rec
            due.append(ev)
            changed = True
            continue
        status = rec.get("status", "pending")
        if status == "done" or status == "dead":
            continue
        if status == "waiting":
            # pre-check（履歴取得）の失敗は投稿の失敗ではなく API 待ちなので
            # attempts に数えない。1時間ごとに起こし直し、24時間以内に履歴が
            # 読めるようになれば通常通り返信する。24時間を過ぎても読めなければ
            # 諦めとして dead にし、デッドレターに記録する。
            # 窓の起点は最初の waiting 時刻のまま、持ち越しで延びない。
            first_wait = parse_iso(rec.get("first_wait_at"))
            if first_wait == datetime.min.replace(tzinfo=timezone.utc):
                first_wait = parse_iso(rec.get("updated_at"))
            if first_wait == datetime.min.replace(tzinfo=timezone.utc):
                first_wait = now
            wait_until = first_wait + timedelta(seconds=WAIT_WINDOW_SECS)
            if now >= wait_until:
                rec["status"] = "dead"
                rec["updated_at"] = now_iso()
                rec["last_error"] = ((rec.get("last_error") or "pre-check unverifiable")
                                     + " (24h window expired; giving up)")
                newly_dead.append({"event": ev, "record": rec})
                changed = True
                append_dead(ev, rec, dry_run)
                continue
            next_wake = parse_iso(rec.get("next_wake_at"))
            if next_wake == datetime.min.replace(tzinfo=timezone.utc):
                # 初めて waiting を見たとき：窓の起点と次回 wake 時刻を刻む。
                rec["first_wait_at"] = first_wait.isoformat()
                rec["next_wake_at"] = (now + timedelta(seconds=WAIT_WAKE_INTERVAL_SECS)).isoformat()
                changed = True
                continue
            if now >= next_wake:
                rec["status"] = "processing"
                rec["updated_at"] = now_iso()
                rec["next_wake_at"] = (now + timedelta(seconds=WAIT_WAKE_INTERVAL_SECS)).isoformat()
                # DLQ再送の最終形（2026-11-14、Yanayana の reconcile_ambiguous.py
                # を参考に取り込み）：再 wake であることをペイロードに明示し、
                # 最初の試行の時刻を添える。ワーカーは送る前に
                # slack-answered-check.py で答え済み確認をし、再送文の先頭に
                # resend_prefix() の1行（「遅れての再送（最初の試行：…JST）」）
                # を付けて送る。
                resend_ev = dict(ev)
                resend_ev["resend"] = {
                    "first_wait_at": first_wait.isoformat(),
                    "first_wait_at_jst": format_jst(first_wait),
                    "prefix": resend_prefix(format_jst(first_wait)),
                }
                due.append(resend_ev)
                changed = True
            continue
        attempts = int(rec.get("attempts", 0))
        updated = parse_iso(rec.get("updated_at"))
        stale = (now - updated) > timedelta(seconds=TIMEOUT_SECS)
        if status == "pending" or stale:
            if attempts < MAX_ATTEMPTS:
                rec["status"] = "processing"
                rec["attempts"] = attempts + 1
                rec["updated_at"] = now_iso()
                due.append(ev)
                changed = True
            else:
                rec["status"] = "dead"
                rec["updated_at"] = now_iso()
                rec["last_error"] = rec.get("last_error", "") or "max attempts exceeded"
                newly_dead.append({"event": ev, "record": rec})
                changed = True
                append_dead(ev, rec, dry_run)
        # else: still processing within timeout -> leave alone.

    if changed and not dry_run:
        save_state(state)

    if due or newly_dead:
        payload = {"events": due, "dead_letter": newly_dead}
        reason = f"{len(due)} new slack messages"
        if newly_dead:
            reason += f", {len(newly_dead)} dead-lettered"
        print(json.dumps({"decision": "wake", "reason": reason,
                          "payload": payload}, ensure_ascii=False))
    else:
        print(json.dumps({"decision": "silent",
                          "reason": "no new slack messages",
                          "payload": {}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
