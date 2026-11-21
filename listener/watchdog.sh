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

# 警告先チャンネルは ids.json から読む（2026-11-21、実ID外部化の残り4件）。
# fail-closed：読めなければ警告送信だけ skip し、空のチャンネルでは送らない。
# 読めないときに状態ファイルだけ先に更新すると、復旧後も警告が出なくなる
# （2026-11-19、sync-check-run.sh への agyレビュー指摘と同じ罠）ので、
# 状態ファイルの更新は送信が成功したときだけ行う。
IDS_LOADER="/home/hatch/hooks/scripts/slack-ids.sh"
ALERT_CHANNEL=""
if [ -f "$IDS_LOADER" ]; then
  # shellcheck source=/dev/null
  source "$IDS_LOADER"
  ALERT_CHANNEL="$(slack_id channel_welcome_muse_torishimariyakai)" || ALERT_CHANNEL=""
fi
if [ -z "$ALERT_CHANNEL" ]; then
  echo "watchdog.sh: 警告先チャンネルが読めないため警告送信を skip します（ids.json を確認してください）" >&2
fi

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

# 再起動前の構文チェック（2026-11-19、Yama さんの実装を取り込み）。
# listener.py を編集中にサービスを起動・再起動すると、書きかけの
# ファイルでリスナーが立ち上がってしまう。起動・再起動の直前に
# py_compile をかけ、通らなければその回は見送り、次の5分周期で
# 再試行する（書きかけなら直っているはず）。見送りは警告ではなく
# 静かにスキップし、ウォッチドッグ自体は exit 0 で終える。
# LISTENER_PY はテスト用の上書き可（既定は稼働中の listener.py）。
LISTENER_PY="${LISTENER_PY:-/home/hatch/workspace/slack-listener/listener.py}"
LISTENER_OK=1
if ! python3 -m py_compile "$LISTENER_PY" 2>/dev/null; then
  echo "listener.py py_compile failed; skipping service start/restart this cycle" >&2
  LISTENER_OK=0
fi

if [ "$LISTENER_OK" = "0" ]; then
  echo "service start/restart skipped (listener.py syntax check failed)"
elif ! systemctl is-active --quiet "$SERVICE"; then
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

# Sync-check timer units (2026-11-19): the hook and listener sync checks
# run on a 5-minute systemd timer (slack-sync-check.timer, Yanayana's
# timer approach) instead of being called directly from this watchdog.
# Same shape as auto-join above: reinstall the units from the workspace
# copies if /etc lost them or they are stale, and make sure the timer is
# enabled and active. Best effort: a failure here must not break the
# watchdog. watchdog.sh itself stays a check target of the listener
# sync check, which is the answer to Yama's "who checks watchdog.sh"
# question — the checks it triggers also check it.
SC_CHANGED=0
for SC_UNIT in slack-sync-check.service slack-sync-check.timer; do
  SC_SRC="/home/hatch/workspace/slack-listener/$SC_UNIT"
  SC_DST="/etc/systemd/system/$SC_UNIT"
  if [ ! -f "$SC_DST" ] || ! cmp -s "$SC_SRC" "$SC_DST"; then
    if cp "$SC_SRC" "$SC_DST"; then
      SC_CHANGED=1
      echo "sync-check unit reinstalled: $SC_UNIT"
    else
      echo "sync-check unit reinstall failed: $SC_UNIT" >&2
    fi
  fi
done
if [ "$SC_CHANGED" = "1" ]; then
  systemctl daemon-reload || true
fi
if ! systemctl is-active --quiet slack-sync-check.timer; then
  systemctl enable --now slack-sync-check.timer >/dev/null 2>&1 || systemctl start slack-sync-check.timer
  echo "sync-check timer started"
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
      AJ_FAIL_MSG="自動参加（slack-auto-join.service）が失敗しています。$(printf '%s' "$AJ_FAIL_OUT" | head -3 | tr '\n' ' ')"
      if [ -n "$ALERT_CHANNEL" ] && /home/hatch/hooks/scripts/slack-send-locked.sh --channel "$ALERT_CHANNEL" --workspace muse-board --request-id "auto-join-failed-$AJ_FAIL_SIG" --message "$AJ_FAIL_MSG" >/dev/null 2>&1; then
        printf '%s' "$AJ_FAIL_SIG" > "$AJ_FAIL_STATE"
      fi
    fi
  else
    rm -f "$AJ_FAIL_STATE"
  fi
fi

# 同期チェックサービスの失敗警告（2026-11-19、自動参加と同じ形）。
# oneshot の失敗はタイマーを止めないので、タイマーが active かだけでは
# 5分ごとに繰り返す失敗が見えず、同期チェックが止まったまま誰も
# 気づかない。失敗状態なら Slack へ警告する（重複抑止はハッシュ付き
# 状態ファイルで、上の自動参加と同じ形）。復旧したら状態ファイルを消す。
SC_FAIL_STATE="/home/hatch/hooks/state/sync-check-failed.state"
if [ -x "$AJ_FAIL_CHECK" ]; then
  if ! SC_FAIL_OUT="$(SERVICE=slack-sync-check.service FAIL_NOTE="同期チェックの実行が失敗しています。systemd の状態を確認してください。" "$AJ_FAIL_CHECK" 2>&1)"; then
    SC_FAIL_SIG="$(printf '%s' "$SC_FAIL_OUT" | md5sum | cut -d' ' -f1)"
    if [ ! -f "$SC_FAIL_STATE" ] || [ "$(cat "$SC_FAIL_STATE" 2>/dev/null)" != "$SC_FAIL_SIG" ]; then
      SC_FAIL_MSG="同期チェック（slack-sync-check.service）が失敗しています。$(printf '%s' "$SC_FAIL_OUT" | head -3 | tr '\n' ' ')"
      if [ -n "$ALERT_CHANNEL" ] && /home/hatch/hooks/scripts/slack-send-locked.sh --channel "$ALERT_CHANNEL" --workspace muse-board --request-id "sync-check-failed-$SC_FAIL_SIG" --message "$SC_FAIL_MSG" >/dev/null 2>&1; then
        printf '%s' "$SC_FAIL_SIG" > "$SC_FAIL_STATE"
      fi
    fi
  else
    rm -f "$SC_FAIL_STATE"
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
        OLDEST_MSG="最古の未処理イベントが180秒以上経っています：${OLDEST_ID}（${OLDEST_AGE}秒、未処理 ${OLDEST_COUNT}件）。フックのワーカーが起動していないか、処理が詰まっている可能性があります。"
        if [ -n "$ALERT_CHANNEL" ] && /home/hatch/hooks/scripts/slack-send-locked.sh --channel "$ALERT_CHANNEL" --workspace muse-board --request-id "oldest-unprocessed-$OLDEST_SIG" --message "$OLDEST_MSG" >/dev/null 2>&1; then
          printf '%s' "$OLDEST_SIG" > "$OLDEST_STATE"
        fi
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
