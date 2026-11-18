#!/usr/bin/env bash
# slack-listener-sync-check.sh — 稼働中のリスナーとリポジトリの同期チェック。
#
# 背景（2026-11-18、Yama さんの質問を取り込み）：
# フック（~/hooks/scripts/*）はリポジトリへのシンボリックリンクで単一正本化
# され、slack-hook-sync-check.sh が5分間隔で照合しているが、リスナー側
# （~/workspace/slack-listener/*.py, *.sh, *.service, *.timer）は実体コピー
# のままで、自動の照合がなかった。2026-11-18 に Yanayana さんのガード
# （04d9ade）がリポジトリにはあるのに稼働中の auto-join.py へ反映されて
# いないずれが、手作業の diff で見つかった。このスクリプトは同じ罠を
# 繰り返さないため、稼働中のコピーとリポジトリの内容を照合する。
#
# 検証するもの：
#   1. リポジトリにあるリスナーファイルに対応する稼働中のファイルが存在すること
#   2. 内容が一致すること（cmp）
#   3. .py は両方とも py_compile を通り、.sh は両方とも bash -n を通ること
#
# 例外：archive.py は内容の照合対象外。公開リポジトリでは個人を特定しうる
# USER_NAMES 対応表を伏せており、稼働中のコピーと意図的に内容が違うため。
# 構文チェックだけは両方に行う。
#
# 実行場所：フックではなくウォッチドッグ側（5分間隔の watchdog.sh から呼ぶ。
# 照合の責務はフックではなくウォッチドッグ側、という 2026-11-15 の整理と同じ）。
# 正常時は exit 0 で何も出さない。異常時は問題を1行1件で stdout に出し
# exit 1。呼び出し側が Slack への警告と重複抑止（状態ファイル）を行う。
#
# 環境変数（テスト用）：LIVE_DIR（既定 $HOME/workspace/slack-listener）、
# REPO_LISTENER_DIR（既定 $HOME/workspace/neiro-slack-system/listener）。
set -u

LIVE_DIR="${LIVE_DIR:-$HOME/workspace/slack-listener}"
REPO_LISTENER_DIR="${REPO_LISTENER_DIR:-$HOME/workspace/neiro-slack-system/listener}"

PROBLEMS=0

check_one() {
  local rel="$1" live="$LIVE_DIR/$rel" repo="$REPO_LISTENER_DIR/$rel"
  if [ ! -e "$live" ]; then
    echo "MISSING: $live （稼働中のファイルが無い）"
    PROBLEMS=$((PROBLEMS + 1))
    return
  fi
  # archive.py は公開版で USER_NAMES を伏せているため内容照合の対象外。
  if [ "$rel" != "archive.py" ]; then
    if ! cmp -s "$live" "$repo"; then
      echo "DRIFT: $live （リポジトリと内容が違う。リポジトリ側を稼働中へ反映するか、稼働中の変更をリポジトリへ戻すこと）"
      PROBLEMS=$((PROBLEMS + 1))
    fi
  fi
  case "$rel" in
    *.sh)
      bash -n "$live" 2>/dev/null || {
        echo "SYNTAX-ERROR: $live （bash -n が通らない）"
        PROBLEMS=$((PROBLEMS + 1))
      }
      bash -n "$repo" 2>/dev/null || {
        echo "SYNTAX-ERROR: $repo （bash -n が通らない）"
        PROBLEMS=$((PROBLEMS + 1))
      }
      ;;
    *.py)
      python3 -m py_compile "$live" 2>/dev/null || {
        echo "SYNTAX-ERROR: $live （py_compile が通らない）"
        PROBLEMS=$((PROBLEMS + 1))
      }
      python3 -m py_compile "$repo" 2>/dev/null || {
        echo "SYNTAX-ERROR: $repo （py_compile が通らない）"
        PROBLEMS=$((PROBLEMS + 1))
      }
      ;;
  esac
}

# リポジトリ側にあるものだけを対象にする（稼働中だけの config.json や
# .bak、__pycache__、state/ は対象外。tests/ も対象外）。
while IFS= read -r -d '' f; do
  rel="${f#"$REPO_LISTENER_DIR"/}"
  check_one "$rel"
done < <(find "$REPO_LISTENER_DIR" -maxdepth 1 -type f \( -name '*.py' -o -name '*.sh' -o -name '*.service' -o -name '*.timer' \) -print0 2>/dev/null)

if [ "$PROBLEMS" -gt 0 ]; then
  echo "NG: ${PROBLEMS}件の問題あり"
  exit 1
fi
exit 0
