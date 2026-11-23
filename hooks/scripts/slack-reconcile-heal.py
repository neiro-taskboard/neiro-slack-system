#!/usr/bin/env python3
"""Periodic heal of sent-but-still-pending events (heal-replied).

Yanayana さんの滞留根本対策② heal-replied の取り込み（2026-11-23）。
「送ったのに pending」の隙間——送信は成功したが、その後の mark done
が別ステップのため、送信済みのイベントが pending/failed のまま残り、
冗長な起こし直し（再 wake → 送信前チェックで送らず done、の1往復）
が発生する——を、送信台帳との照合で回復する。

照合のやり方:
  - 対象は状態ファイル（slack-events.jsonl）の status が pending /
    failed のイベント。processing はポーラーの stale reclaim が、
    waiting は DLQ 再送が、dead は終端としてそれぞれ担当するので
    対象外。done は既に解決済み。
  - 送信台帳（slack-send-log.jsonl）には送信時に event_id が残る
    （slack-send-once.sh の record_send）。対象の event_id が台帳に
    あれば、そのイベントへの返信は送り済みとみなし、done に回復する。
    回復は slack-event-state.sh done 経由で、done は最終状態ガード
    済み（④）なので、回復後に遅れて届いた failed で掘り返される
    心配はない。resolution には heal-replied である旨を残す。
  - 台帳に event_id の記録がない対象は触らない。台帳に無いが印つきで
    着地した返信の検出（Yanayana さんの③ state-heal 相当）は、うちは
    送信と台帳記録が同一 flock 内のため起きる窓がごく狭く、ambiguous
    経路（slack-reconcile-ambiguous.py の文面照合）でも拾われるので、
    今は入れない。②の運用で「台帳なし着地」が実際に起きるか測って
    から判断する（2026-11-23、#slack-へようこそ での共有通り）。

頻度（slack-reconcile-ambiguous.py と同じ形）:
  - state ファイルで「前回から1時間以内ならスキップ」と自己ゲート
    する。ポーリングフック（slack-new-messages.sh）から毎回呼ばれても、
    走るのは1時間に1回まで。手動なら --force で即実行できる。
  - 台帳が読めなかった対象は回復しない（fail-closed）。次の定期実行
    で拾い直す。台帳ファイル自体が無い場合は記録なしとして扱う
    （回復なし、終了コード 0）。

flock（slack-reconcile-ambiguous.py と同じ形）:
  - 照合スクリプト自体が slack-reconcile-heal.lock に LOCK_EX|LOCK_NB
    の flock を取り、スイープ全体を直列化する。ロックが取れなければ
    gated として抜ける（終了コード 0）。ゲートの回は実行ログに
    {"event": "gated", "reason": ...} の1行だけ残す。

実行ログ（slack-reconcile-ambiguous.py と同じ形）:
  - 実行のたびに、開始1行と終了1行（終了コード付き）を
    slack-reconcile-heal-runs.jsonl へ追記する。
  - その回のサマリ（標準出力と同じ JSON）は
    slack-reconcile-heal-last-run.json に上書き保存する。
  - ログ書き込みの失敗で本体が止まらないよう、失敗しても飲み込む。

使い方:
  slack-reconcile-heal.py [--state-file PATH] [--ledger-file PATH]
      [--state-lock-file PATH] [--last-run-file PATH]
      [--runs-file PATH] [--last-summary-file PATH] [--lock-file PATH]
      [--event-state PATH] [--force] [--dry-run]
出力: JSON 1行 {"healed": N, "healed_detail": [...], "targets": N,
  "cannot_verify": [...], "gated": false}
終了コード: 0 = 照合できた（回復なしも含む）。2 = 一部または全部の
  対象の照合ができなかった（cannot_verify）。回復は送らないので
  安全に再実行できる。
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX 環境では常に使える
    fcntl = None

EVENT_STATE = "/home/hatch/hooks/scripts/slack-event-state.sh"
GATE_SECS = 3600  # 1時間に1回まで（slack-reconcile-ambiguous.py と同じ）

STATE_FILE = "/home/hatch/hooks/state/slack-events.jsonl"
STATE_LOCK_FILE = "/home/hatch/hooks/state/slack-events.lock"
LEDGER_FILE = "/home/hatch/hooks/state/slack-send-log.jsonl"
LAST_RUN_FILE = "/home/hatch/hooks/state/slack-reconcile-heal-last.json"
RUNS_FILE = "/home/hatch/hooks/state/slack-reconcile-heal-runs.jsonl"
LAST_SUMMARY_FILE = "/home/hatch/hooks/state/slack-reconcile-heal-last-run.json"
LOCK_FILE = "/home/hatch/hooks/state/slack-reconcile-heal.lock"

HEAL_STATUSES = ("pending", "failed")
RESOLUTION = "heal-replied: send ledger has a record for this event_id"


class LockBusy(Exception):
    """別の照合プロセスがロックを持っている（2つ目は gated で抜ける）。"""


_LOCK_FP = None  # プロセス終了まで保持し、flock を維持する


def acquire_lock(path):
    """照合スクリプト専用の flock を取る。取れなければ LockBusy。

    LOCK_EX|LOCK_NB でスイープ全体を直列化する
    （slack-reconcile-ambiguous.py と同じ形）。ファイルはプロセス終了
    まで開いたままにし、終了時に OS がロックを解放する。fcntl が無い
    環境ではロックを取れないので OSError とする（fail-closed：直列化
    できないまま走らせない）。
    """
    global _LOCK_FP
    if fcntl is None:
        raise OSError("fcntl is not available; cannot serialize runs")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fp = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fp.close()
        raise LockBusy(path)
    _LOCK_FP = fp
    return fp


def release_lock():
    """flock を解放する。main() の終了時に呼ぶ。

    プロセスがそのまま終了する CLI 利用では、解放しなくても OS が
    終了時に解放する（slack-reconcile-ambiguous.py と同じ結果）。
    ただし main() を同一プロセスから繰り返し呼ぶテストでは、解放
    しないと2回目が自分のロックに当たって lock_busy になるため、
    ここで明示的に閉じる。ゲートの読み書きは main() の中で終わって
    いるので、解放してもレースは起きない。
    """
    global _LOCK_FP
    if _LOCK_FP is not None:
        try:
            _LOCK_FP.close()
        except OSError:
            pass
        _LOCK_FP = None


def utc_stamp(epoch=None):
    """UTC の ISO 時刻（例 2026-11-14T07:30:00+0000）。"""
    return datetime.fromtimestamp(
        time.time() if epoch is None else epoch,
        timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")


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


def append_run_line(path, event, fields):
    """実行ログへ開始・終了の1行を追記する。

    失敗しても飲み込んで本体を止めない（slack-reconcile-ambiguous.py
    と同じ形）。
    """
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        rec = {"event": event, "time": utc_stamp()}
        rec.update(fields)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def write_last_summary(path, summary):
    """その回のサマリを上書き保存する。失敗しても飲み込む。"""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def load_targets(state_file):
    """回復対象（pending / failed）の event_id 一覧。読めなければ例外。

    壊れた JSON 行は飛ばす（slack-poll.py と同じ扱い）。
    """
    targets = []
    with open(state_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("status") in HEAL_STATUSES and rec.get("event_id"):
                targets.append(str(rec["event_id"]))
    return targets


def load_ledger_event_ids(ledger_file):
    """送信台帳にある event_id の集合。ファイルが無ければ空集合。

    壊れた JSON 行は飛ばす。ファイルが開けない（権限など）場合は
    OSError を上げ、呼び出し側が fail-closed で止める。
    """
    ids = set()
    try:
        f = open(ledger_file, encoding="utf-8")
    except FileNotFoundError:
        return ids
    with f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event_id"):
                ids.add(str(rec["event_id"]))
    return ids


def heal_one(event_state, state_file, state_lock_file, event_id):
    """1件を slack-event-state.sh done で回復する。失敗時は例外。"""
    env = dict(os.environ)
    env["SLACK_EVENT_STATE_FILE"] = state_file
    env["SLACK_EVENT_STATE_LOCK_FILE"] = state_lock_file
    proc = subprocess.run(
        [event_state, event_id, "done", RESOLUTION],
        capture_output=True, text=True, timeout=60, env=env)
    if proc.returncode != 0:
        raise RuntimeError(
            "event-state failed for %s: %s" % (
                event_id, (proc.stderr or proc.stdout or "").strip()[:200]))


def heal(state_file, ledger_file, state_lock_file, event_state,
         dry_run=False):
    """台帳照合で回復する。戻り値は (healed, cannot_verify)。

    healed は回復した event_id の一覧、cannot_verify は
    (event_id, reason) の一覧。台帳が読めなければ全体を
    cannot_verify として RuntimeError を上げる（fail-closed）。
    """
    targets = load_targets(state_file)
    try:
        ledger_ids = load_ledger_event_ids(ledger_file)
    except OSError as exc:
        raise RuntimeError("ledger unreadable: %s" % exc)
    healed = []
    cannot_verify = []
    for event_id in targets:
        if event_id not in ledger_ids:
            continue
        if dry_run:
            healed.append(event_id)
            continue
        try:
            heal_one(event_state, state_file, state_lock_file, event_id)
        except Exception as exc:  # noqa: BLE001 - 他の対象は続ける
            cannot_verify.append((event_id, str(exc)[:200]))
            continue
        healed.append(event_id)
    return healed, cannot_verify, len(targets)


def main(argv):
    opts = {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--state-file", "--ledger-file", "--state-lock-file",
                 "--last-run-file", "--runs-file", "--last-summary-file",
                 "--lock-file", "--event-state") and i + 1 < len(argv):
            opts[a[2:]] = argv[i + 1]
            i += 2
        elif a in ("--force", "--dry-run"):
            opts[a[2:]] = True
            i += 1
        else:
            print(f"unknown argument: {a}", file=sys.stderr)
            return 2
    state_file = opts.get("state-file") or STATE_FILE
    ledger_file = opts.get("ledger-file") or LEDGER_FILE
    state_lock_file = opts.get("state-lock-file") or STATE_LOCK_FILE
    last_run_file = opts.get("last-run-file") or LAST_RUN_FILE
    runs_file = opts.get("runs-file") or RUNS_FILE
    last_summary_file = opts.get("last-summary-file") or LAST_SUMMARY_FILE
    lock_file = opts.get("lock-file") or LOCK_FILE
    event_state = opts.get("event-state") or EVENT_STATE
    force = bool(opts.get("force"))
    dry_run = bool(opts.get("dry-run"))

    # スクリプト側の flock を先に取る。ゲートの読み書きもロックの中に
    # 入るので、手動実行とポール経由の実行のレースを防ぐ。取れなければ
    # gated として抜ける（slack-reconcile-ambiguous.py と同じ形）。
    try:
        acquire_lock(lock_file)
    except LockBusy:
        append_run_line(runs_file, "gated",
                        {"reason": "lock_busy", "ts": time.time()})
        print(json.dumps({"healed": 0, "healed_detail": [], "targets": 0,
                          "cannot_verify": [], "gated": True,
                          "reason": "lock_busy"}, ensure_ascii=False))
        return 0
    except OSError as exc:
        print(f"cannot acquire reconcile lock: {exc}", file=sys.stderr)
        return 2

    try:
        return _run(argv, opts, state_file, ledger_file, state_lock_file,
                    last_run_file, runs_file, last_summary_file,
                    lock_file, event_state, force, dry_run)
    finally:
        release_lock()


def _run(argv, opts, state_file, ledger_file, state_lock_file,
         last_run_file, runs_file, last_summary_file,
         lock_file, event_state, force, dry_run):
    now = time.time()
    if not force:
        last = read_last_run(last_run_file)
        if last is not None and now - last < GATE_SECS:
            append_run_line(runs_file, "gated",
                            {"reason": "interval_gate", "ts": now,
                             "last_run": last})
            print(json.dumps({"healed": 0, "healed_detail": [], "targets": 0,
                              "cannot_verify": [], "gated": True,
                              "last_run": last}, ensure_ascii=False))
            return 0

    append_run_line(runs_file, "start", {"ts": now, "dry_run": dry_run})
    try:
        healed, cannot_verify, targets = heal(
            state_file, ledger_file, state_lock_file, event_state, dry_run)
        if not dry_run:
            write_last_run(last_run_file, now)
    except Exception as exc:  # noqa: BLE001 - 予期せぬ失敗も終了行に残す
        print(f"reconcile-heal failed: {exc}", file=sys.stderr)
        append_run_line(runs_file, "end",
                        {"ts": time.time(), "ok": False, "exit": 2,
                         "error": str(exc)[:200], "dry_run": dry_run})
        return 2
    out = {"healed": len(healed), "healed_detail": healed,
           "targets": targets,
           "cannot_verify": [{"event_id": e, "reason": r}
                             for e, r in cannot_verify],
           "gated": False, "dry_run": dry_run}
    exit_code = 0 if not cannot_verify else 2
    append_run_line(runs_file, "end",
                    {"ts": time.time(), "ok": exit_code == 0,
                     "exit": exit_code, "healed": out["healed"],
                     "targets": targets,
                     "cannot_verify": len(cannot_verify),
                     "dry_run": dry_run})
    write_last_summary(last_summary_file, out)
    print(json.dumps(out, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
