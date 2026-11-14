#!/usr/bin/env python3
"""chat.postMessage を直接叩いて metadata 付きで投稿する。

Yama さんの metadata 照合の設計（2026-11-14、muse取締役会 #slack-へようこそ）を
うちに取り込むための送信経路。slackcli の messages send には metadata を載せる
指定がないので、chat.postMessage を直接叩く必要がある。

印の形（Yama さんと同じ）:
  metadata: {"event_type": "neiro_reply",
             "event_payload": {"event_id": "<元のイベントID>"}}
  手動投稿（イベント駆動でないもの）は event_id の代わりに
  {"request_id": "<UUID>"} を入れる。
  うちのイベントIDは「workspace_id:channel:ts」の形で、ts を含む。

トークンは slackcli の認証ストア（~/.config/slackcli/workspaces.json）から
読む。トークン自体は出力・ログに一切出さない。

使い方:
  slack-post-metadata.py --channel C... --message '...' [--thread-ts TS]
      [--workspace muse-board] (--event-id <ID> | --request-id <UUID>)
      [--event-type neiro_reply] [--message-file FILE] [--dry-run]

出力: JSON 1行 {"ok": true, "ts": ..., "channel": ...}。
  失敗時は {"ok": false, "error": ...} を出し、終了コード 1。
  --dry-run は送らずに送るはずの payload を出す（トークンは読まない）。

注意: これは送信経路の第一歩。既存の返信フロー（slack-send-once.sh 経由）の
  移行は、event_id の中身（ts か台帳側の独自IDか）が Yama さんの回答で
  決まってから行う。読み取り側の include_all_metadata=True 対応と
  metadata 優先の照合もそのときにあわせて入れる。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

WORKSPACES_JSON = "/home/hatch/.config/slackcli/workspaces.json"
SLACK_API = "https://slack.com/api/chat.postMessage"
EVENT_TYPE = "neiro_reply"
MAX_ATTEMPTS = 3
BACKOFF_SECS = (1, 2)


def load_workspaces(path=None):
    """slackcli の認証ストアを読む。読めなければ None。"""
    if path is None:
        path = WORKSPACES_JSON
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def resolve_token(workspace, path=None):
    """ワークスペース指定からボットトークンを取る。

    workspace はプロファイル名（muse-board）・ワークスペースID
    （T0CGV6V7161）・None（デフォルト）のいずれか。見つからなければ None。
    """
    data = load_workspaces(path)
    if not data:
        return None
    entries = data.get("workspaces", {})
    entry = None
    if workspace is None:
        entry = entries.get(data.get("default_workspace", ""))
    elif workspace in entries:
        entry = entries[workspace]
    else:
        for e in entries.values():
            if e.get("workspace_id") == workspace:
                entry = e
                break
    if not entry:
        return None
    return entry.get("token")


def build_payload(channel, text, thread_ts=None, event_id=None,
                  request_id=None, event_type=EVENT_TYPE):
    """chat.postMessage の body を作る。event_id / request_id はどちらか必須。"""
    if bool(event_id) == bool(request_id):
        raise ValueError("event_id と request_id はどちらか一方だけ指定する")
    if event_id is not None:
        event_payload = {"event_id": str(event_id)}
    else:
        event_payload = {"request_id": str(request_id)}
    payload = {
        "channel": channel,
        "text": text,
        "metadata": {"event_type": event_type, "event_payload": event_payload},
    }
    if thread_ts:
        payload["thread_ts"] = thread_ts
    return payload


def post(payload, token, urlopen=urllib.request.urlopen, sleep=time.sleep):
    """chat.postMessage を叩く。成功時は (True, 応答)、失敗時は (False, 理由)。

    ネットワークエラー・429（ratelimited）・5xx は最大 MAX_ATTEMPTS 回まで
    リトライする（429 は Retry-After を尊重）。Slack の ok:false（チャンネル
    不明など）はリトライしない。
    """
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(
            SLACK_API, data=body,
            headers={"Authorization": "Bearer " + token,
                     "Content-Type": "application/json; charset=utf-8"},
            method="POST")
        try:
            with urlopen(req, timeout=30) as resp:
                raw = resp.read()
                retry_after = None
                try:
                    retry_after = resp.headers.get("Retry-After")
                except AttributeError:
                    pass
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                if attempt < MAX_ATTEMPTS:
                    wait = BACKOFF_SECS[attempt - 1]
                    try:
                        wait = float(e.headers.get("Retry-After", wait))
                    except (AttributeError, ValueError, TypeError):
                        pass
                    sleep(wait)
                    continue
                return False, "http_%d" % e.code
            return False, "http_%d" % e.code
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt < MAX_ATTEMPTS:
                sleep(BACKOFF_SECS[attempt - 1])
                continue
            return False, "network: %s" % e
        try:
            result = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False, "bad_response"
        if result.get("ok"):
            return True, result
        if result.get("error") == "ratelimited":
            if attempt < MAX_ATTEMPTS:
                wait = BACKOFF_SECS[attempt - 1]
                try:
                    wait = float(result.get("retry_after", wait))
                    if retry_after is not None:
                        wait = float(retry_after)
                except (ValueError, TypeError):
                    pass
                sleep(wait)
                continue
        return False, "slack: %s" % result.get("error", "unknown_error")
    return False, "retries_exhausted"


def main(argv=None):
    ap = argparse.ArgumentParser(description="metadata 付きで chat.postMessage を叩く")
    ap.add_argument("--channel", required=True)
    ap.add_argument("--message")
    ap.add_argument("--message-file")
    ap.add_argument("--thread-ts")
    ap.add_argument("--workspace")
    ap.add_argument("--event-id")
    ap.add_argument("--request-id")
    ap.add_argument("--event-type", default=EVENT_TYPE)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.message is not None:
        text = args.message
    elif args.message_file:
        try:
            with open(args.message_file, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            print(json.dumps({"ok": False, "error": "read_message: %s" % e},
                             ensure_ascii=False))
            return 1
    else:
        print(json.dumps({"ok": False, "error": "message or message-file required"},
                         ensure_ascii=False))
        return 1

    try:
        payload = build_payload(args.channel, text, args.thread_ts,
                                args.event_id, args.request_id,
                                args.event_type)
    except ValueError as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, "payload": payload},
                         ensure_ascii=False))
        return 0

    token = resolve_token(args.workspace)
    if not token:
        print(json.dumps({"ok": False, "error": "token_not_found"},
                         ensure_ascii=False))
        return 1

    ok, result = post(payload, token)
    if ok:
        print(json.dumps({"ok": True, "ts": result.get("ts"),
                          "channel": result.get("channel", args.channel)},
                         ensure_ascii=False))
        return 0
    print(json.dumps({"ok": False, "error": result}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    sys.exit(main())
