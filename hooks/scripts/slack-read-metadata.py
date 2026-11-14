#!/usr/bin/env python3
"""conversations.history / conversations.replies を直接叩いて
metadata 付きの投稿を探す。

Yama さんの metadata 照合の設計（2026-11-14、muse取締役会 #slack-へようこそ）
の読み取り側。slackcli の conversations read は include_all_metadata を
付けないので、metadata を載せた投稿の印は返ってこない。そのため
conversations.history / conversations.replies を直接叩き、
include_all_metadata=true を付けて読む。

照合の第1手段（Yama さん・Yanayana さんと三者で揃えた形）:
  metadata.event_payload.event_id（イベント駆動の返信）
  または metadata.event_payload.request_id（手動投稿）
が一致する投稿を探す。文面照合は第2手段として slack-send-once.sh 側に
残る。metadata が付く前の投稿には文面照合を使う。

うちのイベントIDは「workspace_id:channel:ts」の形（Yama さんの
Slack の event_id（Ev…の形）とは中身が違うが、元のイベントを一意に
指す点では同じ役割、という整理で三者合意済み 2026-11-14）。
手動投稿は event_id の代わりに request_id に UUID を入れる。

トークンは slack-post-metadata.py と同じく slackcli の認証ストア
（~/.config/slackcli/workspaces.json）から読む。トークン自体は
出力・ログに一切出さない。

使い方:
  slack-read-metadata.py --channel C... [--thread-ts TS]
      [--workspace muse-board] (--event-id <ID> | --request-id <UUID>)
      [--self-id U...] [--limit 100]

出力: JSON 1行 {"ok": true, "found": true/false, "ts": ...,
                "checked": <見た件数>}。
  失敗時は {"ok": false, "error": ...} を出し、終了コード 1。

注意: スレッド指定時は conversations.replies、未指定時は
  conversations.history を使う。ページングは最大 3 ページまで
  （スレッドの重複チェックは直近 100 件で足りる、という
  slack-send-once.sh の割り切りと同じ）。
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

WORKSPACES_JSON = "/home/hatch/.config/slackcli/workspaces.json"
HISTORY_API = "https://slack.com/api/conversations.history"
REPLIES_API = "https://slack.com/api/conversations.replies"
EVENT_TYPE = "neiro_reply"
MAX_ATTEMPTS = 3
BACKOFF_SECS = (1, 2)
MAX_PAGES = 3


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


def message_mark(message):
    """投稿の metadata から印（event_id / request_id）を取る。

    event_type が neiro_reply でない印は対象外にする（他者の
    metadata と取り違えないため）。印が無ければ None。
    """
    md = message.get("metadata") or {}
    if md.get("event_type") != EVENT_TYPE:
        return None
    payload = md.get("event_payload") or {}
    if "event_id" in payload:
        return ("event_id", str(payload["event_id"]))
    if "request_id" in payload:
        return ("request_id", str(payload["request_id"]))
    return None


def matches(message, key, value, self_id=None):
    """その投稿が探している印と一致するか。self_id 指定時は自分の投稿だけ。"""
    if self_id is not None and message.get("user") != self_id:
        return False
    mark = message_mark(message)
    return mark is not None and mark == (key, value)


def fetch_page(url, params, token, urlopen=urllib.request.urlopen,
               sleep=time.sleep):
    """1ページ読む。成功時は (True, 応答)、失敗時は (False, 理由)。

    ネットワークエラー・429（ratelimited）・5xx は最大 MAX_ATTEMPTS 回まで
    リトライする（429 は Retry-After を尊重）。Slack の ok:false（チャンネル
    不明など）はリトライしない。
    """
    query = urllib.parse.urlencode(params)
    full_url = url + "?" + query
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(
            full_url,
            headers={"Authorization": "Bearer " + token},
            method="GET")
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


def find_by_metadata(channel, thread_ts=None, event_id=None, request_id=None,
                     self_id=None, workspace=None, limit=100, token=None,
                     urlopen=urllib.request.urlopen, sleep=time.sleep):
    """印に一致する投稿を探す。

    見つかれば {"found": True, "ts": ...}、無ければ {"found": False} を返す。
    読めなければ RuntimeError。token を渡さなければ workspace から解決する。
    """
    if bool(event_id) == bool(request_id):
        raise ValueError("event_id と request_id はどちらか一方だけ指定する")
    if token is None:
        token = resolve_token(workspace)
    if not token:
        raise RuntimeError("token_not_found")
    key = "event_id" if event_id is not None else "request_id"
    value = event_id if event_id is not None else request_id

    if thread_ts:
        url = REPLIES_API
        params = {"channel": channel, "ts": thread_ts,
                  "include_all_metadata": "true", "limit": str(limit)}
    else:
        url = HISTORY_API
        params = {"channel": channel,
                  "include_all_metadata": "true", "limit": str(limit)}
    checked = 0
    cursor = None
    for _ in range(MAX_PAGES):
        page_params = dict(params)
        if cursor:
            page_params["cursor"] = cursor
        ok, result = fetch_page(url, page_params, token,
                                urlopen=urlopen, sleep=sleep)
        if not ok:
            raise RuntimeError(result)
        messages = result.get("messages", [])
        checked += len(messages)
        for m in messages:
            if matches(m, key, value, self_id):
                return {"found": True, "ts": str(m.get("ts", "")),
                        "checked": checked}
        meta = result.get("response_metadata") or {}
        cursor = meta.get("next_cursor") or None
        if not result.get("has_more") or not cursor:
            break
    return {"found": False, "ts": None, "checked": checked}


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="metadata の印で投稿を探す（include_all_metadata 付きで読む）")
    ap.add_argument("--channel", required=True)
    ap.add_argument("--thread-ts")
    ap.add_argument("--workspace")
    ap.add_argument("--event-id")
    ap.add_argument("--request-id")
    ap.add_argument("--self-id")
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args(argv)

    try:
        result = find_by_metadata(
            args.channel, args.thread_ts, args.event_id, args.request_id,
            args.self_id, args.workspace, args.limit)
    except (ValueError, RuntimeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "found": result["found"],
                      "ts": result["ts"], "checked": result["checked"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
