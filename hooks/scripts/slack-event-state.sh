#!/usr/bin/env bash
# Mark a Slack event done/failed/waiting in the state file.
# Usage: slack-event-state.sh [--force] <event_id> <done|failed|waiting> [error or resolution message]
# event_id format: <workspace_id>:<channel>:<ts>
# Uses the same lock as the poller so state updates never race.
#   done    : 対応済み。第3引数があれば resolution（諦めた理由など）として残す。
#             last_error は残さない（done は解決済みの意味）。
#   failed  : 送信そのものの失敗など。第3引数を last_error に残す。
#             ポーラーが attempts < 3 の間だけリトライし、使い切ったら dead。
#   waiting : DLQ再送の改訂案（2026-11-13）。送信前の履歴確認ができず
#             送れなかった（cannot-verify / pre-check失敗）とき専用。
#             attempts に数えず、ポーラーが1時間ごとに起こし直し、
#             24時間以内に読めるようになれば通常通り返信する。24時間を
#             過ぎたらポーラーが dead にする。第3引数を last_error に残す。
#   done/dead は最終状態：done/dead 済みのイベントに後から failed/waiting を
#             書き込んでも復活させない（2026-11-23、Yanayana さんの
#             滞留根本対策④を取り込み。Yama さんの監査で指摘された
#             「DONE への FAILED commit で復活する穴」と同じ形。
#             遅れて届いた失敗が解決済みを掘り返すのを防ぐ）。
#             却下した書き込みは mark_rejected.jsonl に1行残す（警告ログ。
#             2026-11-23、Yama・Yanayana さんの実装を取り込み。
#             ログが書けなくても mark 自体は壊さない fail-open）。
#   --force : 終端状態からの明示の訂正経路（2026-11-23、②の検討結果。
#             Yama さんの提案「理由付きの明示フラグ＋アラート1行」を採用）。
#             done/dead を failed/waiting に戻すときにだけ使い、理由（第3引数）
#             が必須。訂正は state に corrected_from / correction_reason /
#             corrected_at として残し、標準出力にも CORRECTION のアラート1行を出す。
#             理由なしの --force は受け付けない。
# STATE_FILE / LOCK_FILE は環境変数で上書きできる（テスト用）。
# SLACK_EVENT_MARK_REJECTED_FILE で警告ログの場所を上書きできる（テスト用）。
#   既定は STATE_FILE と同じディレクトリの mark_rejected.jsonl。
set -euo pipefail

STATE_FILE="${SLACK_EVENT_STATE_FILE:-/home/hatch/hooks/state/slack-events.jsonl}"
LOCK_FILE="${SLACK_EVENT_STATE_LOCK_FILE:-/home/hatch/hooks/state/slack-events.lock}"
mkdir -p "$(dirname "$STATE_FILE")"

FORCE=0
if [ "${1:-}" = "--force" ]; then
  FORCE=1
  shift
fi

if [ $# -lt 2 ]; then
  echo "usage: $0 [--force] <event_id> <done|failed|waiting> [error]" >&2
  exit 2
fi
EVENT_ID="$1"
STATUS="$2"
ERROR="${3:-}"
case "$STATUS" in
  done|failed|waiting) ;;
  *) echo "unknown status: $STATUS (use done|failed|waiting)" >&2; exit 2 ;;
esac
if [ "$FORCE" = 1 ] && [ -z "$ERROR" ]; then
  echo "--force correction needs a reason (3rd argument)" >&2
  exit 2
fi
NOW=$(date -u +%Y-%m-%dT%H:%M:%S.%6N+00:00)

exec 9>"$LOCK_FILE"
flock 9

python3 - "$STATE_FILE" "$EVENT_ID" "$STATUS" "$ERROR" "$NOW" "$FORCE" <<'PYEOF'
import json, os, sys
state_file = sys.argv[1]
eid, status, error, now = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
force = sys.argv[6] == "1"
rejected_file = os.environ.get("SLACK_EVENT_MARK_REJECTED_FILE") or os.path.join(
    os.path.dirname(state_file), "mark_rejected.jsonl")
state = {}
if os.path.exists(state_file):
    with open(state_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                state[rec["event_id"]] = rec
            except json.JSONDecodeError:
                continue
rec = state.get(eid, {"event_id": eid, "attempts": 0})
current = rec.get("status")
if current in ("done", "dead") and status in ("failed", "waiting") and not force:
    # done/dead は最終状態。遅れて届いた失敗で解決済み・諦め済みを復活させない。
    # 却下は警告ログに1行残す（at/key/attempted_status/current_status/reason）。
    # ログが書けなくても mark 自体は壊さない fail-open。
    try:
        with open(rejected_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "at": now, "key": eid, "attempted_status": status,
                "current_status": current,
                "reason": f"late {status} write to terminal {current} refused",
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass
    print(f"{eid} already {current}; keeping {current} (not reviving to {status})")
    sys.exit(0)
if force and current in ("done", "dead") and status in ("failed", "waiting"):
    # 明示の訂正経路：理由付きでだけ終端から戻す。訂正の痕跡を残す。
    rec["corrected_from"] = current
    rec["correction_reason"] = error
    rec["corrected_at"] = now
    print(f"CORRECTION: {eid} {current} -> {status} (reason: {error})")
rec["status"] = status
rec["updated_at"] = now
if status == "done":
    # DONE にしたら古いエラーを残さない。Yama さんの指摘（2026-11-12）：
    # DONE 済みで error_message が残ると「失敗したまま」と読める穴。
    rec.pop("last_error", None)
    # done の第3引数は resolution（諦めた理由など）として残す。
    # 例：誰かが先に答えていたので再送しなかった、親投稿が消えていた。
    if error:
        rec["resolution"] = error
elif error:
    rec["last_error"] = error
state[eid] = rec
tmp = state_file + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    for k in sorted(state):
        f.write(json.dumps(state[k], ensure_ascii=False) + "\n")
os.replace(tmp, state_file)
print(f"{eid} -> {status}")
PYEOF
