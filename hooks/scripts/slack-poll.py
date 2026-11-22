#!/usr/bin/env python3
"""Slack inbox poller: state-machine based event dispatch.

Reads the Socket Mode listener's inbox.jsonl and a per-event state file,
then decides which events are due for processing.

State file (JSONL, one object per line):
  {"event_id": "<workspace_id>:<channel>:<ts>", "status": "pending|processing|done|failed|dead|waiting",
   "attempts": <int>, "updated_at": "<ISO8601 UTC>", "last_error": "<str>"}

Rules:
- New event (not in state)            -> mark processing, attempts=1, wake it.
  The first wake time is recorded (first_wake_at).
- processing older than TIMEOUT_SECS  -> within START_GRACE_SECS of the first
  wake, the worker may not have started yet: do nothing (no attempt burned,
  no re-wake, no dead). After the grace, retry if attempts < MAX_ATTEMPTS
  (mark processing, attempts+1, wake it); else mark dead, append to
  dead-letter file.
- failed (worker reported failure)     -> same retry/dead handling as stale
  processing, but with no start grace (the worker did start).
- done / dead                          -> skip.
- waiting (pre-check failure only)     -> DLQ再送の改訂案（2026-11-13、Yama・
  Yanayana・Neiroで合意）。送信前の履歴取得が読めなかった分は投稿の失敗
  ではなく API 待ちなので、attempts（retry_count）に数えない。1時間ごとに
  起こし直し、24時間以内に履歴が読めるようになれば通常通り返信する。
  24時間を過ぎても読めなければ諦めとして dead にし、デッドレターに記録する。
  起こし直しのペイロードには resend（元の失敗時刻つき・接頭辞つき）を付け、ワーカーが
  再送前に答え済み確認をしてから「遅れての再送」の接頭辞つきで送る
  （2026-11-14、Yanayana の reconcile_ambiguous.py を参考に取り込み）。
  接頭辞の文言は 2026-11-14 の3者合意で「遅れての再送（元の失敗時刻：…）」に揃えた。
  一時は「最初の試行」に寄せる合意だったが、同日のレビューで「一見そろって
  いるのに微妙に違う状態が一番分かりにくい」と指摘があり、Neiro・Yanayana
  の元の文言「元の失敗時刻」に戻すことで3者統一した（Yama さんの共有 2026-11-14）。
- Newly dead events are reported in the payload so the owner can be notified.
- Burst cap (2026-11-21): at most MAX_PER_WAKE events are dispatched per
  poll, in priority order (DMs first, then posts addressing Neiro, then
  the rest; oldest first within a tier). A candidate's state change is
  applied only when it is dispatched, so the rest are reconsidered on
  the next poll (30s later) instead of piling into one long session.
  The payload's queue_remaining says how many were left behind.

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
# ワーカーの起動猶予（2026-11-14 追加）：最初の wake からこの時間は、
# ワーカーがまだ起動していなくても試行回数を消費しない。最初の wake は
# 既に届いているので、この間は起こし直しもしない。
# 根拠（実測）：dead-letter 165件全件がイベント発生から 30〜37分で dead 化
# （3回×10分の時計だけの判定）し、そのうち 116件は遅れて起動したワーカーが
# 後に done にしていた。つまり dead の大半は誤判定で、ワーカーの起動遅延は
# 実測 40分〜2時間ある。猶予はその実測の上に余裕を取って 3時間。
# 代償：本当に起動したワーカーが応答しない（クラッシュ等）場合の検知が
# 最大で猶予＋30分（3回×10分）まで遅くなる。failed（ワーカーが起動して
# 失敗を報告した分）には猶予は適用しないので、そちらの検知は従来通り。
START_GRACE_SECS = 3 * 3600  # 3 hours
# 高速スイープ（cron slack-fastpath）専用の取り戻し（2026-11-14 追加）。
# 猶予と TIMEOUT は「wake されたワーカーの起動が遅い」ことを前提にした
# 保護だが、スイープ側はポーリングを実行しているエージェント自身が
# そのまま処理するので、起動遅延を待つ理由がない。SLACK_POLL_FASTPATH=1
# のときだけ、processing のままこの秒数を超えたイベントをスイープ側が
# 引き取って即処理する（payload に fastpath_reclaim を付けて渡す）。
# attempts と first_wake_at は触らない：リトライ／dead の会計は従来通り
# フック側が持ち、遅れて起動した元のワーカーとの競合は送信前チェック
# （slack-send-once.sh）が受ける。フック側の挙動は一切変えない。
FASTPATH_RECLAIM_SECS = int(os.environ.get(
    "SLACK_POLL_FASTPATH_RECLAIM_SECS", "120"))
# バースト対策（2026-11-21 追加）：1回の wake に詰めるイベント数の上限。
# 長文が短時間に集中すると、ワーカーは1セッションで全件を直列に処理する
# ため、後ろのイベントほど返信が遅れ、セッションが途中で終わると残りは
# 次の取り戻しまで待たされる。上限を超えた分は状態を付けずに残し、次の
# ポーリング（30秒間隔）で次のバッチとして起こす。0以下で無制限（従来動作）。
# 優先順位は DM > ねいろ宛て（本文に ねいろ / Neiro を含む）> その他、
# 同順位では古い順（dispatch_priority / ts_key を参照）。
MAX_PER_WAKE = int(os.environ.get("SLACK_POLL_MAX_PER_WAKE", "4"))
# 下位の飢餓防止（エージング、2026-11-21 追加）：DM・ねいろ宛ての
# トラフィックが続くと「その他」は後回しにされ続ける。待ち時間
# （received_at からの経過）で「その他」を段階的に昇格させる。
# 5分（10ポール）を超えたら一段昇格（ねいろ宛て相当）、15分を超えたら
# 最上位（DM相当）。昇格しても同順位では古い順を維持する。
# （Yamaさんの質問 2026-11-21 への回答で決めた条件）
# DLQ再送の改訂案（2026-11-13、Yama・Yanayana・Neiroで合意）。
WAIT_WINDOW_SECS = 24 * 3600  # pre-check失敗から再送を諦めるまでの窓（起点は最初の失敗時刻）
WAIT_WAKE_INTERVAL_SECS = 3600  # waiting の再 wake 間隔（毎時の定期照合に合わせる）


AGING_PROMOTE_SECS = 300  # 5 minutes: 「その他」-> ねいろ宛て相当
AGING_TOP_SECS = 900  # 15 minutes: 「その他」-> DM相当


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return datetime.min.replace(tzinfo=timezone.utc)


def event_id(ev):
    return f"{ev.get('workspace_id','')}:{ev.get('channel','')}:{ev.get('ts','')}"


IDS_FILE = os.environ.get("SLACK_POLLER_IDS_FILE",
                          os.path.expanduser("~/.config/neiro-slack/ids.json"))
_BOT_IDS = None


def bot_ids():
    """ねいろのボットID一覧（ids.json の bot_neiro_* キー）を返す。

    メンション（<@ID> 形式）の判定用。読めなければ空リストにフォール
    バックする（優先順位の振り分けは本文の ねいろ / Neiro でも効くので、
    ids.json が無い環境でもポーリング自体は止めない）。
    """
    global _BOT_IDS
    if _BOT_IDS is None:
        _BOT_IDS = []
        try:
            with open(IDS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                _BOT_IDS = [v for k, v in data.items()
                            if k.startswith("bot_neiro_")
                            and isinstance(v, str) and v]
        except (OSError, ValueError):
            _BOT_IDS = []
    return _BOT_IDS


def dispatch_priority(ev, now=None):
    """バースト時の dispatch 優先順位（小さいほど先に起こす）。

    DM（チャンネルIDが D 始まり）を最優先、次に ねいろ宛ての投稿
    （本文の ねいろ / Neiro、またはボットへのメンション <@ID>）、
    その他の順。宛てられた投稿が議論の長文の後ろで待たされ続けるのを
    防ぐための粗い振り分けで、同順位では ts の古い順にする。
    さらに「その他」はエージングで段階的に昇格する（5分で一段、
    15分で最上位）。received_at が読めないときは昇格しない。
    """
    if str(ev.get("channel", "")).startswith("D"):
        return 0
    text = ev.get("text") or ""
    if "ねいろ" in text or "Neiro" in text:
        return 1
    if any(f"<@{bid}>" in text for bid in bot_ids()):
        return 1
    if now is not None:
        received = parse_iso(ev.get("received_at"))
        if received.tzinfo is None:
            # タイムゾーンなしの received_at は UTC として扱う（naive の
            # まま aware と比べると TypeError でポーラー全体が落ちる）。
            received = received.replace(tzinfo=timezone.utc)
        if received > datetime.min.replace(tzinfo=timezone.utc):
            age = (now - received).total_seconds()
            if age > AGING_TOP_SECS:
                return 0
            if age > AGING_PROMOTE_SECS:
                return 1
    return 2


def ts_key(ev):
    try:
        return float(ev.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


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

    2026-11-14 の3者合意で「遅れての再送（元の失敗時刻：YYYY-MM-DD HH:MM JST）」
    に揃えた。一時は Yama さんの提案で「最初の試行」に寄せる合意だったが、
    同日のレビューで「一見そろっているのに微妙に違う状態が一番分かりにくい」
    と指摘があり、Neiro・Yanayana の元の文言「元の失敗時刻」に戻すことで
    3者統一した（Yama さんの共有 2026-11-14）。時刻は「最初に止まった時刻」
    （first_wait）なので「元の失敗時刻」が実態にも合う。
    Yama さんの指摘で、文言と判定はプロンプト側でなくコード側に置く
    （Yanayana の resend_prefix と同じ考え方）。
    なお「最初の試行」で送られた再送はまだ1件もない（送信台帳で 2026-11-14
    に確認済み）が、Yama・Yanayana と同じく新旧両方の文言を認識対象に残す。
    認識は slack-send-once.sh の重複チェックで行う（接頭辞の新旧を同一視）。
    """
    return f"遅れての再送（元の失敗時刻：{first_wait_jst}）"


def attach_resend(ev, first_wait):
    """Copy of ev annotated as a late resend (遅れての再送).

    waiting 再送と、取り戻し／stale リトライで共通の注記を作る
    （中-2：取り戻し・stale リトライでは resend 注記が付かず、
    再送文の先頭に「遅れての再送（元の失敗時刻：…）」が落ちていた）。
    first_wait は元の失敗時刻（最初の waiting 時刻）。
    """
    resend_ev = dict(ev)
    resend_ev["resend"] = {
        "first_wait_at": first_wait.isoformat(),
        "first_wait_at_jst": format_jst(first_wait),
        "prefix": resend_prefix(format_jst(first_wait)),
    }
    return resend_ev


def resend_first_wait(rec):
    """Return the record's original failure time, or None.

    first_wait_at が無い記録（waiting を経ていない）は None。
    first_wait_at は waiting を経た記録にだけ刻まれるので、
    在ること自体が「この処理は遅れての再送である」の目印になる。
    """
    first_wait = parse_iso(rec.get("first_wait_at"))
    if first_wait == datetime.min.replace(tzinfo=timezone.utc):
        return None
    return first_wait


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
    fastpath = os.environ.get("SLACK_POLL_FASTPATH") == "1"
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
    candidates = []  # (priority, ts, event, claim) — claim applies the state change on dispatch
    newly_dead = []
    changed = False

    seen_eids = set()
    for ev in inbox:
        eid = event_id(ev)
        if eid in seen_eids:
            # 同一ポーリング内の重複行（inbox に同じイベントが2行ある場合）。
            # 状態変更の遅延適用で候補が二重に入るのを防ぐ。従来は即時の
            # state 代入が自然に2件目を弾いていた（agy レビュー指摘、2026-11-21）。
            continue
        seen_eids.add(eid)
        rec = state.get(eid)
        if rec is None:
            # New event: first attempt. The state record is only written
            # if the event is actually dispatched (burst cap below), so
            # undispatched events stay "new" for the next poll.
            def claim_new(eid=eid):
                state[eid] = {"event_id": eid, "status": "processing",
                              "attempts": 1, "updated_at": now_iso(),
                              "last_error": "", "first_wake_at": now_iso()}
            candidates.append((dispatch_priority(ev, now), ts_key(ev), ev, claim_new))
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
                # DLQ再送の最終形（2026-11-14、Yanayana の reconcile_ambiguous.py
                # を参考に取り込み）：再 wake であることをペイロードに明示し、
                # 元の失敗時刻を添える。ワーカーは送る前に
                # slack-answered-check.py で答え済み確認をし、再送文の先頭に
                # resend_prefix() の1行（「遅れての再送（元の失敗時刻：…JST）」）
                # を付けて送る。状態の更新は dispatch が決まってから行う
                # （バースト上限で見送られた場合は waiting のまま次回に回す）。
                resend_ev = attach_resend(ev, first_wait)

                def claim_waiting(rec=rec, now=now):
                    rec["status"] = "processing"
                    rec["updated_at"] = now_iso()
                    rec["next_wake_at"] = (now + timedelta(seconds=WAIT_WAKE_INTERVAL_SECS)).isoformat()
                candidates.append((dispatch_priority(ev, now), ts_key(ev),
                                   resend_ev, claim_waiting))
            continue
        attempts = int(rec.get("attempts", 0))
        updated = parse_iso(rec.get("updated_at"))
        if fastpath and status == "processing":
            # 高速スイープの取り戻し：起動猶予の内側でも、processing の
            # 経過がしきい値を超えていたらスイープ側が即処理する。
            # 状態は processing のまま updated_at だけ進める（次回の
            # 取り戻しはそこから FASTPATH_RECLAIM_SECS 後）。
            age_secs = (now - updated).total_seconds()
            if age_secs > FASTPATH_RECLAIM_SECS:
                reclaim_ev = dict(ev)
                reclaim_ev["fastpath_reclaim"] = {
                    "processing_age_secs": int(age_secs),
                }
                # 中-2：waiting を経た再送が処理中に死んだ場合、取り戻しでも
                # resend 注記を付け直す。付けないと再送文の先頭から
                # 「遅れての再送（元の失敗時刻：…）」が落ちる。
                first_wait = resend_first_wait(rec)
                if first_wait is not None:
                    reclaim_ev = attach_resend(reclaim_ev, first_wait)

                def claim_reclaim(rec=rec):
                    rec["updated_at"] = now_iso()
                candidates.append((dispatch_priority(ev, now), ts_key(ev),
                                   reclaim_ev, claim_reclaim))
            continue
        stale = (now - updated) > timedelta(seconds=TIMEOUT_SECS)
        if status == "pending" or stale:
            if status == "processing":
                # 起動猶予：最初の wake から START_GRACE_SECS の間は、ワーカーが
                # まだ起動していない可能性があるので試行回数を消費しない。
                # 最初の wake は既に届いているので起こし直しもしない。
                # 猶予を過ぎたら通常のリトライ／dead 判定へ。
                # first_wake_at が無い古い記録は updated_at を代用する
                # （移行期の処理中イベントも猶予の対象にするため）。
                first_wake = parse_iso(rec.get("first_wake_at"))
                if first_wake == datetime.min.replace(tzinfo=timezone.utc):
                    first_wake = updated
                if now < first_wake + timedelta(seconds=START_GRACE_SECS):
                    continue
            if attempts < MAX_ATTEMPTS:
                # 中-2：waiting を経た再送が stale リトライに回った場合も、
                # fastpath 取り戻しと同じく resend 注記を付け直す。
                retry_ev = ev
                first_wait = resend_first_wait(rec)
                if first_wait is not None:
                    retry_ev = attach_resend(ev, first_wait)

                def claim_retry(rec=rec, attempts=attempts):
                    rec["status"] = "processing"
                    rec["attempts"] = attempts + 1
                    rec["updated_at"] = now_iso()
                candidates.append((dispatch_priority(ev, now), ts_key(ev),
                                   retry_ev, claim_retry))
            else:
                rec["status"] = "dead"
                rec["updated_at"] = now_iso()
                rec["last_error"] = rec.get("last_error", "") or "max attempts exceeded"
                newly_dead.append({"event": ev, "record": rec})
                changed = True
                append_dead(ev, rec, dry_run)
        # else: still processing within timeout -> leave alone.

    # バースト対策：候補を優先順位（DM > ねいろ宛て > その他、同順位は
    # 古い順）に並べ、上限件数だけをこの wake で dispatch する。残りの
    # 候補は状態を付けないので、次のポーリングで改めて候補になる。
    candidates.sort(key=lambda c: (c[0], c[1]))
    if MAX_PER_WAKE > 0:
        dispatched = candidates[:MAX_PER_WAKE]
    else:
        dispatched = candidates
    queue_remaining = len(candidates) - len(dispatched)
    for _prio, _ts, ev, claim in dispatched:
        claim()
        due.append(ev)
        changed = True

    if changed and not dry_run:
        save_state(state)

    if due or newly_dead:
        payload = {"events": due, "dead_letter": newly_dead,
                   "queue_remaining": queue_remaining}
        reason = f"{len(due)} new slack messages"
        if queue_remaining:
            reason += f", {queue_remaining} more queued"
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
