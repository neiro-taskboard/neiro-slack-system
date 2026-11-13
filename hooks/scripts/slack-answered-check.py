#!/usr/bin/env python3
"""Answer check before a delayed (DLQ) resend.

遅れての再送を送る前に、そのスレッドがすでに答え済みかどうかを確認する。
Yanayana の reconcile_ambiguous.py の reconcile_pending_resends の判定を、
ねいろの構成（ワーカーが slack-send-once.sh で送る）に合わせて取り込んだ
もの（2026-11-14）。

判定（スレッドの親 ts を起点に全ページ走査する。時刻順序つき）:
  1. parent_deleted — チャンネル履歴に親投稿がない。送り先がないので再送しない。
  2. answered_by_other — 元の投稿より後に、質問者と自分（ねいろ）以外による
     本文のあるテキスト投稿が 1 件でもあれば答え済み。再送しない。
  3. どちらでもなければ ok_to_resend。

順序の注意（2026-11-14、Yanayana の実装へのねいろのレビュー指摘と同じ考え）:
  「既送信チェック」はこのスクリプトでは行わない。ねいろの waiting 再送では
  元の送信は pre-check 失敗で試みられていない（fail-closed で送る前に止まる）
  ため、既送信の経路はそもそも起きない。加えて、送るときは必ず
  slack-send-once.sh の送信前チェック（台帳＋スレッド全体の重複確認）が先に
  走るので、届いていた返信の二重投稿はそちらで防がれる。

使い方:
  slack-answered-check.py --channel C... --event-ts 1794.......
      [--thread-ts 1794.......] --questioner U... [--workspace muse-board]
      [--self-id U...] [--slackcli PATH]
  --thread-ts がなければイベント ts を親とする。--self-id がなければ
  slackcli auth whoami から取る（取れなければ既知の対応表にフォールバック）。

出力: JSON 1行 {"verdict": "ok_to_resend"|"answered_by_other"|"parent_deleted",
  "answered_by_ts": ..., "checked_messages": N, ...}
終了コード: 0 = 判定できた（再送可・不可のいずれも）。2 = スレッド／履歴が
  読めなかった（cannot_verify）。読めないときは送らず waiting に戻すこと。
  判定できた場合は、理由つきで done にしてよい。
"""
import json
import re
import subprocess
import sys
import time

SLACKCLI = "/home/hatch/workspace/bin/slackcli"
SELF_IDS = {"muse-board": "U0CH1B19D4J", "T0CGV6V7161": "U0CH1B19D4J"}
DEFAULT_SELF_ID = "U0CH32D1P5F"  # Neiro Task Board
HISTORY_ATTEMPTS = 5  # slack-send-once.sh と同じ回数
BACKOFF_SECS = (1, 2, 4, 8)  # 指数バックオフ（同上）
MAX_PAGES = 20
PAGE_LIMIT = 50


def parse_slack_json(raw):
    """slackcli の出力から JSON 部分だけ抜いて読む。読めなければ None。"""
    i = raw.find("{")
    if i < 0:
        return None
    try:
        return json.loads(raw[i:])
    except json.JSONDecodeError:
        return None


def self_user_id(workspace, slackcli=SLACKCLI):
    """自分のユーザーID。whoami が取れなければ既知の対応表にフォールバック。"""
    try:
        args = [slackcli, "auth", "whoami"]
        if workspace:
            args += ["--workspace", workspace]
        out = subprocess.run(args, capture_output=True, text=True,
                             timeout=30).stdout
        m = re.search(r"\(U[A-Z0-9]+", out)
        if m:
            return m.group(0)[1:]
    except Exception:
        pass
    return SELF_IDS.get(workspace or "", DEFAULT_SELF_ID)


def is_reply_after_event(msg_ts, event_ts):
    """返信 ts がイベント ts より後か（数値比較、だめなら文字列比較）。

    Yanayana の _is_reply_after_event と同じ。ts が無い返信は決して
    一致しない（到達を推測しない）。
    """
    if msg_ts is None or event_ts is None:
        return False
    try:
        return float(msg_ts) > float(event_ts)
    except (TypeError, ValueError):
        return str(msg_ts) > str(event_ts)


def read_page(slackcli, channel, oldest, thread_ts, workspace):
    """slackcli で履歴1ページを読む。読めなければ RuntimeError。"""
    args = [slackcli, "conversations", "read", channel,
            "--limit", str(PAGE_LIMIT), "--json", "--oldest", str(oldest)]
    if thread_ts:
        args += ["--thread-ts", thread_ts]
    if workspace:
        args += ["--workspace", workspace]
    r = subprocess.run(args, capture_output=True, text=True, timeout=60)
    data = parse_slack_json(r.stdout or "")
    if data is None or not isinstance(data.get("messages"), list):
        raise RuntimeError(f"slackcli read failed (exit {r.returncode})")
    return data


def read_page_retry(slackcli, channel, oldest, thread_ts, workspace):
    """1ページの読み取りを最大5回リトライする（間隔 1・2・4・8 秒）。"""
    last_exc = None
    for attempt in range(HISTORY_ATTEMPTS):
        try:
            return read_page(slackcli, channel, oldest, thread_ts, workspace)
        except Exception as exc:  # noqa: BLE001 - リトライ対象は広く取る
            last_exc = exc
            if attempt < HISTORY_ATTEMPTS - 1:
                time.sleep(BACKOFF_SECS[min(attempt, len(BACKOFF_SECS) - 1)])
    raise RuntimeError(f"history unreadable after retries: {last_exc}")


def read_thread_all(page_reader, channel, thread_ts):
    """スレッドの全メッセージを古い順に集める。

    --oldest=親ts からページングする（slack-send-once.sh と同じ流儀。
    --oldest なしだと初回が「親＋最新N件」になり、next_oldest が最新を
    指して古い返信が見えなくなる）。
    """
    messages = []
    oldest = thread_ts
    for _ in range(MAX_PAGES):
        data = page_reader(channel, thread_ts, oldest)
        messages.extend({"ts": m.get("ts"), "user": m.get("user"),
                         "text": m.get("text") or ""}
                        for m in data.get("messages", []))
        if not data.get("has_more"):
            break
        nxt = data.get("next_oldest")
        if not nxt or nxt == oldest:
            break
        oldest = nxt
    return messages


def check(channel, event_ts, thread_ts, questioner, self_id,
          page_reader, channel_reader):
    """答え済み確認の本体。reader はテストで差し替えられる。

    page_reader(channel, thread_ts, oldest) -> page dict
    channel_reader(channel, oldest) -> page dict
    どちらかが読めなければ {"verdict": "cannot_verify", ...} を返す。
    """
    parent_ts = thread_ts
    # 1. 親投稿がチャンネル履歴にあるか（--oldest=親-1秒で先頭に出る）。
    try:
        page = channel_reader(channel, float(parent_ts) - 1)
    except Exception as exc:  # noqa: BLE001 - 読めない理由は verdict に載せる
        return {"verdict": "cannot_verify", "reason": "parent_check_read_failed",
                "error": str(exc)[:200]}
    if not any(m.get("ts") == parent_ts
               for m in page.get("messages", [])):
        return {"verdict": "parent_deleted", "parent_ts": parent_ts}
    # 2. スレッド全件を走査し、イベントより後の他者による本文つき投稿を探す。
    try:
        replies = read_thread_all(page_reader, channel, thread_ts)
    except Exception as exc:  # noqa: BLE001 - 同上
        return {"verdict": "cannot_verify", "reason": "thread_read_failed",
                "error": str(exc)[:200]}
    answered_by = next(
        (m for m in replies
         if is_reply_after_event(m.get("ts"), event_ts)
         and m.get("user") not in (questioner, self_id)
         and (m.get("text") or "").strip()),
        None)
    if answered_by:
        return {"verdict": "answered_by_other",
                "answered_by_ts": answered_by.get("ts"),
                "answered_by_user": answered_by.get("user"),
                "checked_messages": len(replies)}
    return {"verdict": "ok_to_resend", "checked_messages": len(replies)}


def main(argv):
    opts = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--channel", "--event-ts", "--thread-ts", "--questioner",
                 "--workspace", "--self-id", "--slackcli") and i + 1 < len(argv):
            opts[a[2:]] = argv[i + 1]
            i += 2
        else:
            print(f"unknown argument: {a}", file=sys.stderr)
            return 2
    missing = [k for k in ("channel", "event-ts", "questioner")
               if k not in opts]
    if missing:
        print(f"missing --{' --'.join(missing)}", file=sys.stderr)
        return 2
    channel = opts["channel"]
    event_ts = opts["event-ts"]
    thread_ts = opts.get("thread-ts") or event_ts
    questioner = opts["questioner"]
    workspace = opts.get("workspace") or ""
    slackcli = opts.get("slackcli") or SLACKCLI
    self_id = opts.get("self-id") or self_user_id(workspace, slackcli)

    def page_reader(ch, th, oldest):
        return read_page_retry(slackcli, ch, oldest, th, workspace)

    def channel_reader(ch, oldest):
        return read_page_retry(slackcli, ch, oldest, None, workspace)

    result = check(channel, event_ts, thread_ts, questioner, self_id,
                   page_reader, channel_reader)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["verdict"] != "cannot_verify" else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
