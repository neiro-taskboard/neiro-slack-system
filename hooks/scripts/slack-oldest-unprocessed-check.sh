#!/usr/bin/env bash
# slack-oldest-unprocessed-check.sh — 最古の未処理イベントの古さを見る。
#
# 背景（2026-11-19、Yanayana さんの依頼を取り込み）：
# fastpath の取り戻しは 120 秒、確認の余裕 60 秒で、警告は 180 秒。
# 取り戻しが走ってもワーカーが done にするまで状態は processing の
# ままなので、「最古の未処理が 180 秒以上」で警告する。警告時には
# フラグファイルを作り、未処理が 0 件になったら消す、という
# Yanayana さんの最小構成で、フラグの作成・削除と Slack 警告は
# 呼び出し側（watchdog.sh）が行う。
#
# 実行場所：フックではなくウォッチドッグ側（5分間隔の watchdog.sh から
# 呼ぶ。照合の責務はフックではなくウォッチドッグ側、という 2026-11-15
# の整理と同じ）。正常時（未処理なし、または最古でもしきい値未満）は
# exit 0。最古の未処理がしきい値以上なら問題を stdout に出し exit 1。
#
# 古さの起点は first_wake_at（最初に wake された時刻）で、updated_at
# ではない。理由：fastpath の取り戻しが 120 秒ごとに updated_at を
# 進めるので、updated_at 起点だと詰まったイベントがいつまでも若く
# 見えて警告が出ない。first_wake_at が無い古い記録だけ updated_at
# を代用する。未処理＝ status が done / dead 以外（pending /
# processing / failed / waiting）。
#
# 出力は JSON 1行：{"unprocessed_count": N, "oldest_event_id": ...,
# "oldest_age_secs": ..., "threshold_secs": 180}。重複抑止の署名には
# oldest_event_id だけを使う（年齢の秒数を入れると毎回変わって連投に
# なるため）。年齢と件数は Slack 警告文に入れる。
#
# 環境変数（テスト用）：SLACK_OLDEST_UNPROCESSED_STATE_FILE
# （既定 /home/hatch/hooks/state/slack-events.jsonl）、
# SLACK_OLDEST_UNPROCESSED_THRESHOLD_SECS（既定 180）。
set -u

STATE_FILE="${SLACK_OLDEST_UNPROCESSED_STATE_FILE:-/home/hatch/hooks/state/slack-events.jsonl}"
THRESHOLD_SECS="${SLACK_OLDEST_UNPROCESSED_THRESHOLD_SECS:-180}"

python3 - "$STATE_FILE" "$THRESHOLD_SECS" <<'PYEOF'
import json
import os
import sys
from datetime import datetime, timezone

state_file, threshold = sys.argv[1], int(sys.argv[2])
now = datetime.now(timezone.utc)


def parse_iso(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


oldest_id = None
oldest_age = None
count = 0
if os.path.exists(state_file):
    with open(state_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") in ("done", "dead"):
                continue
            started = parse_iso(rec.get("first_wake_at"))
            if started is None:
                started = parse_iso(rec.get("updated_at"))
            if started is None:
                continue
            age = (now - started).total_seconds()
            count += 1
            if oldest_age is None or age > oldest_age:
                oldest_age = age
                oldest_id = rec.get("event_id")

print(json.dumps({
    "unprocessed_count": count,
    "oldest_event_id": oldest_id,
    "oldest_age_secs": int(oldest_age) if oldest_age is not None else None,
    "threshold_secs": threshold,
}, ensure_ascii=False))
sys.exit(1 if oldest_age is not None and oldest_age >= threshold else 0)
PYEOF
