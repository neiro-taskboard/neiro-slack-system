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
# しきい値は status で分ける（2026-11-23、Yama さんの誤報指摘を受けて）。
# processing はワーカーが掴んで作業中の証拠なので、重いバッチ
# （git commit＋push＋全テストなど）が 180 秒を超えても警告しないよう
# 600 秒にする。止まったワーカーは processing のまま年齢が伸びるので
# 10 分で警告は出る。誰も掴んでいない pending / failed / waiting は
# 180 秒のまま。警告の対象は「自分のしきい値を超えたもの」のうち最古
# のもので、超えたものが無ければ最古の未処理を情報として出す。
# 出力は JSON 1行：{"unprocessed_count": N, "oldest_event_id": ...,
# "oldest_age_secs": ..., "threshold_secs": 180,
# "processing_threshold_secs": 600, "oldest_threshold_secs": ...}。
# 重複抑止の署名には oldest_event_id だけを使う（年齢の秒数を入れると
# 毎回変わって連投になるため）。年齢と件数は Slack 警告文に入れる。
#
# 環境変数（テスト用）：SLACK_OLDEST_UNPROCESSED_STATE_FILE
# （既定 /home/hatch/hooks/state/slack-events.jsonl）、
# SLACK_OLDEST_UNPROCESSED_THRESHOLD_SECS（既定 180）、
# SLACK_OLDEST_UNPROCESSED_PROCESSING_THRESHOLD_SECS（既定 600）。
set -u

STATE_FILE="${SLACK_OLDEST_UNPROCESSED_STATE_FILE:-/home/hatch/hooks/state/slack-events.jsonl}"
THRESHOLD_SECS="${SLACK_OLDEST_UNPROCESSED_THRESHOLD_SECS:-180}"
PROCESSING_THRESHOLD_SECS="${SLACK_OLDEST_UNPROCESSED_PROCESSING_THRESHOLD_SECS:-600}"

python3 - "$STATE_FILE" "$THRESHOLD_SECS" "$PROCESSING_THRESHOLD_SECS" <<'PYEOF'
import json
import os
import sys
from datetime import datetime, timezone

state_file, threshold, processing_threshold = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
now = datetime.now(timezone.utc)


def parse_iso(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


oldest_id = None
oldest_age = None
oldest_threshold = None
warn_oldest_id = None
warn_oldest_age = None
warn_oldest_threshold = None
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
            this_threshold = processing_threshold if rec.get("status") == "processing" else threshold
            if oldest_age is None or age > oldest_age:
                oldest_age = age
                oldest_id = rec.get("event_id")
                oldest_threshold = this_threshold
            if age >= this_threshold:
                if warn_oldest_age is None or age > warn_oldest_age:
                    warn_oldest_age = age
                    warn_oldest_id = rec.get("event_id")
                    warn_oldest_threshold = this_threshold

if warn_oldest_id is not None:
    oldest_id, oldest_age, oldest_threshold = warn_oldest_id, warn_oldest_age, warn_oldest_threshold

print(json.dumps({
    "unprocessed_count": count,
    "oldest_event_id": oldest_id,
    "oldest_age_secs": int(oldest_age) if oldest_age is not None else None,
    "threshold_secs": threshold,
    "processing_threshold_secs": processing_threshold,
    "oldest_threshold_secs": oldest_threshold,
}, ensure_ascii=False))
sys.exit(1 if warn_oldest_id is not None else 0)
PYEOF
