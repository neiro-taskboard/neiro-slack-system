#!/usr/bin/env bash
# slack-autojoin-fail-check.sh — 自動参加サービスの失敗検出。
#
# 背景（2026-11-18、Yama さんの質問を取り込み）：
# auto-join.py は join state が本体・.bak ともに読めないとき main が
# return 1 で終わり、systemd タイマーのユニットが失敗として目立つ設計
# （fail-closed）。だが oneshot サービスの失敗はタイマーを止めないため、
# watchdog の「タイマーが active か」だけでは毎分繰り返す失敗が見えず、
# 人が systemd の状態を見に行くまで誰も気づかない。このスクリプトは
# systemctl is-failed でサービスの失敗状態を見て、失敗内容を出す。
#
# 実行場所：フックではなくウォッチドッグ側（5分間隔の watchdog.sh から
# 呼ぶ。照合の責務はフックではなくウォッチドッグ側、という 2026-11-15
# の整理と同じ）。正常時は exit 0 で何も出さない。失敗時は問題を
# stdout に出し exit 1。呼び出し側が Slack への警告と重複抑止
# （状態ファイル）を行う。
#
# 環境変数（テスト用）：SYSTEMCTL（既定 systemctl）、SERVICE
# （既定 slack-auto-join.service）、FAIL_NOTE（既定は自動参加の
# join state についての説明。同期チェックなど他のサービスで使うときは
# そのサービスに合う文言を指定すること）。
set -u

SYSTEMCTL="${SYSTEMCTL:-systemctl}"
SERVICE="${SERVICE:-slack-auto-join.service}"
FAIL_NOTE="${FAIL_NOTE:-join state が読めないなどで main が return 1 で終わっています。systemd の状態を確認してください。}"

if "$SYSTEMCTL" is-failed --quiet "$SERVICE"; then
  RESULT="$("$SYSTEMCTL" show -p Result --value "$SERVICE" 2>/dev/null || echo unknown)"
  STATUS="$("$SYSTEMCTL" show -p ExecMainStatus --value "$SERVICE" 2>/dev/null || echo ?)"
  echo "FAILED: $SERVICE （Result=$RESULT、終了コード=$STATUS）"
  echo "$FAIL_NOTE"
  exit 1
fi
exit 0
