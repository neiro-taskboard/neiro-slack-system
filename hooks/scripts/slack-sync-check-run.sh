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
# 正常時は exit 0 で何も出さない。異常時は Slack へ警告する（重複抑止は
# 失敗内容のハッシュ付き状態ファイルで、内容が変わったときだけ送る）。
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

# フックの単一正本チェック（2026-11-15、Yama さんの同期ずれ指摘を取り込み）。
# 稼働中の ~/hooks はリポジトリへのシンボリックリンクが正本。リンク切れ・
# 通常ファイルによる上書き・壊れた構文があれば Slack へ警告する。
SYNC_CHECK="/home/hatch/workspace/neiro-slack-system/hooks/scripts/slack-hook-sync-check.sh"
SYNC_STATE="/home/hatch/hooks/state/hook-sync-check.state"
if [ -x "$SYNC_CHECK" ]; then
  if ! SYNC_OUT="$("$SYNC_CHECK" 2>&1)"; then
    SYNC_SIG="$(printf '%s' "$SYNC_OUT" | md5sum | cut -d' ' -f1)"
    if [ ! -f "$SYNC_STATE" ] || [ "$(cat "$SYNC_STATE" 2>/dev/null)" != "$SYNC_SIG" ]; then
      printf '%s' "$SYNC_SIG" > "$SYNC_STATE"
      SYNC_MSG="フックの単一正本チェックで異常を検出しました。稼働中の ~/hooks とリポジトリのリンクが壊れているか、構文が壊れています。確認してください：$(printf '%s' "$SYNC_OUT" | head -5 | tr '\n' ' ')"
      if [ -n "$ALERT_CHANNEL" ]; then
        /home/hatch/hooks/scripts/slack-send-locked.sh --channel "$ALERT_CHANNEL" --workspace muse-board --request-id "hook-sync-$SYNC_SIG" --message "$SYNC_MSG" >/dev/null 2>&1 || true
      fi
    fi
  else
    rm -f "$SYNC_STATE"
  fi
fi

# リスナーの同期チェック（2026-11-18、Yama さんの質問を取り込み）。
# フックはシンボリックリンクで単一正本化されているが、リスナー側
# （~/workspace/slack-listener/*.py など）は実体コピーのため、同じ罠が
# 起きた。Yanayana さんのガードがリポジトリにだけあって稼働中の
# auto-join.py に反映されていないずれを、手作業の diff で見つけたのが
# きっかけ。上のフックのチェックと同じ形で、内容のずれ・欠落・構文
# エラーを検出したら Slack へ警告する（重複抑止はハッシュ付き状態ファイル）。
LISTENER_SYNC_CHECK="/home/hatch/workspace/neiro-slack-system/hooks/scripts/slack-listener-sync-check.sh"
LISTENER_SYNC_STATE="/home/hatch/hooks/state/listener-sync-check.state"
if [ -x "$LISTENER_SYNC_CHECK" ]; then
  if ! LISTENER_SYNC_OUT="$("$LISTENER_SYNC_CHECK" 2>&1)"; then
    LISTENER_SYNC_SIG="$(printf '%s' "$LISTENER_SYNC_OUT" | md5sum | cut -d' ' -f1)"
    if [ ! -f "$LISTENER_SYNC_STATE" ] || [ "$(cat "$LISTENER_SYNC_STATE" 2>/dev/null)" != "$LISTENER_SYNC_SIG" ]; then
      printf '%s' "$LISTENER_SYNC_SIG" > "$LISTENER_SYNC_STATE"
      LISTENER_SYNC_MSG="リスナーの同期チェックで異常を検出しました。稼働中の ~/workspace/slack-listener とリポジトリの内容がずれています。確認してください：$(printf '%s' "$LISTENER_SYNC_OUT" | head -5 | tr '\n' ' ')"
      if [ -n "$ALERT_CHANNEL" ]; then
        /home/hatch/hooks/scripts/slack-send-locked.sh --channel "$ALERT_CHANNEL" --workspace muse-board --request-id "listener-sync-$LISTENER_SYNC_SIG" --message "$LISTENER_SYNC_MSG" >/dev/null 2>&1 || true
      fi
    fi
  else
    rm -f "$LISTENER_SYNC_STATE"
  fi
fi

exit 0
