#!/usr/bin/env python3
"""Periodic reconciliation of ambiguous (arrival-unknown) send records.

Yanayana さんの reconcile_ambiguous.py の取り込み（2026-11-14）。
うちにはこれまで、送信前チェックや到達確認で投稿が見つかったときに
ambiguous 記録へ resolved_by_thread_check の訂正を入れる「発見時の訂正」
（slack-send-once.sh の resolve_ambiguous）しかなかった。その後の送信
チェックが二度と起きない対象は拾われないままになるので、1時間に1回まで
定期的にライブのスレッド／チャンネル履歴と照合し、届いていたことが
分かった ambiguous 記録に後から訂正を入れる。

照合のやり方（Yanayana さんの共有 2026-11-14 に合わせる）:
  - 照合の対象は record_ambiguous に文面（text）が残っている
    ambiguous_post_outcome 記録（2026-11-14 の取り込みで文面を残す
    ようにしたので、それ以降の記録が対象）。
  - 文面のない記録は文面照合では原理的に解決できないので、
    resolved とは別の終端 closed_no_text で閉じる（Yanayana さんの
    案の取り込み、2026-11-14）。元の記録は残し、(channel, thread_ts)
    ごとに closed_no_text エントリを1行追加する。閉じた後に別の
    pre-send チェックで返信が見つかれば resolved_by_thread_check が
    別途つく（終端は未解決のまま放置しないためのもので、記録自体は
    消えない）。closed_no_text より後に出た文面なし記録は、新しい
    グループとして改めて閉じる。
  - 同じ (channel, thread_ts, text) は1つの対象としてまとめる。
    元の記録は履歴として残し、消さない。
  - 一致は完全一致（==、前後の空白を除かない）。Slack から返される
    text が送信した文面そのままのため。
  - 照合対象は自分（ねいろ）の投稿だけ。他者の同一文面は拾わない。
  - 見つかった返信の ts は、対象スレッドの親 ts より後、かつ記録時刻の
    1時間前より後であること。対象より前に出た同一文面は誤照合しない
    （2026-11-14、Yama さんの監査指摘から Yanayana さんが採用した絞り込み）。
  - 解決に使った reply ts は使い回さない。同じ文面の対象が2つあっても
    1つの返信に2重に解決されない（Yanayana さんの残る穴への対応案を
    こちらでは最初から入れる）。既存の resolved_by_thread_check の
    found_ts も使用済みとして扱う。
  - ページングは oldest から順に全件読む。最初に見つかったもの＝最も
    古い該当返信を取る。

頻度（Yanayana さんの形）:
  - state ファイルで「前回から1時間以内ならスキップ」と自己ゲートする。
    ポーリングフック（slack-new-messages.sh）から毎回呼ばれても、走るのは
    1時間に1回まで。手動なら --force で即実行できる。
  - 履歴が読めなかった対象は訂正しない（fail-closed）。次の定期実行で
    拾い直す。

使い方:
  slack-reconcile-ambiguous.py [--ambiguous-file PATH]
      [--last-run-file PATH] [--force] [--dry-run] [--slackcli PATH]
出力: JSON 1行 {"resolved": N, "targets": N, "closed_no_text": N,
  "skipped_no_text": N, "cannot_verify": [...], "gated": false}
終了コード: 0 = 照合できた（解決なし・スキップも含む）。2 = 一部または
  全部の対象の履歴が読めなかった（cannot_verify）。訂正は送らないので
  安全に再実行できる。
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

SLACKCLI = "/home/hatch/workspace/bin/slackcli"
SELF_IDS = {"muse-board": "U0CH1B19D4J", "T0CGV6V7161": "U0CH1B19D4J"}
DEFAULT_SELF_ID = "U0CH32D1P5F"  # Neiro Task Board
WORKSPACES = ("muse-board", "")  # チャンネルIDはワークスペースごとに
# 一意なので、読めた方が正しいワークスペース。muse-board から試す。
HISTORY_ATTEMPTS = 5  # slack-send-once.sh / slack-answered-check.py と同じ回数
BACKOFF_SECS = (1, 2, 4, 8)  # 指数バックオフ（同上）
MAX_PAGES = 20
PAGE_LIMIT = 50
GATE_SECS = 3600  # 1時間に1回まで（Yanayana の slack-reconcile.timer 相当）
GRACE_SECS = 3600  # 記録時刻の1時間前までを照合の下限とする猶予

AMBIGUOUS_FILE = "/home/hatch/hooks/state/slack-ambiguous-posts.jsonl"
LAST_RUN_FILE = "/home/hatch/hooks/state/slack-reconcile-ambiguous-last.json"


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
    import re
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


def parse_record_time(value):
    """record_ambiguous の time（例 2026-11-12T11:30:25+0000）を epoch 秒に。"""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z").timestamp()
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def is_reply_after(msg_ts, lower_ts):
    """返信 ts が下限 ts より後か（数値比較、だめなら文字列比較）。"""
    if msg_ts is None or lower_ts is None:
        return False
    try:
        return float(msg_ts) > float(lower_ts)
    except (TypeError, ValueError):
        return str(msg_ts) > str(lower_ts)


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


def read_all(page_reader, channel, thread_ts, oldest):
    """履歴の全メッセージを古い順に集める（oldest からページング）。"""
    messages = []
    cur = oldest
    for _ in range(MAX_PAGES):
        data = page_reader(channel, thread_ts, cur)
        messages.extend({"ts": m.get("ts"), "user": m.get("user"),
                         "text": m.get("text") or ""}
                        for m in data.get("messages", []))
        if not data.get("has_more"):
            break
        nxt = data.get("next_oldest")
        if not nxt or nxt == cur:
            break
        cur = nxt
    return messages


def load_records(path):
    """ambiguous ファイルを読む。読めなければ空リスト（初回は記録なし）。"""
    records = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return records


def build_targets(records):
    """照合対象を作る。

    (channel, thread_ts, text) ごとに1対象。文面のない記録は飛ばす
    （空白だけの文面も文面なし扱い）。
    戻り値: (targets, used_found_ts)
      targets: [{channel, thread_ts, text, earliest_time}]
      used_found_ts: {(channel, thread_ts): {found_ts, ...}} — 既存の
        resolved_by_thread_check の found_ts は使用済みとして扱う。
    """
    targets = {}
    used = {}
    for rec in records:
        channel = rec.get("channel") or ""
        thread_ts = rec.get("thread_ts") or ""
        outcome = rec.get("outcome")
        if outcome == "resolved_by_thread_check":
            found = rec.get("found_ts")
            if found:
                used.setdefault((channel, thread_ts), set()).add(found)
            continue
        if outcome != "ambiguous_post_outcome":
            continue
        text = rec.get("text") or ""
        if not text.strip():
            # 空白だけの文面も文面なし扱い（Yamaさんの指摘 2026-11-14：
            # " " は not text では弾かれずキーになりうる）。
            continue
        key = (channel, thread_ts, text)
        rec_time = parse_record_time(rec.get("time"))
        if key not in targets:
            targets[key] = {"channel": channel, "thread_ts": thread_ts,
                            "text": text, "earliest_time": rec_time}
        elif rec_time is not None:
            cur = targets[key]["earliest_time"]
            if cur is None or rec_time < cur:
                targets[key]["earliest_time"] = rec_time
    ordered = sorted(targets.values(),
                     key=lambda t: (t["earliest_time"] is None,
                                    t["earliest_time"] or 0))
    return ordered, used


def build_no_text_groups(records):
    """文面のない ambiguous 記録のうち、まだ閉じていないグループを返す。

    (channel, thread_ts) ごとに数え、closed_no_text エントリが出たら
    それより前の文面なし記録は終端済みとして数え直す（エントリより
    後に出た文面なし記録は新しいグループになる）。
    戻り値: [{channel, thread_ts, count, earliest_time}]（出現順）。
    """
    groups = {}
    order = []
    for rec in records:
        channel = rec.get("channel") or ""
        thread_ts = rec.get("thread_ts") or ""
        key = (channel, thread_ts)
        outcome = rec.get("outcome")
        if outcome == "closed_no_text":
            if key in groups:
                groups[key]["count"] = 0
                groups[key]["earliest_time"] = None
            continue
        if outcome != "ambiguous_post_outcome":
            continue
        if (rec.get("text") or "").strip():
            continue
        if key not in groups:
            groups[key] = {"channel": channel, "thread_ts": thread_ts,
                           "count": 0, "earliest_time": None}
            order.append(key)
        g = groups[key]
        g["count"] += 1
        t = parse_record_time(rec.get("time"))
        if t is not None and (g["earliest_time"] is None
                              or t < g["earliest_time"]):
            g["earliest_time"] = t
    return [groups[k] for k in order if groups[k]["count"] > 0]


def reconcile(records, history_reader):
    """照合の本体。history_reader はテストで差し替えられる。

    history_reader(channel, thread_ts, oldest) -> (messages, self_id)
    読めなければ例外を投げる。
    戻り値: {"resolved": [...], "closed_no_text": [...],
             "skipped_no_text": N,
             "cannot_verify": [(channel, thread_ts, reason)], "targets": N}
    resolved の各要素は {"channel", "thread_ts", "text", "found_ts"}。
    closed_no_text の各要素は build_no_text_groups のグループで、
    今回 closed_no_text として閉じる対象。skipped_no_text は文面なし
    記録のうち終端済み（過去に閉じた）ものの件数。
    """
    targets, used = build_targets(records)
    no_text_groups = build_no_text_groups(records)
    total_no_text = sum(
        1 for r in records
        if r.get("outcome") == "ambiguous_post_outcome"
        and not (r.get("text") or "").strip())
    skipped_no_text = total_no_text - sum(g["count"] for g in no_text_groups)
    resolved = []
    cannot_verify = []
    for target in targets:
        channel = target["channel"]
        thread_ts = target["thread_ts"]
        text = target["text"]
        rec_time = target["earliest_time"]
        # 照合の下限: スレッドなら親 ts、かつ記録時刻の1時間前より後。
        # チャンネル投稿なら記録時刻の1時間前より後（親がないため）。
        lower = None
        if thread_ts:
            lower = thread_ts
        if rec_time is not None:
            grace = str(rec_time - GRACE_SECS)
            if lower is None:
                lower = grace
            else:
                try:
                    lower = grace if float(grace) > float(lower) else lower
                except (TypeError, ValueError):
                    pass
        oldest = thread_ts if thread_ts else (
            str(rec_time - GRACE_SECS) if rec_time is not None else "0")
        try:
            messages, self_id = history_reader(channel, thread_ts, oldest)
        except Exception as exc:  # noqa: BLE001 - 読めない対象は飛ばして次へ
            cannot_verify.append((channel, thread_ts, str(exc)[:200]))
            continue
        spent = used.setdefault((channel, thread_ts), set())
        candidates = [m for m in messages
                      if m.get("user") == self_id
                      and (m.get("text") or "") == text
                      and m.get("ts") not in spent
                      and (lower is None
                           or is_reply_after(m.get("ts"), lower))]
        candidates.sort(key=lambda m: (m.get("ts") is None, m.get("ts") or ""))
        if not candidates:
            continue
        found_ts = candidates[0].get("ts")
        spent.add(found_ts)
        resolved.append({"channel": channel, "thread_ts": thread_ts,
                         "text": text, "found_ts": found_ts})
    return {"resolved": resolved, "closed_no_text": no_text_groups,
            "skipped_no_text": skipped_no_text,
            "cannot_verify": cannot_verify, "targets": len(targets)}


def append_resolved(path, resolved, now=None):
    """訂正エントリを ambiguous ファイルへ追加する（元の記録は残す）。"""
    if not resolved:
        return
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S%z")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in resolved:
            rec = {"time": stamp, "channel": r["channel"],
                   "thread_ts": r["thread_ts"],
                   "outcome": "resolved_by_thread_check",
                   "found_ts": r["found_ts"], "text": r["text"]}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def append_closed_no_text(path, groups, now=None):
    """文面のない記録のグループを closed_no_text として終端する。

    元の記録は残し、終端エントリを1グループ1行で追加する。
    """
    if not groups:
        return
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S%z")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for g in groups:
            rec = {"time": stamp, "channel": g["channel"],
                   "thread_ts": g["thread_ts"],
                   "outcome": "closed_no_text",
                   "reason": "no text recorded; cannot verify by text "
                             "match (closed_no_text)",
                   "count": g["count"]}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def read_last_run(path):
    """前回の実行時刻（epoch 秒）。なければ None。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return float(data.get("last_run"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def write_last_run(path, epoch):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"last_run": epoch}, f)
    os.replace(tmp, path)


def main(argv):
    opts = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--ambiguous-file", "--last-run-file", "--slackcli") \
                and i + 1 < len(argv):
            opts[a[2:]] = argv[i + 1]
            i += 2
        elif a in ("--force", "--dry-run"):
            opts[a[2:]] = True
            i += 1
        else:
            print(f"unknown argument: {a}", file=sys.stderr)
            return 2
    ambiguous_file = opts.get("ambiguous-file") or AMBIGUOUS_FILE
    last_run_file = opts.get("last-run-file") or LAST_RUN_FILE
    slackcli = opts.get("slackcli") or SLACKCLI
    force = bool(opts.get("force"))
    dry_run = bool(opts.get("dry-run"))

    now = time.time()
    if not force:
        last = read_last_run(last_run_file)
        if last is not None and now - last < GATE_SECS:
            print(json.dumps({"resolved": 0, "targets": 0,
                              "closed_no_text": 0,
                              "skipped_no_text": 0, "cannot_verify": [],
                              "gated": True,
                              "last_run": last}, ensure_ascii=False))
            return 0

    records = load_records(ambiguous_file)

    def history_reader(channel, thread_ts, oldest):
        last_exc = None
        for workspace in WORKSPACES:
            try:
                def page_reader(ch, th, cur, ws=workspace):
                    return read_page_retry(slackcli, ch, cur, th, ws)
                messages = read_all(page_reader, channel, thread_ts, oldest)
                return messages, self_user_id(workspace, slackcli)
            except Exception as exc:  # noqa: BLE001 - 次のワークスペースを試す
                last_exc = exc
        raise RuntimeError(f"history unreadable in all workspaces: {last_exc}")

    result = reconcile(records, history_reader)
    if not dry_run:
        append_resolved(ambiguous_file, result["resolved"])
        append_closed_no_text(ambiguous_file, result["closed_no_text"])
        write_last_run(last_run_file, now)
    out = {"resolved": len(result["resolved"]),
           "resolved_detail": result["resolved"],
           "targets": result["targets"],
           "closed_no_text": sum(g["count"] for g in result["closed_no_text"]),
           "closed_no_text_detail": result["closed_no_text"],
           "skipped_no_text": result["skipped_no_text"],
           "cannot_verify": [{"channel": c, "thread_ts": t, "reason": r}
                             for c, t, r in result["cannot_verify"]],
           "gated": False, "dry_run": dry_run}
    print(json.dumps(out, ensure_ascii=False))
    return 0 if not result["cannot_verify"] else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
