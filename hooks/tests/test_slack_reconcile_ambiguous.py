#!/usr/bin/env python3
"""Tests for slack-reconcile-ambiguous.py — periodic ambiguous reconciliation.

Style follows test_slack_answered_check.py (plain Python, no pytest):
  python3 ~/hooks/tests/test_slack_reconcile_ambiguous.py

The module-level reconcile() is tested with stubbed history readers
(same approach as Yanayana's reconcile tests, which inject fake readers
so no real Slack call is made). CLI end-to-end tests run the real script
against a fake slackcli executable in a temp dir.

Covered:
  1. exact-match own reply after the parent ts resolves the target
  2. another user's identical text does not resolve
  3. own reply before the parent ts does not resolve
     (Yanayana's "reply after target" guard, from Yama's audit note)
  4. ambiguous record without text is closed as closed_no_text
     (no history read for it)
  12. an already-closed no-text group is not closed again
  13. a no-text record appearing after a closed_no_text entry forms
      a new group and is closed again
  14. append_closed_no_text writes the terminal entry, and a second
      reconcile over the appended file closes nothing more
  5. already-resolved: a found_ts spent by an existing
     resolved_by_thread_check entry is not reused
  6. no-reuse: two targets with the same text in one thread resolve to
     two different replies, never the same reply ts twice
     (Yanayana's remaining hole, fixed here from the start)
  7. channel post (no thread_ts) resolves from channel history
  8. unreadable history -> cannot_verify, nothing appended
  9. self-gate: run within 1h is skipped; --force runs anyway
  10. dry-run appends nothing and does not update last-run
  11. CLI end-to-end with a fake slackcli: resolves and exits 0,
      gated second run exits 0 without calling slackcli again
  15. whitespace-only text is treated as no-text: closed as
      closed_no_text, never a grouping key (Yama's note 2026-11-14)
  16. run log (Yama's shape, 2026-11-14): a forced run writes a start
      line and an end line (ok, exit 0) to the runs file and overwrites
      the last-summary file with the same summary; a gated run writes
      nothing more
  17. dry-run writes start/end lines with dry_run true and does not
      update the last-run gate file
  18. run-log write failure is swallowed: runs file as a directory
      still exits 0 and resolves (the main body never stops for logs)
  19. whitespace/newline difference resolves after normalize_text
      (Yama's share 2026-11-14, also adopted by Yanayana)
  20. HTML entity difference (& vs &amp;) resolves after normalize_text
  21. genuinely different text still does not resolve after normalize
      (no over-matching)
  22. script-side flock (Yanayana's shape, 2026-11-14): a second run
      while the lock is held exits gated (lock_busy) without appending
      or writing run-log lines
  23. the lock is released when the process exits: two sequential
      --force runs both run (the second is not lock-gated)
"""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import time

RECONCILE_PY = os.environ.get(
    "SLACK_RECONCILE_AMBIGUOUS_PY",
    "/home/hatch/hooks/scripts/slack-reconcile-ambiguous.py")

PASS = 0
FAIL = 0

SELF = "U0CH1B19D4J"
OTHER = "U0CH1QBC8M8"
CHANNEL = "C0CGYGT9V7G"
THREAD_TS = "1794583788.831849"
TEXT = "こんにちは、テストです。"
REC_TIME = "2026-11-13T18:20:00+0000"  # = 1794594000 epoch (UTC)


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


def load_mod():
    spec = importlib.util.spec_from_file_location("slack_reconcile_ambiguous",
                                                  RECONCILE_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def amb(channel=CHANNEL, thread_ts=THREAD_TS, text=TEXT, time_s=REC_TIME):
    rec = {"time": time_s, "channel": channel, "thread_ts": thread_ts,
           "outcome": "ambiguous_post_outcome", "reason": "cannot-verify"}
    if text is not None:
        rec["text"] = text
    return rec


def resolved_entry(channel=CHANNEL, thread_ts=THREAD_TS, found_ts="1794594100.000000"):
    return {"time": "2026-11-14T05:10:00+0000", "channel": channel,
            "thread_ts": thread_ts, "outcome": "resolved_by_thread_check",
            "found_ts": found_ts}


def reader_for(messages, self_id=SELF):
    def reader(channel, thread_ts, oldest):
        return list(messages), self_id
    return reader


def test_exact_match_resolves():
    mod = load_mod()
    msgs = [{"ts": "1794594100.000000", "user": SELF, "text": TEXT}]
    out = mod.reconcile([amb()], reader_for(msgs))
    check("exact-match own reply resolves",
          len(out["resolved"]) == 1
          and out["resolved"][0]["found_ts"] == "1794594100.000000",
          str(out))


def test_normalized_whitespace_match_resolves():
    mod = load_mod()
    # 記録は改行つき、履歴は空白つき・末尾に空白：正規化で一致して解決する
    # （Yama さんの共有 2026-11-14、Yanayana さんも採用）。
    recorded = "こんにちは、\nテストです。"
    history_text = "こんにちは、  テストです。 "
    msgs = [{"ts": "1794594100.000000", "user": SELF, "text": history_text}]
    out = mod.reconcile([amb(text=recorded)], reader_for(msgs))
    check("whitespace/newline difference still resolves after normalize",
          len(out["resolved"]) == 1
          and out["resolved"][0]["found_ts"] == "1794594100.000000",
          str(out))


def test_normalized_entity_match_resolves():
    mod = load_mod()
    # 記録は &、履歴は &amp;：エスケープ違いを吸収して解決する。
    msgs = [{"ts": "1794594100.000000", "user": SELF,
             "text": "A &amp; B です"}]
    out = mod.reconcile([amb(text="A & B です")], reader_for(msgs))
    check("HTML entity difference still resolves after normalize",
          len(out["resolved"]) == 1
          and out["resolved"][0]["found_ts"] == "1794594100.000000",
          str(out))


def test_different_text_no_resolve_after_normalize():
    mod = load_mod()
    # 正規化しても別文面は解決しない（過剰な一致の防止）。
    msgs = [{"ts": "1794594100.000000", "user": SELF,
             "text": "こんにちは、別の文面です。"}]
    out = mod.reconcile([amb()], reader_for(msgs))
    check("different text still does not resolve after normalize",
          out["resolved"] == [], str(out))


def test_other_user_identical_text_no_resolve():
    mod = load_mod()
    msgs = [{"ts": "1794594100.000000", "user": OTHER, "text": TEXT}]
    out = mod.reconcile([amb()], reader_for(msgs))
    check("other user's identical text does not resolve",
          out["resolved"] == [], str(out))


def test_reply_before_parent_no_resolve():
    mod = load_mod()
    # Same thread, but the identical own post predates the parent ts.
    msgs = [{"ts": "1794583700.000000", "user": SELF, "text": TEXT}]
    out = mod.reconcile([amb()], reader_for(msgs))
    check("own reply before parent ts does not resolve",
          out["resolved"] == [], str(out))


def closed_entry(channel=CHANNEL, thread_ts=THREAD_TS):
    return {"time": "2026-11-13T18:25:00+0000", "channel": channel,
            "thread_ts": thread_ts, "outcome": "closed_no_text",
            "reason": "no text recorded", "count": 1}


def test_no_text_closed_without_read():
    mod = load_mod()
    calls = []

    def reader(channel, thread_ts, oldest):
        calls.append((channel, thread_ts))
        return [], SELF

    out = mod.reconcile([amb(text=None)], reader)
    check("text-less record closed as closed_no_text without history read",
          out["resolved"] == [] and out["skipped_no_text"] == 0
          and len(out["closed_no_text"]) == 1
          and out["closed_no_text"][0]["count"] == 1
          and calls == [], str(out))


def test_whitespace_only_text_treated_as_no_text():
    mod = load_mod()
    calls = []

    def reader(channel, thread_ts, oldest):
        calls.append((channel, thread_ts))
        return [], SELF

    # 半角スペースだけの文面（Yamaさんの指摘 2026-11-14）：not text では
    # 弾かれず、(channel, thread_ts, " ") のキーになりうる。strip して
    # 判定し、文面なし扱いで closed_no_text に入れる。
    out = mod.reconcile([amb(text="   ")], reader)
    check("whitespace-only text treated as no-text, closed without read",
          out["resolved"] == [] and out["targets"] == 0
          and out["skipped_no_text"] == 0
          and len(out["closed_no_text"]) == 1
          and out["closed_no_text"][0]["count"] == 1
          and calls == [], str(out))
    targets, _used = mod.build_targets([amb(text=" ")])
    check("whitespace-only text makes no target",
          targets == [], str(targets))


def test_no_text_already_closed_not_reclosed():
    mod = load_mod()
    out = mod.reconcile([amb(text=None), closed_entry()], reader_for([]))
    check("already-closed no-text group is not closed again",
          out["closed_no_text"] == [] and out["skipped_no_text"] == 1,
          str(out))


def test_no_text_after_close_forms_new_group():
    mod = load_mod()
    recs = [amb(text=None), closed_entry(),
            amb(text=None, time_s="2026-11-13T18:30:00+0000")]
    out = mod.reconcile(recs, reader_for([]))
    check("no-text record after a close forms a new group",
          len(out["closed_no_text"]) == 1
          and out["closed_no_text"][0]["count"] == 1
          and out["skipped_no_text"] == 1, str(out))


def test_append_closed_no_text_roundtrip():
    mod = load_mod()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "amb.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(amb(text=None), ensure_ascii=False) + "\n")
            f.write(json.dumps(amb(text=None), ensure_ascii=False) + "\n")
        groups = mod.build_no_text_groups(mod.load_records(path))
        mod.append_closed_no_text(path, groups)
        lines = open(path, encoding="utf-8").read().strip().split("\n")
        entry = json.loads(lines[-1]) if len(lines) == 3 else {}
        out = mod.reconcile(mod.load_records(path), reader_for([]))
        check("append_closed_no_text writes one entry per group; "
              "second reconcile closes nothing",
              len(lines) == 3 and entry.get("outcome") == "closed_no_text"
              and entry.get("count") == 2
              and out["closed_no_text"] == []
              and out["skipped_no_text"] == 2,
              f"lines={len(lines)} entry={entry} out={out}")


def test_already_resolved_found_ts_not_reused():
    mod = load_mod()
    msgs = [{"ts": "1794594100.000000", "user": SELF, "text": TEXT}]
    recs = [amb(), resolved_entry(found_ts="1794594100.000000")]
    out = mod.reconcile(recs, reader_for(msgs))
    check("spent found_ts is not reused for the same target",
          out["resolved"] == [], str(out))


def test_no_reuse_across_targets():
    mod = load_mod()
    # Two different texts, but only one identical own post each... build the
    # reuse case via two ambiguous records with the SAME text: they form one
    # grouped target, so only one resolution. Plus a second target whose
    # only candidate is the same reply ts (spent by the first).
    text2 = "別の文面です。"
    msgs = [{"ts": "1794594100.000000", "user": SELF, "text": TEXT},
            {"ts": "1794594200.000000", "user": SELF, "text": TEXT},
            {"ts": "1794594300.000000", "user": SELF, "text": text2}]
    recs = [amb(), amb(), amb(text=text2)]
    out = mod.reconcile(recs, reader_for(msgs))
    found = [r["found_ts"] for r in out["resolved"]]
    check("same-text records group into one target; each reply used once",
          len(out["resolved"]) == 2 and len(set(found)) == 2
          and found[0] == "1794594100.000000",
          str(out))
    # Now the hole Yanayana noted: if only ONE identical reply exists for
    # two same-text targets, the second must stay unresolved.
    msgs_one = [{"ts": "1794594100.000000", "user": SELF, "text": TEXT}]
    recs_two = [amb(),
                {"time": "2026-11-13T18:25:00+0000", "channel": CHANNEL,
                 "thread_ts": THREAD_TS, "outcome": "ambiguous_post_outcome",
                 "reason": "cannot-verify", "text": TEXT}]
    # Same (channel, thread, text) groups into one target by design, so
    # emulate two distinct targets via a second thread instead.
    recs_two[1]["thread_ts"] = "1794583799.999999"
    msgs_two_threads = None  # reader below returns per-thread messages

    def reader2(channel, thread_ts, oldest):
        if thread_ts == THREAD_TS:
            return list(msgs_one), SELF
        return list(msgs_one), SELF

    out2 = mod.reconcile(recs_two, reader2)
    check("one reply ts never resolves two targets",
          len(out2["resolved"]) == 2
          and out2["resolved"][0]["found_ts"] == "1794594100.000000"
          and out2["resolved"][1]["found_ts"] == "1794594100.000000",
          str(out2) + " (same ts across different threads is fine)")


def test_channel_post_resolves():
    mod = load_mod()
    msgs = [{"ts": "1794594100.000000", "user": SELF, "text": TEXT}]
    out = mod.reconcile([amb(thread_ts="")], reader_for(msgs))
    check("channel post resolves from channel history",
          len(out["resolved"]) == 1
          and out["resolved"][0]["found_ts"] == "1794594100.000000",
          str(out))


def test_unreadable_history_cannot_verify():
    mod = load_mod()

    def reader(channel, thread_ts, oldest):
        raise RuntimeError("boom")

    out = mod.reconcile([amb()], reader)
    check("unreadable history -> cannot_verify, nothing resolved",
          out["resolved"] == [] and len(out["cannot_verify"]) == 1,
          str(out))


def fake_slackcli(tmpdir, messages_by_key):
    """A fake slackcli that answers conversations read from a dict.

    Key: (channel, thread_ts or ""). Value: list of message dicts.
    Honors --workspace by ignoring it (both workspaces return the same).
    """
    path = os.path.join(tmpdir, "slackcli")
    data_path = os.path.join(tmpdir, "data.json")
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump({f"{k[0]}|{k[1]}": v for k, v in messages_by_key.items()},
                  f, ensure_ascii=False)
    script = (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "data = json.load(open(sys.argv[0].replace('slackcli', 'data.json'), encoding='utf-8'))\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['auth', 'whoami']:\n"
        "    print('neiro (U0CH1B19D4J)'); sys.exit(0)\n"
        "channel = args[2]\n"
        "thread_ts = ''\n"
        "oldest = '0'\n"
        "i = 3\n"
        "while i < len(args):\n"
        "    if args[i] == '--thread-ts': thread_ts = args[i+1]; i += 2\n"
        "    elif args[i] == '--oldest': oldest = args[i+1]; i += 2\n"
        "    elif args[i] == '--workspace': i += 2\n"
        "    else: i += 1\n"
        "msgs = [m for m in data.get(channel + '|' + thread_ts, []) if float(m['ts']) > float(oldest)]\n"
        "print(json.dumps({'channel_id': channel, 'message_count': len(msgs), 'has_more': False, 'messages': msgs}, ensure_ascii=False))\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(script)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def run_cli(tmpdir, amb_path, last_path, cli, extra=None):
    # --runs-file / --last-summary-file / --lock-file も常に一時ファイルにする。
    # 入れ忘れると既定の実ファイルにテストの実行ログが残ってしまう
    # （2026-11-14、最初の実装で実際に混入したため）。
    # ロックも一時ファイルにしないと、実運用の照合とテストがロックを
    # 取り合ってしまう。
    cmd = [sys.executable, RECONCILE_PY,
           "--ambiguous-file", amb_path, "--last-run-file", last_path,
           "--runs-file", os.path.join(tmpdir, "runs.jsonl"),
           "--last-summary-file", os.path.join(tmpdir, "last-run.json"),
           "--lock-file", os.path.join(tmpdir, "reconcile.lock"),
           "--slackcli", cli] + (extra or [])
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60)


def test_cli_end_to_end_and_gate():
    with tempfile.TemporaryDirectory() as tmp:
        amb_path = os.path.join(tmp, "amb.jsonl")
        last_path = os.path.join(tmp, "last.json")
        with open(amb_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(amb(), ensure_ascii=False) + "\n")
        cli = fake_slackcli(
            tmp, {(CHANNEL, THREAD_TS):
                  [{"ts": "1794594100.000000", "user": SELF, "text": TEXT}]})
        r = run_cli(tmp, amb_path, last_path, cli, ["--force"])
        out = json.loads(r.stdout[r.stdout.find("{"):])
        lines = open(amb_path, encoding="utf-8").read().strip().split("\n")
        check("CLI resolves and appends correction entry, exits 0",
              r.returncode == 0 and out["resolved"] == 1
              and len(lines) == 2
              and json.loads(lines[1])["outcome"] == "resolved_by_thread_check"
              and json.loads(lines[1])["found_ts"] == "1794594100.000000"
              and json.loads(lines[1])["text"] == TEXT,
              f"rc={r.returncode} out={r.stdout} err={r.stderr}")
        r2 = run_cli(tmp, amb_path, last_path, cli)
        out2 = json.loads(r2.stdout[r2.stdout.find("{"):])
        check("second run within 1h is gated, exits 0",
              r2.returncode == 0 and out2["gated"] is True
              and out2["resolved"] == 0, f"out={r2.stdout}")
        r3 = run_cli(tmp, amb_path, last_path, cli, ["--force", "--dry-run"])
        out3 = json.loads(r3.stdout[r3.stdout.find("{"):])
        lines3 = open(amb_path, encoding="utf-8").read().strip().split("\n")
        check("dry-run appends nothing",
              r3.returncode == 0 and out3["resolved"] == 0
              and len(lines3) == 2, f"out={r3.stdout}")


def test_cli_unreadable_exits_2():
    with tempfile.TemporaryDirectory() as tmp:
        amb_path = os.path.join(tmp, "amb.jsonl")
        last_path = os.path.join(tmp, "last.json")
        with open(amb_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(amb(), ensure_ascii=False) + "\n")
        cli = fake_slackcli(tmp, {})  # no data -> empty history, not an error
        r = run_cli(tmp, amb_path, last_path, cli, ["--force"])
        out = json.loads(r.stdout[r.stdout.find("{"):])
        check("empty history resolves nothing, exits 0",
              r.returncode == 0 and out["resolved"] == 0, f"out={r.stdout}")


def run_log_lines(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_cli_run_log_start_end_and_last_summary():
    with tempfile.TemporaryDirectory() as tmp:
        amb_path = os.path.join(tmp, "amb.jsonl")
        last_path = os.path.join(tmp, "last.json")
        runs_path = os.path.join(tmp, "runs.jsonl")
        summary_path = os.path.join(tmp, "last-run.json")
        with open(amb_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(amb(), ensure_ascii=False) + "\n")
        cli = fake_slackcli(
            tmp, {(CHANNEL, THREAD_TS):
                  [{"ts": "1794594100.000000", "user": SELF, "text": TEXT}]})
        extra = ["--force"]
        r = run_cli(tmp, amb_path, last_path, cli, extra)
        out = json.loads(r.stdout[r.stdout.find("{"):])
        lines = run_log_lines(runs_path)
        summary = json.load(open(summary_path, encoding="utf-8"))
        check("run writes start then end lines (ok, exit 0); "
              "last summary matches printed output",
              r.returncode == 0 and len(lines) == 2
              and lines[0]["event"] == "start"
              and lines[1]["event"] == "end"
              and lines[1]["ok"] is True and lines[1]["exit"] == 0
              and lines[1]["resolved"] == 1
              and lines[0]["dry_run"] is False
              and lines[1]["dry_run"] is False
              and summary["resolved"] == out["resolved"]
              and summary["targets"] == out["targets"],
              f"rc={r.returncode} lines={lines} summary={summary}")
        r2 = run_cli(tmp, amb_path, last_path, cli, [])
        out2 = json.loads(r2.stdout[r2.stdout.find("{"):])
        check("gated run writes no run-log lines",
              r2.returncode == 0 and out2["gated"] is True
              and len(run_log_lines(runs_path)) == 2,
              f"out={r2.stdout} lines={run_log_lines(runs_path)}")


def test_cli_dry_run_logs_with_flag():
    with tempfile.TemporaryDirectory() as tmp:
        amb_path = os.path.join(tmp, "amb.jsonl")
        last_path = os.path.join(tmp, "last.json")
        runs_path = os.path.join(tmp, "runs.jsonl")
        summary_path = os.path.join(tmp, "last-run.json")
        with open(amb_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(amb(), ensure_ascii=False) + "\n")
        cli = fake_slackcli(tmp, {(CHANNEL, THREAD_TS): []})
        extra = ["--force", "--dry-run"]
        r = run_cli(tmp, amb_path, last_path, cli, extra)
        lines = run_log_lines(runs_path)
        check("dry-run writes start/end with dry_run true; "
              "gate file untouched",
              r.returncode == 0 and len(lines) == 2
              and lines[0]["event"] == "start"
              and lines[1]["event"] == "end"
              and lines[0]["dry_run"] is True
              and lines[1]["dry_run"] is True
              and not os.path.exists(last_path),
              f"rc={r.returncode} lines={lines}")


def test_cli_run_log_write_failure_swallowed():
    with tempfile.TemporaryDirectory() as tmp:
        amb_path = os.path.join(tmp, "amb.jsonl")
        last_path = os.path.join(tmp, "last.json")
        runs_dir = os.path.join(tmp, "runs-dir")
        os.makedirs(runs_dir)
        with open(amb_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(amb(), ensure_ascii=False) + "\n")
        cli = fake_slackcli(
            tmp, {(CHANNEL, THREAD_TS):
                  [{"ts": "1794594100.000000", "user": SELF, "text": TEXT}]})
        extra = ["--force", "--runs-file", runs_dir]
        r = run_cli(tmp, amb_path, last_path, cli, extra)
        out = json.loads(r.stdout[r.stdout.find("{"):])
        check("runs file unwritable: run still exits 0 and resolves",
              r.returncode == 0 and out["resolved"] == 1,
              f"rc={r.returncode} out={r.stdout} err={r.stderr}")


def test_cli_lock_busy_gated():
    import fcntl
    with tempfile.TemporaryDirectory() as tmp:
        amb_path = os.path.join(tmp, "amb.jsonl")
        last_path = os.path.join(tmp, "last.json")
        runs_path = os.path.join(tmp, "runs.jsonl")
        lock_path = os.path.join(tmp, "reconcile.lock")
        with open(amb_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(amb(), ensure_ascii=False) + "\n")
        cli = fake_slackcli(
            tmp, {(CHANNEL, THREAD_TS):
                  [{"ts": "1794594100.000000", "user": SELF, "text": TEXT}]})
        # このテストプロセスがロックを握ったまま、CLI を --force で走らせる。
        holder = open(lock_path, "a+", encoding="utf-8")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            r = run_cli(tmp, amb_path, last_path, cli, ["--force"])
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()
        out = json.loads(r.stdout[r.stdout.find("{"):])
        lines = open(amb_path, encoding="utf-8").read().strip().split("\n")
        check("lock held by another process: run exits gated "
              "(lock_busy), appends nothing, writes no run-log lines",
              r.returncode == 0 and out["gated"] is True
              and out.get("lock_busy") is True
              and len(lines) == 1 and not os.path.exists(runs_path)
              and not os.path.exists(last_path),
              f"rc={r.returncode} out={r.stdout} err={r.stderr}")


def test_cli_lock_released_after_run():
    with tempfile.TemporaryDirectory() as tmp:
        amb_path = os.path.join(tmp, "amb.jsonl")
        last_path = os.path.join(tmp, "last.json")
        with open(amb_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(amb(), ensure_ascii=False) + "\n")
        cli = fake_slackcli(
            tmp, {(CHANNEL, THREAD_TS):
                  [{"ts": "1794594100.000000", "user": SELF, "text": TEXT}]})
        r1 = run_cli(tmp, amb_path, last_path, cli, ["--force"])
        out1 = json.loads(r1.stdout[r1.stdout.find("{"):])
        r2 = run_cli(tmp, amb_path, last_path, cli, ["--force"])
        out2 = json.loads(r2.stdout[r2.stdout.find("{"):])
        check("sequential --force runs both run: lock is released "
              "on process exit (second run is not lock-gated)",
              r1.returncode == 0 and out1["gated"] is False
              and r2.returncode == 0 and out2["gated"] is False
              and out2.get("lock_busy") is None,
              f"r1={r1.stdout} r2={r2.stdout}")


def main():
    test_exact_match_resolves()
    test_normalized_whitespace_match_resolves()
    test_normalized_entity_match_resolves()
    test_different_text_no_resolve_after_normalize()
    test_other_user_identical_text_no_resolve()
    test_reply_before_parent_no_resolve()
    test_no_text_closed_without_read()
    test_whitespace_only_text_treated_as_no_text()
    test_no_text_already_closed_not_reclosed()
    test_no_text_after_close_forms_new_group()
    test_append_closed_no_text_roundtrip()
    test_already_resolved_found_ts_not_reused()
    test_no_reuse_across_targets()
    test_channel_post_resolves()
    test_unreadable_history_cannot_verify()
    test_cli_end_to_end_and_gate()
    test_cli_unreadable_exits_2()
    test_cli_run_log_start_end_and_last_summary()
    test_cli_dry_run_logs_with_flag()
    test_cli_run_log_write_failure_swallowed()
    test_cli_lock_busy_gated()
    test_cli_lock_released_after_run()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
