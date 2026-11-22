#!/usr/bin/env bash
# slack-sync-check-run.sh — フックとリスナーの同期チェックをまとめて実行する。
#
# 背景（2026-11-19、Yanayana さんの専用タイマーへの切り出しを取り込み）：
# これまで2つの同期チェック（フックの単一正本チェックとリスナーの同期
# チェック）は watchdog.sh から直接呼ばれていた。Yanayana さんの
# slack-sync-check.timer（5分）と同じ形にし、呼び出し元を専用タイマー
# （slack-sync-check.timer、5分間隔）へ切り出す。watchdog.sh は
# auto-join と同じ形で、ユニットの再設置とタイマーの有効化だけを見る。
# Yama さんの「watchdog.sh 自体は誰が照合するのか」という問いの答えは
# 変わらない：watchdog.sh 自体はリスナーの同期チェックの照合対象で、
# チェックを呼ぶタイマーのユニットは watchdog.sh が再設置する。
#
# 実行場所：systemd タイマー slack-sync-check.timer（5分間隔）から呼ぶ。
# 正常時は exit 0 で何も出さない。異常時は Slack へ警告する。
# 警告には猶予がある（2026-11-22、Yanayana さんの同期チェック警告の
# 猶予期間を取り込み。公開側 commit 9307903）：同じずれ（失敗内容の
# 署名）が約15分（5分間隔で3回分）続いたときだけ警告する。初回検出は
# 状態ファイルに署名と first_seen を記録して黙り、署名が変われば時計は
# リセット、ずれが解消されれば状態ごとクリア。警告送信の失敗は猶予の
# 対象外で、従来どおり即座に表面化する（送信に失敗したときは warned を
# 立てないので、次回は猶予を待たず再送を試みる）。チェック自体の異常
# 終了も猶予の対象外だが、こちらは slack-sync-check-gate.sh が別途
# カバーしている（ランナーの構文が10分以上壊れたままなら exit 2 で
# FAILED 警告を出す）。警告済みのずれが変化した場合も、同じ猶予ルール
# を通す。
# 警告の送信に失敗しても exit 0 のまま終える（タイマーの失敗扱いにしない）。
# チェック自体はリポジトリ側の正本を直接呼ぶので、~/hooks 側のリンクが
# 壊れていても動く。
set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=slack-ids.sh
# shellcheck disable=SC1091
source "$SCRIPT_DIR/slack-ids.sh"
# 警告先チャンネルは ids.json から読む（2026-11-19、実ID外部化の第1本）。
# fail-closed：読めなければ警告送信だけ skip し、空のチャンネルでは送らない。
ALERT_CHANNEL="$(slack_id channel_welcome_muse_torishimariyakai)" || ALERT_CHANNEL=""
if [ -z "$ALERT_CHANNEL" ]; then
  echo "slack-sync-check-run.sh: 警告先チャンネルが読めないため警告送信を skip します（ids.json を確認してください）" >&2
fi

# 猶予（秒）。5分間隔のタイマーで3回分＝約15分。
# 環境変数で上書きできる（テスト用）。
GRACE_SECS="${SYNC_CHECK_RUN_GRACE_SECS:-900}"

# チェックスクリプトと状態ファイルのパスは環境変数で上書きできる
# （テスト用。既定は従来どおり）。
HOOK_CHECK="${SYNC_CHECK_RUN_HOOK_CHECK:-/home/hatch/workspace/neiro-slack-system/hooks/scripts/slack-hook-sync-check.sh}"
HOOK_STATE="${SYNC_CHECK_RUN_HOOK_STATE:-/home/hatch/hooks/state/hook-sync-check.state}"
LISTENER_CHECK="${SYNC_CHECK_RUN_LISTENER_CHECK:-/home/hatch/workspace/neiro-slack-system/hooks/scripts/slack-listener-sync-check.sh}"
LISTENER_STATE="${SYNC_CHECK_RUN_LISTENER_STATE:-/home/hatch/hooks/state/listener-sync-check.state}"

# 状態ファイルの形式：
#   sig=<失敗内容の md5>
#   first_seen=<初回検出の epoch 秒>
#   warned=<0|1>
# 旧形式（署名だけ1行）のファイルは、署名が一致すれば warned=1 として
# 読み替える（移行前から警告済みのずれを、移行直後に猶予で遅らせないため）。
write_state() {
  printf 'sig=%s\nfirst_seen=%s\nwarned=%s\n' "$1" "$2" "$3" > "$4"
}

# 1件の同期チェックを実行し、必要なら警告する。
# 引数：<check_script> <state_file> <msg_prefix> <req_prefix>
# msg_prefix は警告文の先頭、req_prefix は request_id の接頭辞。
run_one_check() {
  local check_script="$1" state_file="$2" msg_prefix="$3" req_prefix="$4"
  [ -x "$check_script" ] || return 0
  local out sig now
  if out="$("$check_script" 2>&1)"; then
    rm -f "$state_file"
    return 0
  fi
  sig="$(printf '%s' "$out" | md5sum | cut -d' ' -f1)"
  now="$(date +%s)"
  local state_sig="" state_first="" state_warned=0
  if [ -f "$state_file" ]; then
    local key val
    while IFS='=' read -r key val; do
      case "$key" in
        sig) state_sig="$val" ;;
        first_seen) state_first="$val" ;;
        warned) state_warned="$val" ;;
      esac
    done < "$state_file"
    if [ -z "$state_sig" ]; then
      # 旧形式（署名だけ1行）か、壊れたファイル。
      local only
      only="$(head -n 1 "$state_file" 2>/dev/null | tr -d '[:space:]')"
      if [ -n "$only" ] && [ "$only" = "$sig" ]; then
        state_sig="$sig"
        state_first="$now"
        state_warned=1
      fi
    fi
  fi
  # first_seen が数字でなければ初回検出として扱う（壊れた状態ファイルは
  # 時計をリセットする。ずれが続いていれば猶予後に警告が出る）。
  case "$state_first" in
    ''|*[!0-9]*) state_sig="" ;;
  esac
  if [ "$state_warned" != "1" ]; then
    state_warned=0
  fi
  if [ -z "$state_sig" ] || [ "$state_sig" != "$sig" ]; then
    # 初回検出か、署名が変わった：時計をリセットして黙る。
    write_state "$sig" "$now" 0 "$state_file"
    return 0
  fi
  if [ "$state_warned" = "1" ]; then
    return 0
  fi
  if [ $((now - state_first)) -lt "$GRACE_SECS" ]; then
    return 0
  fi
  # 猶予を過ぎた：警告する。
  local msg req
  msg="${msg_prefix}$(printf '%s' "$out" | head -5 | tr '\n' ' ')"
  # request_id は失敗内容のハッシュだけではだめで、発生時刻も入れる
  # （2026-11-21、Claude Code（Opus）の指摘1件目を取り込み）。
  # ハッシュだけだと、一度復旧して同じ障害が再発したときに前の警告と
  # 同じ request_id になり、送信ガードの metadata 照合が「重複」と
  # 判定して再警告を送らない。状態ファイルが同一エピソード内の再送
  # を抑えるので、request_id 側はエピソードごとに一意でよい。
  req="$req_prefix-$sig-$now"
  # warned は送信が成功したときだけ立てる（2026-11-19、agyレビュー指摘）。
  # 先に立てると、ids.json 欠落などで送信が skip された異常が永久に
  # 送られなくなる。送信に失敗したときは warned=0 のままなので、次回は
  # 猶予を待たず再送を試みる（送信失敗は猶予の対象外）。
  if [ -n "$ALERT_CHANNEL" ] && "$SCRIPT_DIR/slack-send-locked.sh" --channel "$ALERT_CHANNEL" --workspace muse-board --request-id "$req" --message "$msg" >/dev/null 2>&1; then
    write_state "$sig" "$state_first" 1 "$state_file"
  fi
  return 0
}

# フックの単一正本チェック（2026-11-15、Yama さんの同期ずれ指摘を取り込み）。
# 稼働中の ~/hooks はリポジトリへのシンボリックリンクが正本。リンク切れ・
# 通常ファイルによる上書き・壊れた構文があれば Slack へ警告する。
run_one_check "$HOOK_CHECK" "$HOOK_STATE" \
  "フックの単一正本チェックで異常を検出しました。稼働中の ~/hooks とリポジトリのリンクが壊れているか、構文が壊れています。確認してください：" \
  "hook-sync"

# リスナーの同期チェック（2026-11-18、Yama さんの質問を取り込み）。
# フックはシンボリックリンクで単一正本化されているが、リスナー側
# （~/workspace/slack-listener/*.py など）は実体コピーのため、同じ罠が
# 起きた。Yanayana さんのガードがリポジトリにだけあって稼働中の
# auto-join.py に反映されていないずれを、手作業の diff で見つけたのが
# きっかけ。上のフックのチェックと同じ形で、内容のずれ・欠落・構文
# エラーを検出したら Slack へ警告する。
run_one_check "$LISTENER_CHECK" "$LISTENER_STATE" \
  "リスナーの同期チェックで異常を検出しました。稼働中の ~/workspace/slack-listener とリポジトリの内容がずれています。確認してください：" \
  "listener-sync"

exit 0
