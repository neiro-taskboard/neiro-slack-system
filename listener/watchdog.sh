#!/usr/bin/env bash
# Watchdog for the Slack Socket Mode listener.
# - Reinstalls the systemd unit from the workspace copy if /etc lost it
#   (e.g. after a VM replacement), or if the installed copy is stale
#   (hash differs from the workspace copy — Yanayana's suggestion,
#   2026-11-12: a unit file can survive a rebuild while being outdated).
# - Starts the service if it is not running; restarts it when the unit
#   was updated so the new unit actually takes effect (daemon-reload
#   alone does not change a running service).
# - Backfills messages missed while the listener was down, from Slack
#   channel history (best effort; never fails the watchdog).
# Safe to run repeatedly; exits 0 in all expected cases.
set -u

UNIT_SRC="/home/hatch/workspace/slack-listener/slack-listener.service"
UNIT_DST="/etc/systemd/system/slack-listener.service"
SERVICE="slack-listener.service"

# Yanayana's finding (2026-11-13): after a VM restart timers.target itself
# can stay inactive, so enabled timers never fire. Our watchdog runs on a
# 5-minute scheduled job rather than a systemd timer, so it is not directly
# hit — but wake the target here anyway, so the VM's other timers
# (apt-daily etc.) do not stay asleep after a restart. Best effort: a
# failure here must not break the watchdog.
if ! systemctl is-active --quiet timers.target; then
  if systemctl start timers.target 2>/dev/null; then
    echo "timers.target started"
  else
    echo "timers.target start failed" >&2
  fi
fi

UNIT_CHANGED=0
if [ ! -f "$UNIT_DST" ] || ! cmp -s "$UNIT_SRC" "$UNIT_DST"; then
  # Reinstall with up to 3 attempts and exponential backoff (1s, 2s, 4s).
  # Yama's suggestion (2026-11-12): cap retries so a transient failure
  # cannot loop forever. A failed run is retried by the next cron run.
  ATTEMPT=1
  BACKOFF=1
  while [ "$ATTEMPT" -le 3 ]; do
    if cp "$UNIT_SRC" "$UNIT_DST" && systemctl daemon-reload; then
      UNIT_CHANGED=1
      echo "unit reinstalled (attempt $ATTEMPT)"
      break
    fi
    echo "unit reinstall attempt $ATTEMPT failed, retrying in ${BACKOFF}s" >&2
    sleep "$BACKOFF"
    BACKOFF=$((BACKOFF * 2))
    ATTEMPT=$((ATTEMPT + 1))
  done
  if [ "$UNIT_CHANGED" = "0" ]; then
    echo "unit reinstall failed after 3 attempts" >&2
  fi
fi

if ! systemctl is-active --quiet "$SERVICE"; then
  systemctl enable --now "$SERVICE" >/dev/null 2>&1 || systemctl start "$SERVICE"
  echo "service started"
elif [ "$UNIT_CHANGED" = "1" ]; then
  systemctl restart "$SERVICE"
  echo "service restarted with updated unit"
else
  echo "service running"
fi

# Auto-join timer units (2026-11-18): auto-join.py runs on a 1-minute
# systemd timer (slack-auto-join.timer) instead of being called
# directly from this watchdog. Reinstall the units from the workspace
# copies if /etc lost them (VM replacement) or they are stale, and
# make sure the timer is enabled and active. Best effort: a failure
# here must not break the watchdog.
AJ_CHANGED=0
for AJ_UNIT in slack-auto-join.service slack-auto-join.timer; do
  AJ_SRC="/home/hatch/workspace/slack-listener/$AJ_UNIT"
  AJ_DST="/etc/systemd/system/$AJ_UNIT"
  if [ ! -f "$AJ_DST" ] || ! cmp -s "$AJ_SRC" "$AJ_DST"; then
    if cp "$AJ_SRC" "$AJ_DST"; then
      AJ_CHANGED=1
      echo "auto-join unit reinstalled: $AJ_UNIT"
    else
      echo "auto-join unit reinstall failed: $AJ_UNIT" >&2
    fi
  fi
done
if [ "$AJ_CHANGED" = "1" ]; then
  systemctl daemon-reload || true
fi
if ! systemctl is-active --quiet slack-auto-join.timer; then
  systemctl enable --now slack-auto-join.timer >/dev/null 2>&1 || systemctl start slack-auto-join.timer
  echo "auto-join timer started"
fi

# 自動参加サービスの失敗警告（2026-11-18、Yama さんの質問を取り込み）。
# oneshot の失敗はタイマーを止めないので、上の「タイマーが active か」
# だけでは毎分繰り返す失敗が見えず、人が systemd の状態を見に行くまで
# 誰も気づかない。失敗状態なら Slack へ警告する（重複抑止はハッシュ付き
# 状態ファイルで、上の同期チェックと同じ形）。復旧したら状態ファイルを消す。
AJ_FAIL_CHECK="/home/hatch/workspace/neiro-slack-system/hooks/scripts/slack-autojoin-fail-check.sh"
AJ_FAIL_STATE="/home/hatch/hooks/state/auto-join-failed.state"
if [ -x "$AJ_FAIL_CHECK" ]; then
  if ! AJ_FAIL_OUT="$("$AJ_FAIL_CHECK" 2>&1)"; then
    AJ_FAIL_SIG="$(printf '%s' "$AJ_FAIL_OUT" | md5sum | cut -d' ' -f1)"
    if [ ! -f "$AJ_FAIL_STATE" ] || [ "$(cat "$AJ_FAIL_STATE" 2>/dev/null)" != "$AJ_FAIL_SIG" ]; then
      printf '%s' "$AJ_FAIL_SIG" > "$AJ_FAIL_STATE"
      AJ_FAIL_MSG="自動参加（slack-auto-join.service）が失敗しています。$(printf '%s' "$AJ_FAIL_OUT" | head -3 | tr '\n' ' ')"
      /home/hatch/hooks/scripts/slack-send-locked.sh --channel C0CGYGT9V7G --workspace muse-board --request-id "auto-join-failed-$AJ_FAIL_SIG" --message "$AJ_FAIL_MSG" >/dev/null 2>&1 || true
    fi
  else
    rm -f "$AJ_FAIL_STATE"
  fi
fi

# フックの単一正本チェック（2026-11-15、Yama さんの同期ずれ指摘を取り込み）。
# 稼働中の ~/hooks はリポジトリへのシンボリックリンクが正本。リンク切れ・
# 通常ファイルによる上書き・壊れた構文があれば Slack へ警告する。照合の
# 責務はフックではなくウォッチドッグ側（Yama さん・agy の指摘）。
# 警告は失敗内容のハッシュが変わったときだけ送る（5分間隔の連投を防ぐ）。
# チェック自体はリポジトリ側の正本を直接呼ぶので、~/hooks 側のリンクが
# 壊れていても動く。警告の送信に失敗してもウォッチドッグは止めない。
SYNC_CHECK="/home/hatch/workspace/neiro-slack-system/hooks/scripts/slack-hook-sync-check.sh"
SYNC_STATE="/home/hatch/hooks/state/hook-sync-check.state"
if [ -x "$SYNC_CHECK" ]; then
  if ! SYNC_OUT="$("$SYNC_CHECK" 2>&1)"; then
    SYNC_SIG="$(printf '%s' "$SYNC_OUT" | md5sum | cut -d' ' -f1)"
    if [ ! -f "$SYNC_STATE" ] || [ "$(cat "$SYNC_STATE" 2>/dev/null)" != "$SYNC_SIG" ]; then
      printf '%s' "$SYNC_SIG" > "$SYNC_STATE"
      SYNC_MSG="フックの単一正本チェックで異常を検出しました。稼働中の ~/hooks とリポジトリのリンクが壊れているか、構文が壊れています。確認してください：$(printf '%s' "$SYNC_OUT" | head -5 | tr '\n' ' ')"
      /home/hatch/hooks/scripts/slack-send-locked.sh --channel C0CGYGT9V7G --workspace muse-board --request-id "hook-sync-$SYNC_SIG" --message "$SYNC_MSG" >/dev/null 2>&1 || true
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
      /home/hatch/hooks/scripts/slack-send-locked.sh --channel C0CGYGT9V7G --workspace muse-board --request-id "listener-sync-$LISTENER_SYNC_SIG" --message "$LISTENER_SYNC_MSG" >/dev/null 2>&1 || true
    fi
  else
    rm -f "$LISTENER_SYNC_STATE"
  fi
fi

# 最古の未処理イベントの警告（2026-11-19、Yanayana さんの依頼を取り込み）。
# fastpath の取り戻しは 120 秒、確認の余裕 60 秒で、警告は 180 秒。
# 最古の未処理（done / dead 以外）が 180 秒以上なら Slack へ警告し、
# フラグファイルを作る。警告なし（未処理 0 件、または最古でも 180 秒
# 未満）になったらフラグと警告状態を消す（0件で削除、の形）。
# 警告は最古のイベント ID が変わったときだけ送る（年齢の秒数を署名に
# 入れると 5 分ごとに変わって連投になるため）。復旧したら状態ファイルを
# 消すので、次の詰まりではまた警告が出る。警告の送信に失敗しても
# ウォッチドッグは止めない。
OLDEST_CHECK="/home/hatch/workspace/neiro-slack-system/hooks/scripts/slack-oldest-unprocessed-check.sh"
OLDEST_STATE="/home/hatch/hooks/state/oldest-unprocessed-warning.state"
OLDEST_FLAG="/home/hatch/hooks/state/oldest-unprocessed-warning.flag"
if [ -x "$OLDEST_CHECK" ]; then
  if OLDEST_OUT="$("$OLDEST_CHECK" 2>/dev/null)"; then
    rm -f "$OLDEST_FLAG" "$OLDEST_STATE"
  else
    OLDEST_FIELDS="$(printf '%s' "$OLDEST_OUT" | python3 -c 'import json,sys; d=json.load(sys.stdin); print((d.get("oldest_event_id") or "") + "|" + str(d.get("oldest_age_secs") or "") + "|" + str(d.get("unprocessed_count") or ""))' 2>/dev/null)"
    IFS='|' read -r OLDEST_ID OLDEST_AGE OLDEST_COUNT <<< "$OLDEST_FIELDS"
    if [ -n "$OLDEST_ID" ]; then
      printf '%s' "$OLDEST_ID" > "$OLDEST_FLAG"
      OLDEST_SIG="$(printf '%s' "$OLDEST_ID" | md5sum | cut -d' ' -f1)"
      if [ ! -f "$OLDEST_STATE" ] || [ "$(cat "$OLDEST_STATE" 2>/dev/null)" != "$OLDEST_SIG" ]; then
        printf '%s' "$OLDEST_SIG" > "$OLDEST_STATE"
        OLDEST_MSG="最古の未処理イベントが180秒以上経っています：${OLDEST_ID}（${OLDEST_AGE}秒、未処理 ${OLDEST_COUNT}件）。フックのワーカーが起動していないか、処理が詰まっている可能性があります。"
        /home/hatch/hooks/scripts/slack-send-locked.sh --channel C0CGYGT9V7G --workspace muse-board --request-id "oldest-unprocessed-$OLDEST_SIG" --message "$OLDEST_MSG" >/dev/null 2>&1 || true
      fi
    else
      echo "oldest-unprocessed check output unparseable: $OLDEST_OUT" >&2
    fi
  fi
fi

# Auto-join public channels (2026-11-18, owner's standing permission:
# the bot does not need to ask before joining) runs on the 1-minute
# systemd timer above, no longer as a direct call from this watchdog.
# It joins only public channels where the bot's user account is
# already a member and at least 2 members are present, so default
# empty channels and private channels are left alone.

# Catch up on messages posted while the listener was down. Best effort:
# a failure here must not break the watchdog itself.
python3 /home/hatch/workspace/slack-listener/backfill.py 2>&1 || true
