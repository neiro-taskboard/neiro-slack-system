#!/usr/bin/env bash
# slack-sync-check-gate.sh — slack-sync-check-run.sh の実行前ゲート（bash -n）。
#
# 背景（2026-11-19）：5分間隔のタイマーが、コミット書き込み中の
# slack-sync-check-run.sh（途中で切れた行 39）を読み込んで syntax error
# で失敗した。原因は「編集中のファイルにタイマーが当たる」罠で、11/15 の
# IncompleteRead と同じ構図。Yama さん・Yanayana さんとのスレッドで
# 決めた両輪のうち、読み手側の対策がこれ。
#   書き手側：Yanayana さんの原子化ルール（一時ファイルへ書いてから
#             リネームする）を採用し、編集中の壊れた状態が稼働側に
#             見えないようにする。
#   読み手側：このゲートが実行前に bash -n で構文を確認し、壊れて
#             いれば実行しない。
#
# 振る舞い：
#   - bash -n が通れば、そのまま runner を exec する。
#   - 通らなければ、初回失敗時刻を状態ファイルに記録し、静かに
#     exit 0 で終える（次の 5 分ティックで再試行する。書きかけは
#     数秒〜1分で直るため、FAILED 警告を出さない）。
#   - 壊れた状態が GRACE_SECS（600秒＝10分）以上続いたら、bash -n の
#     エラーを stderr に出して exit 2 で終える。systemd が FAILED と
#     判定し、従来の失敗警告パス（watchdog の失敗チェック経由の Slack
#     警告）が動く。つまり本当に壊れたコミットは見逃さない。
#
# 実行場所：slack-sync-check.service の ExecStart から呼ぶ。
# 引数：対象スクリプト（既定は slack-sync-check-run.sh）。テスト用に
# 変更できる。
set -u

TARGET="${1:-/home/hatch/workspace/neiro-slack-system/hooks/scripts/slack-sync-check-run.sh}"
STATE_FILE="${SYNC_GATE_STATE:-/home/hatch/hooks/state/sync-check-gate.state}"
GRACE_SECS=600

if ERR="$(bash -n "$TARGET" 2>&1)"; then
  rm -f "$STATE_FILE"
  exec /bin/bash "$TARGET"
fi

# 構文が壊れている（書きかけか、本当に壊れたコミットか）。
NOW="$(date +%s)"
FIRST=""
if [ -f "$STATE_FILE" ]; then
  FIRST="$(cat "$STATE_FILE" 2>/dev/null || true)"
fi
case "$FIRST" in
  ''|*[!0-9]*)
    FIRST="$NOW"
    printf '%s' "$NOW" > "$STATE_FILE"
    ;;
esac
if [ $((NOW - FIRST)) -ge "$GRACE_SECS" ]; then
  echo "slack-sync-check-gate.sh: $TARGET の構文が ${GRACE_SECS}秒以上壊れたままのため実行を中止します：" >&2
  printf '%s\n' "$ERR" >&2
  exit 2
fi
# 一時的な書きかけとみなし、静かにスキップする。
exit 0
