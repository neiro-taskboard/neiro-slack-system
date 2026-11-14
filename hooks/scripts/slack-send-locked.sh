#!/usr/bin/env bash
# slack-send-locked.sh — slack-send-once.sh を flock で直列化するラッパー。
#
# Yanayana さんの send_reply_locked.py（2026-11-14）の考え方を採用：
# check→send→record の全体を1つの flock 排他区間に入れることで、
# 並行するワーカーが同時に送信前チェックを抜けて二重送信する余地を
# なくす。うちの従来の送信前チェックだけでは、チェックと投稿の間に
# 相手が割り込む余地が原理的に残っていた（Yama さんの質問 2026-11-14）。
#
# flock はプロセス終了時に自動解放されるので、クラッシュしても
# stale lock にはならない（Yanayana さんの共有と同じ性質）。
# ロックはこのラッパー経由の送信同士でのみ有効。slack-send-once.sh を
# 直接呼ぶ経路が残っていると直列化されないので、返信はこのラッパー
# 経由で行うこと（フック定義 slack-new-messages.json を参照）。
#
# 使い方・引数・終了コードは slack-send-once.sh と同じ：
#   slack-send-locked.sh --channel C... --message '...' [--workspace muse-board]
#   slack-send-locked.sh --permalink <url> --message '...' [--workspace muse-board]
#   slack-send-locked.sh --channel C... --thread-ts <ts> --message-file note.txt
# ロックファイルは SLACK_SEND_LOCK_FILE で上書きできる（テスト用）。
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCK_FILE="${SLACK_SEND_LOCK_FILE:-$HOME/hooks/state/send.lock}"
mkdir -p "$(dirname "$LOCK_FILE")"

exec flock "$LOCK_FILE" "$SCRIPT_DIR/slack-send-once.sh" "$@"
