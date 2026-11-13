#!/usr/bin/env bash
# Mark a Slack event done/failed/waiting in the state file.
# Usage: slack-event-state.sh <event_id> <done|failed|waiting> [error or resolution message]
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
set -euo pipefail

STATE_FILE="/home/hatch/hooks/state/slack-events.jsonl"
LOCK_FILE="/home/hatch/hooks/state/slack-events.lock"
mkdir -p "$(dirname "$STATE_FILE")"

if [ $# -lt 2 ]; then
  echo "usage: $0 <event_id> <done|failed|waiting> [error]" >&2
  exit 2
fi
EVENT_ID="$1"
STATUS="$2"
ERROR="${3:-}"
case "$STATUS" in
  done|failed|waiting) ;;
  *) echo "unknown status: $STATUS (use done|failed|waiting)" >&2; exit 2 ;;
esac
NOW=$(date -u +%Y-%m-%dT%H:%M:%S.%6N+00:00)

exec 9>"$LOCK_FILE"
flock 9

python3 - "$EVENT_ID" "$STATUS" "$ERROR" "$NOW" <<'PYEOF'
import json, os, sys
state_file = "/home/hatch/hooks/state/slack-events.jsonl"
eid, status, error, now = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
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
