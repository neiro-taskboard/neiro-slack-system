#!/usr/bin/env bash
# slack-ids.sh — Slack の実IDを ~/.config/neiro-slack/ids.json から読む共通ヘルパー。
#
# 背景（2026-11-19、muse取締役会 #slack-へようこそ の外部化設計）：
# スクリプト本体に直書きされていた実ID（ワークスペース／チャンネル／
# ボット）を、リポジトリ外の設定ファイルへ逃がす。公開用コピーが稼働中
# のものそのもののため、公開時 scrub ではなく外部化を選んだ。
#
# 使い方（source して使う）：
#   source "$(dirname "$0")/slack-ids.sh"
#   CHANNEL="$(slack_id channel_welcome_muse_torishimariyakai)" || exit 1
# 直接実行でも1件読める：
#   bash slack-ids.sh channel_welcome_muse_torishimariyakai
#
# fail-closed：ファイル欠落・JSON不正・キー欠落・ID形状でない値の
# いずれも非0で終了し、stdout には何も出さない（誤ったチャンネルへ
# 送る・空のチャンネルで送る余地を残さない）。テストでは環境変数
# NEIRO_SLACK_IDS_PATH で読むファイルを指定できる。
# キー一覧はリポジトリ直下の ids.example.json を参照（値は非ID形状の
# プレースホルダで、実値は入っていない）。
set -u

slack_id() {
  local key="${1:-}"
  if [ -z "$key" ]; then
    echo "slack-ids.sh: key is required" >&2
    return 1
  fi
  local path="${NEIRO_SLACK_IDS_PATH:-$HOME/.config/neiro-slack/ids.json}"
  if [ ! -f "$path" ]; then
    echo "slack-ids.sh: ids file not found: $path" >&2
    return 1
  fi
  python3 - "$path" "$key" <<'PY'
import json
import re
import sys

path, key = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except (OSError, ValueError) as exc:
    print(f"slack-ids.sh: cannot read ids file: {exc}", file=sys.stderr)
    sys.exit(1)
value = data.get(key)
if not isinstance(value, str) or not re.fullmatch(r"[UWBCTGDA]0[A-Z0-9]{8,}", value):
    print(f"slack-ids.sh: missing or invalid id for key: {key}", file=sys.stderr)
    sys.exit(1)
print(value)
PY
}

# 直接実行されたときだけ、引数のキーを読んで表示する。
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
  slack_id "${1:-}"
fi
