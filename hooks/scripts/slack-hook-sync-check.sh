#!/usr/bin/env bash
# slack-hook-sync-check.sh — フックの単一正本（シンボリックリンク）整合性チェック。
#
# 背景（2026-11-15、Yama さんの指摘を取り込み）：
# 稼働中の ~/hooks/scripts/*.sh,*.py と ~/hooks/definitions/*.json は、
# リポジトリ neiro-slack-system 内の hooks/ へのシンボリックリンクで、
# リポジトリが唯一の正本。リポジトリ側だけ直して稼働中に反映されない
# 同期ずれ（2026-11-15、IncompleteRead の修正が稼働中の hooks に
# 反映されず fail-closed が続いた件）を再発させないための仕組み。
#
# このスクリプトは以下を検証する：
#   1. リポジトリにある全フックファイルに対応する稼働中のパスが
#      シンボリックリンクであること（通常ファイルによる上書きの検出）
#   2. リンク先が存在し、期待するリポジトリパスを指していること
#      （リンク切れ・差し替えの検出）
#   3. JSON 定義がパースできること、シェルスクリプトが bash -n を通り、
#      Python ファイルが py_compile を通ること（壊れた編集中コードの検出）
#
# 対象は scripts/・definitions/ に加えて tests/（2026-11-21、Yanayana さん
# の提案を取り込み）。テストもリポジトリが単一正本で、稼働中の tests/ は
# リポジトリへのシンボリックリンクにする。リンクなら内容は同一なので
# scrub 比較は要らない（Yanayana さん側の scrub.sed とは方針が違う）。
# 稼働中にだけあるテストは UNPUBLISHED で拾う。tests/ は再帰的に見る
# （将来サブディレクトリのテストが出ても見逃さない。Yama さんの問いへの
# 回答）。意図的にローカル専用にするテストは今は無い。必要になったら
# チェック側に明示の除外リストを置く（黙って無視はしない）。
#
# 実行場所：フックではなくウォッチドッグ側（5分間隔の slack-sync-check.timer
# から slack-sync-check-run.sh 経由で呼ぶ。Yama さん・agy の指摘：照合の
# 責務はフックではなくウォッチドッグ側）。
# 正常時は exit 0 で何も出さない。異常時は問題を1行1件で stdout に出し
# exit 1。呼び出し側が Slack への警告と重複抑止（状態ファイル）を行う。
#
# 環境変数（テスト用）：HOOKS_DIR（既定 $HOME/hooks）、REPO_HOOKS_DIR
# （既定 $HOME/workspace/neiro-slack-system/hooks）。
set -u

HOOKS_DIR="${HOOKS_DIR:-$HOME/hooks}"
REPO_HOOKS_DIR="${REPO_HOOKS_DIR:-$HOME/workspace/neiro-slack-system/hooks}"

PROBLEMS=0

check_one() {
  local rel="$1" live="$HOOKS_DIR/$rel" repo="$REPO_HOOKS_DIR/$rel"
  if [ ! -L "$live" ]; then
    if [ -e "$live" ]; then
      echo "NOT-A-SYMLINK: $live （通常ファイルが置かれている。リポジトリへのリンクにすること）"
    else
      echo "MISSING: $live （稼働中のファイルが無い）"
    fi
    PROBLEMS=$((PROBLEMS + 1))
    return
  fi
  if [ ! -e "$live" ]; then
    echo "BROKEN-LINK: $live -> $(readlink "$live") （リンク先が無い）"
    PROBLEMS=$((PROBLEMS + 1))
    return
  fi
  local target
  target="$(readlink -f "$live")"
  local expected
  expected="$(readlink -f "$repo")"
  if [ "$target" != "$expected" ]; then
    echo "WRONG-TARGET: $live -> $target （期待： $expected）"
    PROBLEMS=$((PROBLEMS + 1))
    return
  fi
  case "$rel" in
    *.json)
      python3 -m json.tool "$live" >/dev/null 2>&1 || {
        echo "INVALID-JSON: $live"
        PROBLEMS=$((PROBLEMS + 1))
      }
      ;;
    *.sh)
      bash -n "$live" 2>/dev/null || {
        echo "SYNTAX-ERROR: $live （bash -n が通らない）"
        PROBLEMS=$((PROBLEMS + 1))
      }
      ;;
    *.py)
      python3 -m py_compile "$live" 2>/dev/null || {
        echo "SYNTAX-ERROR: $live （py_compile が通らない）"
        PROBLEMS=$((PROBLEMS + 1))
      }
      ;;
  esac
}

# リポジトリ側にあるものだけを対象にする（稼働中だけの .bak や
# __pycache__ は対象外）。tests/ も再帰的に対象。
while IFS= read -r -d '' f; do
  rel="${f#"$REPO_HOOKS_DIR"/}"
  check_one "$rel"
done < <(find "$REPO_HOOKS_DIR/scripts" "$REPO_HOOKS_DIR/definitions" "$REPO_HOOKS_DIR/tests" -type f \( -name '*.sh' -o -name '*.py' -o -name '*.json' \) -print0 2>/dev/null)

# 稼働中の tests/ にだけある .py は UNPUBLISHED（リポジトリに無いテストは
# 公開されていない）。稼働中だけの .bak は *.py ではないので対象外。
# -type f だけでなく -type l も見る：シンボリックリンクで置かれた未公開
# テストもすり抜けないようにする（2026-11-21、agyレビュー指摘）。
if [ -d "$HOOKS_DIR/tests" ]; then
  while IFS= read -r -d '' f; do
    rel="tests/${f#"$HOOKS_DIR/tests"/}"
    if [ ! -e "$REPO_HOOKS_DIR/$rel" ]; then
      echo "UNPUBLISHED: $f （稼働中のテストがリポジトリに無い）"
      PROBLEMS=$((PROBLEMS + 1))
    fi
  done < <(find "$HOOKS_DIR/tests" \( -type f -o -type l \) -name '*.py' -print0 2>/dev/null)
fi

if [ "$PROBLEMS" -gt 0 ]; then
  echo "NG: ${PROBLEMS}件の問題あり"
  exit 1
fi
exit 0
