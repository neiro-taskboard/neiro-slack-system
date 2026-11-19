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
#
# キーごとの形状（2026-11-19、Yama さんの受け取り口の見直しへの対応）：
# bot_neiro_* はボットの「ユーザーID」（U...／Grid の W...）のキーなの
# で、ユーザーID専用の形状 [UW]0... で検証する。Bot ID（B...）や
# トークン（xox...）が誤登録されていても、ここで非0で止める（例外で
# 大きく失敗させる。警告して既定値に戻すような推測の置き換えはしない）。
# 以前は全キー共通の汎用形状 [UWBCTGDA]0... で、B... をそのまま返して
# いた。呼び出し側（slack-send-once.sh／answered-check.py）が改めて
# ユーザーID形状で検証していたので実害はなかったが、共通ローダーが
# 通す形では将来の呼び出し側が検証を忘れたときに素通りする。受け取り口
# の一段目で弾くのが筋。workspace_*／channel_* は汎用形状のまま。
#
# 注意：このファイルは source される前提のため、トップレベルで set -u
# などのシェルオプションを変えないこと（2026-11-19、agyレビュー指摘）。
# 呼び出し元のフラグを勝手に書き換えると、set -u 前提で作られていない
# 別スクリプトから source されたときに予期せぬ停止を招く。

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
import os
import re
import sys

path, key = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except (OSError, ValueError) as exc:
    print(f"slack-ids.sh: cannot read ids file: {exc}", file=sys.stderr)
    sys.exit(1)
if not isinstance(data, dict):
    print("slack-ids.sh: ids file must contain a JSON object", file=sys.stderr)
    sys.exit(1)
# 実ID入りのファイルは 0600 が前提。他ユーザーに読める状態では
# fail-closed（コミットログの 0600 記載と実態を一致させる）。
if os.stat(path).st_mode & 0o077:
    print("slack-ids.sh: ids file must not be readable by others", file=sys.stderr)
    sys.exit(1)
value = data.get(key)
# bot_neiro_* はユーザーIDのキー。Bot ID（B...）やトークン（xox...）
# はユーザーID形状に合わないのでここで止まる（2026-11-19、Yama さん
# の受け取り口の見直し）。それ以外のキーは汎用ID形状で検証する。
if key.startswith("bot_neiro_"):
    pattern = r"[UW]0[A-Z0-9]{8,}"
else:
    pattern = r"[UWBCTGDA]0[A-Z0-9]{8,}"
if not isinstance(value, str) or not re.fullmatch(pattern, value):
    print(f"slack-ids.sh: missing or invalid id for key: {key}", file=sys.stderr)
    sys.exit(1)
print(value)
PY
}

# 直接実行されたときだけ、引数のキーを読んで表示する。
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
  slack_id "${1:-}"
fi
