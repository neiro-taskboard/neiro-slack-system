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
  slack-read-metadata.py --channel C... [--thread-ts TS]
      [--workspace muse-board] --dump [--limit 100]
      # --dump: 印付きの投稿一覧を出す（文面照合のガード用、2026-11-15）。
      # slackcli の読み取りでは metadata が返ってこないので、印の違いが
      # 見えない。--dump は各投稿に印（event_id / request_id、無ければ
      # null）を付けて返す。スレッド指定時は conversations.replies、
      # 未指定時は conversations.history。20件ずつページングして
      # 直近 limit 件を返す（2026-11-19、101件スレッドの約606KB読みが
      # IncompleteRead で切れたため。Yanayana さんの提案）。
      # 文面照合の窓は直近100件、という slack-send-once.sh の割り切りと同じ。

出力: JSON 1行 {"ok": true, "found": true/false, "ts": ...,
                "checked": <見た件数>}。
  失敗時は {"ok": false, "error": ...} を出し、終了コード 1。

注意: スレッド指定時は conversations.replies、未指定時は
  conversations.history を使う。ページングは最大 6 ページまで
  （1ページ20件。101件スレッドの metadata 付き1ページ読み
  （約606KB）が IncompleteRead で切れたため、2026-11-19 に
  小さいページ＋カーソルへ変更。Yanayana さんの提案）。
  スレッドの重複チェックは直近 100 件で足りる、という
  slack-send-once.sh の割り切りと同じ。
"""
import argparse
import http.client
import json
import os
import shutil
import subprocess
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
# metadata 付きの1ページは大きい（101件スレッドで約606KB）ため
# IncompleteRead で途中で切れる。1ページを20件に絞り、カーソルで
# 回す。2026-11-19、Yanayana さんの提案（limit を小さくして
# カーソルで回す）を採用。20件×6ページ＝120件で101件スレッドを覆う。
PAGE_LIMIT = 20
MAX_PAGES = 6


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
    ・None（デフォルト）のいずれか。見つからなければ None。
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


def _get_page_curl(full_url, token, timeout=30):
    """curl で GET し、(status, body_bytes, retry_after) を返す。

    接続できなかったときは (None, None, None)。トークンは argv に
    載せず、0600 の一時設定ファイル経由で渡す（ps 等への露出を避ける）。
    """
    cfg = None
    try:
        import tempfile
        with tempfile.NamedTemporaryFile(
                "w", delete=False, prefix="slack-curl-") as f:
            f.write('header = "Authorization: Bearer %s"\n' % token)
            cfg = f.name
        os.chmod(cfg, 0o600)
        proc = subprocess.run(
            ["curl", "-sS", "--max-time", str(timeout),
             "-w", "\n__STATUS__:%{http_code}",
             "-K", cfg, full_url],
            capture_output=True, timeout=timeout + 5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None, None, None
    finally:
        if cfg:
            try:
                os.unlink(cfg)
            except OSError:
                pass
    out = proc.stdout
    marker = b"\n__STATUS__:"
    if marker not in out:
        return None, None, None
    body, _, status_b = out.rpartition(marker)
    try:
        status = int(status_b.decode("ascii").strip())
    except ValueError:
        return None, None, None
    return status, body, None


def _get_page_urllib(full_url, token, urlopen):
    """urllib で GET し、(status, body_bytes, retry_after) を返す。

    接続できなかったときは (None, None, None)。curl が無いときの
    フォールバック。
    """
    req = urllib.request.Request(
        full_url,
        headers={"Authorization": "Bearer " + token},
        method="GET")
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
            try:
                retry_after = resp.headers.get("Retry-After")
            except AttributeError:
                retry_after = None
            return resp.status, raw, retry_after
    except urllib.error.HTTPError as e:
        try:
            raw = e.read()
        except Exception:
            raw = b""
        try:
            retry_after = e.headers.get("Retry-After")
        except AttributeError:
            retry_after = None
        return e.code, raw, retry_after
    except (urllib.error.URLError, TimeoutError, OSError,
            http.client.IncompleteRead):
        # IncompleteRead は OSError / URLError のサブクラスではない
        # ため、ここに入れないと呼び出し側へ例外が抜ける。2026-11-15：
        # 大きなスレッドの dump（約386KB）が途中で切れて IncompleteRead
        # になり、送信前チェックが fail-closed で返信を止めた。
        return None, None, None


def fetch_page(url, params, token, urlopen=urllib.request.urlopen,
               sleep=time.sleep):
    """1ページ読む。成功時は (True, 応答)、失敗時は (False, 理由)。

    ネットワークエラー・429（ratelimited）・5xx は最大 MAX_ATTEMPTS 回まで
    リトライする（429 は Retry-After を尊重）。Slack の ok:false（チャンネル
    不明など）はリトライしない。

    読み取りは curl を使う（2026-11-19：同じ URL・同じトークンで
    urllib が IncompleteRead / Remote end closed を繰り返す一方、
    curl は 5/5 成功したため。出口プロキシとの相性と思われる）。
    curl が無ければ urllib へフォールバックする。
    """
    query = urllib.parse.urlencode(params)
    full_url = url + "?" + query
    use_curl = shutil.which("curl") is not None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if use_curl:
            status, raw, retry_after = _get_page_curl(full_url, token)
        else:
            status, raw, retry_after = _get_page_urllib(
                full_url, token, urlopen)
        if status is None:
            # 接続できなかった等（IncompleteRead を含む）。
            # GET は冪等なので、他のネットワークエラーと同じくリトライする。
            if attempt < MAX_ATTEMPTS:
                sleep(BACKOFF_SECS[attempt - 1])
                continue
            return False, "network: connection_failed"
        if status == 429 or status >= 500:
            if attempt < MAX_ATTEMPTS:
                wait = BACKOFF_SECS[attempt - 1]
                if retry_after is not None:
                    try:
                        wait = float(retry_after)
                    except (TypeError, ValueError):
                        pass
                sleep(wait)
                continue
            return False, "http_%d" % status
        if status != 200:
            return False, "http_%d" % status
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
                     oldest=None,
                     urlopen=urllib.request.urlopen, sleep=time.sleep):
    """印に一致する投稿を探す。

    見つかれば {"found": True, "ts": ...}、無ければ {"found": False} を返す。
    読めなければ RuntimeError。token を渡さなければ workspace から解決する。
    1ページは PAGE_LIMIT（20件）に絞り、カーソルで最大 MAX_PAGES ページ
    まで回る（2026-11-19、101件スレッドの約606KB読みが IncompleteRead
    で切れたため。Yanayana さんの提案）。
    oldest を渡すと、その時刻より後の投稿だけを読む（イベント駆動の
    返信では、重複はイベントの後でしか起きないため。Yanayana さんの
    --oldest <floor> 付き1ページの割り切りと同じ考え方）。
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
                  "include_all_metadata": "true", "limit": str(PAGE_LIMIT)}
    else:
        url = HISTORY_API
        params = {"channel": channel,
                  "include_all_metadata": "true", "limit": str(PAGE_LIMIT)}
    if oldest:
        params["oldest"] = str(oldest)
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
        if checked >= limit:
            break
        meta = result.get("response_metadata") or {}
        cursor = meta.get("next_cursor") or None
        if not result.get("has_more") or not cursor:
            break
    return {"found": False, "ts": None, "checked": checked}


def dump_messages(channel, thread_ts=None, workspace=None, limit=100,
                  token=None, oldest=None,
                  urlopen=urllib.request.urlopen,
                  sleep=time.sleep):
    """include_all_metadata 付きで読んだ投稿の一覧を返す。

    文面照合のガード用（2026-11-15、muse取締役会 #slack-へようこそ
    1794621025.648949 で約束）。slackcli の conversations read では
    metadata が返ってこないので、印の違いが見えない。こちらは直接 API
    を叩き、投稿ごとに印（event_id / request_id、無ければ None）を
    付けて返す。呼び出し側（slack-send-once.sh のスレッド文面チェック）
    は、現在の送信と違う印の付いた自分の投稿を、文面一致の候補から
    外す。印のない投稿は従来どおり文面で拾う。

    スレッド指定時は conversations.replies、未指定時は
    conversations.history。20件ずつページングして読み、直近 limit 件
    を返す（replies は1ページに親＋直近 PAGE_LIMIT 件を返し、カーソル
    を辿ると古い方へ進む。2026-11-19 実測。2026-11-19、101件スレッド
    の約606KB読みが IncompleteRead で切れたため。Yanayana さんの提案）。
    oldest を渡すと、その時刻より後の投稿だけを読む（イベント駆動の
    返信では、重複はイベントの後でしか起きないため）。
    文面照合の窓は直近100件、という slack-send-once.sh の
    割り切りと同じで、それより古い投稿は文面照合の対象外）。
    読めなければ RuntimeError。
    """
    if token is None:
        token = resolve_token(workspace)
    if not token:
        raise RuntimeError("token_not_found")
    if thread_ts:
        url = REPLIES_API
        params = {"channel": channel, "ts": thread_ts,
                  "include_all_metadata": "true", "limit": str(PAGE_LIMIT)}
    else:
        url = HISTORY_API
        params = {"channel": channel,
                  "include_all_metadata": "true", "limit": str(PAGE_LIMIT)}
    if oldest:
        params["oldest"] = str(oldest)
    all_messages = []
    cursor = None
    for _ in range(MAX_PAGES):
        page_params = dict(params)
        if cursor:
            page_params["cursor"] = cursor
        ok, result = fetch_page(url, page_params, token,
                                urlopen=urlopen, sleep=sleep)
        if not ok:
            raise RuntimeError(result)
        all_messages.extend(result.get("messages", []))
        # 1ページ目が直近分なので、窓が埋まったら古い方は読まない。
        if limit and len(all_messages) >= limit:
            break
        meta = result.get("response_metadata") or {}
        cursor = meta.get("next_cursor") or None
        if not result.get("has_more") or not cursor:
            break
    window = all_messages[:limit] if limit else all_messages
    messages = []
    for m in window:
        mark = message_mark(m)
        messages.append({
            "ts": str(m.get("ts", "")),
            "user": m.get("user"),
            "text": m.get("text") or "",
            "mark": ({"kind": mark[0], "value": mark[1]}
                     if mark else None),
        })
    return {"messages": messages, "checked": len(messages)}


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
    ap.add_argument("--oldest",
                    help="この時刻より後の投稿だけ読む（イベント駆動の返信では"
                         "イベントの ts を渡す。重複はイベントの後でしか"
                         "起きないため）")
    ap.add_argument("--dump", action="store_true",
                    help="印付きの投稿一覧を JSON で出す（文面照合のガード用）")
    args = ap.parse_args(argv)

    try:
        if args.dump:
            result = dump_messages(
                args.channel, args.thread_ts, args.workspace, args.limit,
                oldest=args.oldest)
            print(json.dumps({"ok": True, "messages": result["messages"],
                              "checked": result["checked"]},
                             ensure_ascii=False))
            return 0
        result = find_by_metadata(
            args.channel, args.thread_ts, args.event_id, args.request_id,
            args.self_id, args.workspace, args.limit,
            oldest=args.oldest)
    except (ValueError, RuntimeError) as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1
    print(json.dumps({"ok": True, "found": result["found"],
                      "ts": result["ts"], "checked": result["checked"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
