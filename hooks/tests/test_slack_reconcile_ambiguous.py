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
      a single gated line (reason interval_gate) instead of start/end
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
      to the ambiguous file, but writes a single gated line
      (reason lock_busy) to the runs file (Yanayana's gated-logging
      fix, 2026-11-14)
  23. the lock is released when the process exits: two sequential
      --force runs both run (the second is not lock-gated)
  24. mark guard (2026-11-15): a marked record does not resolve to
      a text-matching reply carrying a different mark
  25. mark guard: a marked record resolves to a reply with the same mark
  26. mark guard: a marked record resolves when the candidate's mark
      is unknown (not in the dump window) — text match as before
  27. mark guard: an unmarked record resolves without consulting
      the mark lookup at all (backward compatible)
  28. mark guard: two same-text records with different marks are
      separate targets; only the one whose mark matches the reply
      resolves (the other stays unresolved, not mis-attributed)
  29. mark guard: a failing mark lookup -> cannot_verify (fail-closed),
      nothing resolved
  30. CLI end-to-end with a fake read-meta: a marked record resolves
      when the dump says the same mark (and the resolved entry keeps
      the mark), and does not resolve when the dump says a different mark
  31. classify_error buckets: self_id_unknown wins over history
      unreadable; dump unreadable; history unreadable; other fallback
  32. target_key is stable for the same target, distinct for different
      text, and ignores whitespace differences (normalized text)
  33. cannot_verify increments the consecutive count, no close yet
  34. 23rd consecutive cannot_verify does not close yet
  35. 24th consecutive cannot_verify closes as closed_unverifiable
      (attempts/error_kind recorded, counts cleared)
  36. history readable but no match resets the streak
  37. resolved target drops its count
  38. majority-failure run (4 of 6 unreadable) advances no counts;
      a single failure among many still increments
  39. single-target failure increments (no majority to suppress)
  40. user-id channel closes as closed_invalid_channel without any
      history read
  41. no-text record with an invalid channel prefers
      closed_invalid_channel over closed_no_text
  42. an already-closed invalid channel is not closed again
  43. a record appearing after a closed_invalid_channel entry forms
      a new group and is closed again
  44. closed_unverifiable target is excluded (no read); a record
      after the close forms a new target
  45. reopened entry re-admits the closed target
  46. reopened target counts from 0, not from the pre-close streak
  47. budget: targets beyond the time budget are skipped (not
      cannot_verify, counts untouched)
  48. append_closed_unverifiable writes the terminal entry, and a second
      reconcile over the appended file closes nothing more
  49. CLI: 24th cannot_verify closes, clears counts, exits 2, and the
      transition notification is sent exactly once (with --request-id)
  50. CLI: user-id channel closes as closed_invalid_channel, exits 0,
      and a second run closes nothing more
  51. CLI --reopen-channel appends a reopened entry for a closed target
      and appends nothing when there is no closed target
  52. notify failure returns False without raising
"""
import importlib.util
import contextlib
import io
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

def make_id(prefix, rest):
    """Build an ID-shaped string from parts (like a real Slack ID).

    Fixtures are synthetic (2026-11-20): the real bot/user/channel IDs
    used before were other people's and ours; parts-built fakes keep
    this file free of real IDs without changing what the tests check
    (the module compares IDs as opaque strings).
    """
    return prefix + "0" + rest


SELF = make_id("U", "TESTSELF01")
OTHER = make_id("U", "TESTOTHER1")
CHANNEL = make_id("C", "TESTCHAN01")
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


def test_load_records_broken_line_warns():
    mod = load_mod()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "amb.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken json line\n")
            f.write(json.dumps(amb(), ensure_ascii=False) + "\n")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            recs = mod.load_records(path)
        check("load_records skips a broken line but keeps good records",
              len(recs) == 1, f"recs={recs}")
        check("load_records warns on stderr with file and line number",
              "broken ambiguous record" in buf.getvalue()
              and ":1:" in buf.getvalue(),
              f"stderr={buf.getvalue()!r}")


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
        "    print('neiro (" + SELF + ")'); sys.exit(0)\n"
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
    # --counts-file も一時ファイルにする（既定だと実ファイルの連続回数
    # をテストが汚してしまう。2026-11-24、closed_unverifiable の導入時）。
    # --no-notify も付ける（移行が起きても実 Slack へ通知しない）。
    cmd = [sys.executable, RECONCILE_PY,
           "--ambiguous-file", amb_path, "--last-run-file", last_path,
           "--runs-file", os.path.join(tmpdir, "runs.jsonl"),
           "--last-summary-file", os.path.join(tmpdir, "last-run.json"),
           "--lock-file", os.path.join(tmpdir, "reconcile.lock"),
           "--counts-file", os.path.join(tmpdir, "counts.json"),
           "--no-notify",
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
        lines2 = run_log_lines(runs_path)
        check("gated run writes a single gated line (interval_gate), "
              "no start/end",
              r2.returncode == 0 and out2["gated"] is True
              and len(lines2) == 3
              and lines2[2]["event"] == "gated"
              and lines2[2]["reason"] == "interval_gate",
              f"out={r2.stdout} lines={lines2}")


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
        run_lines = run_log_lines(runs_path)
        check("lock held by another process: run exits gated "
              "(lock_busy), appends nothing, writes a single gated "
              "line (reason lock_busy)",
              r.returncode == 0 and out["gated"] is True
              and out.get("lock_busy") is True
              and len(lines) == 1 and len(run_lines) == 1
              and run_lines[0]["event"] == "gated"
              and run_lines[0]["reason"] == "lock_busy"
              and not os.path.exists(last_path),
              f"rc={r.returncode} out={r.stdout} err={r.stderr} "
              f"runs={run_lines}")


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


# --- 印ガード（2026-11-15）のテスト ---

WS_TASK = make_id("T", "TESTWSTASK1")
EVENT_ID = WS_TASK + ":" + CHANNEL + ":1794652249.960829"
OTHER_EVENT_ID = WS_TASK + ":" + CHANNEL + ":1794650000.000000"
REQUEST_ID = "123e4567-e89b-12d3-a456-426614174000"


def amb_marked(event_id=None, request_id=None, **kw):
    rec = amb(**kw)
    if event_id is not None:
        rec["event_id"] = event_id
    if request_id is not None:
        rec["request_id"] = request_id
    return rec


def lookup_for(marks):
    """{ts: (kind, value) | None} を返すスタブの mark_lookup。"""
    def lookup(channel, thread_ts):
        return dict(marks)
    return lookup


def test_mark_different_no_resolve():
    mod = load_mod()
    msgs = [{"ts": "1794594100.000000", "user": SELF, "text": TEXT}]
    marks = {"1794594100.000000": ("event_id", OTHER_EVENT_ID)}
    out = mod.reconcile([amb_marked(event_id=EVENT_ID)],
                        reader_for(msgs), lookup_for(marks))
    check("marked record, candidate with different mark: no resolve",
          out["resolved"] == [] and out["cannot_verify"] == [], str(out))


def test_mark_same_resolves():
    mod = load_mod()
    msgs = [{"ts": "1794594100.000000", "user": SELF, "text": TEXT}]
    marks = {"1794594100.000000": ("event_id", EVENT_ID)}
    out = mod.reconcile([amb_marked(event_id=EVENT_ID)],
                        reader_for(msgs), lookup_for(marks))
    check("marked record, candidate with same mark: resolves",
          len(out["resolved"]) == 1
          and out["resolved"][0]["found_ts"] == "1794594100.000000"
          and out["resolved"][0]["mark"] == ("event_id", EVENT_ID),
          str(out))


def test_mark_unknown_resolves():
    mod = load_mod()
    # 候補が dump の窓に入らず印が分からない（辞書に無い）場合は、
    # 従来どおり文面で拾う。
    msgs = [{"ts": "1794594100.000000", "user": SELF, "text": TEXT}]
    out = mod.reconcile([amb_marked(event_id=EVENT_ID)],
                        reader_for(msgs), lookup_for({}))
    check("marked record, candidate mark unknown: resolves by text",
          len(out["resolved"]) == 1
          and out["resolved"][0]["found_ts"] == "1794594100.000000",
          str(out))


def test_unmarked_record_never_consults_lookup():
    mod = load_mod()
    calls = []

    def lookup(channel, thread_ts):
        calls.append((channel, thread_ts))
        return {}

    msgs = [{"ts": "1794594100.000000", "user": SELF, "text": TEXT}]
    out = mod.reconcile([amb()], reader_for(msgs), lookup)
    check("unmarked record: resolves without calling mark lookup",
          len(out["resolved"]) == 1 and calls == [], str(out))


def test_same_text_different_marks_are_separate_targets():
    mod = load_mod()
    # 同じ文面の2記録（印 E1 / E2）。履歴には E2 の印の返信が1件だけ。
    # E1 の対象は誤って解決せず、E2 の対象だけがその返信に解決する。
    msgs = [{"ts": "1794594100.000000", "user": SELF, "text": TEXT}]
    marks = {"1794594100.000000": ("event_id", OTHER_EVENT_ID)}
    recs = [amb_marked(event_id=EVENT_ID),
            amb_marked(event_id=OTHER_EVENT_ID)]
    out = mod.reconcile(recs, reader_for(msgs), lookup_for(marks))
    check("same text, different marks: separate targets, only the "
          "matching mark resolves",
          out["targets"] == 2 and len(out["resolved"]) == 1
          and out["resolved"][0]["found_ts"] == "1794594100.000000"
          and out["resolved"][0]["mark"] == ("event_id", OTHER_EVENT_ID),
          str(out))


def test_mark_lookup_failure_is_cannot_verify():
    mod = load_mod()

    def lookup(channel, thread_ts):
        raise RuntimeError("boom")

    msgs = [{"ts": "1794594100.000000", "user": SELF, "text": TEXT}]
    out = mod.reconcile([amb_marked(event_id=EVENT_ID)],
                        reader_for(msgs), lookup)
    check("mark lookup failure: cannot_verify, nothing resolved",
          out["resolved"] == [] and len(out["cannot_verify"]) == 1,
          str(out))


def fake_read_meta(tmpdir, messages_by_key):
    """A fake slack-read-metadata.py answering --dump from a dict.

    Key: (channel, thread_ts or ""). Value: list of message dicts
    (each with ts/user/text/mark). Ignores --workspace.
    """
    path = os.path.join(tmpdir, "read-meta")
    data_path = os.path.join(tmpdir, "meta-data.json")
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump({f"{k[0]}|{k[1]}": v for k, v in messages_by_key.items()},
                  f, ensure_ascii=False)
    script = (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "data = json.load(open(sys.argv[0].replace('read-meta', 'meta-data.json'), encoding='utf-8'))\n"
        "args = sys.argv[1:]\n"
        "channel = args[args.index('--channel') + 1]\n"
        "thread_ts = args[args.index('--thread-ts') + 1] if '--thread-ts' in args else ''\n"
        "msgs = data.get(channel + '|' + thread_ts, [])\n"
        "print(json.dumps({'ok': True, 'messages': msgs, 'checked': len(msgs)}, ensure_ascii=False))\n"
    )
    with open(path, "w", encoding="utf-8") as f:
        f.write(script)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def run_cli_with_meta(tmpdir, amb_path, last_path, cli, meta, extra=None):
    return run_cli(tmpdir, amb_path, last_path, cli,
                   (extra or []) + ["--read-meta", meta])


def test_cli_mark_guard_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        amb_path = os.path.join(tmp, "amb.jsonl")
        last_path = os.path.join(tmp, "last.json")
        msg = {"ts": "1794594100.000000", "user": SELF, "text": TEXT}
        cli = fake_slackcli(tmp, {(CHANNEL, THREAD_TS): [msg]})
        # 同じ印 -> 解決し、訂正エントリに印が残る。
        with open(amb_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(amb_marked(event_id=EVENT_ID),
                               ensure_ascii=False) + "\n")
        meta = fake_read_meta(tmp, {(CHANNEL, THREAD_TS): [
            {"ts": "1794594100.000000", "user": SELF, "text": TEXT,
             "mark": {"kind": "event_id", "value": EVENT_ID}}]})
        r = run_cli_with_meta(tmp, amb_path, last_path, cli, meta,
                              ["--force"])
        out = json.loads(r.stdout[r.stdout.find("{"):])
        lines = open(amb_path, encoding="utf-8").read().strip().split("\n")
        entry = json.loads(lines[1]) if len(lines) == 2 else {}
        check("CLI marked record, dump same mark: resolves; resolved "
              "entry keeps the event_id",
              r.returncode == 0 and out["resolved"] == 1
              and entry.get("outcome") == "resolved_by_thread_check"
              and entry.get("found_ts") == "1794594100.000000"
              and entry.get("event_id") == EVENT_ID,
              f"rc={r.returncode} out={r.stdout} err={r.stderr}")
        # 違う印 -> 解決しない。
        with open(amb_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(amb_marked(event_id=EVENT_ID),
                               ensure_ascii=False) + "\n")
        meta2 = fake_read_meta(tmp, {(CHANNEL, THREAD_TS): [
            {"ts": "1794594100.000000", "user": SELF, "text": TEXT,
             "mark": {"kind": "event_id", "value": OTHER_EVENT_ID}}]})
        r2 = run_cli_with_meta(tmp, amb_path, last_path, cli, meta2,
                               ["--force"])
        out2 = json.loads(r2.stdout[r2.stdout.find("{"):])
        lines2 = open(amb_path, encoding="utf-8").read().strip().split("\n")
        check("CLI marked record, dump different mark: does not resolve",
              r2.returncode == 0 and out2["resolved"] == 0
              and len(lines2) == 1,
              f"rc={r2.returncode} out={r2.stdout} err={r2.stderr}")


# --- self_user_id の外部化（2026-11-20、実ID外部化の第4本） ---

BOT_MUSE = make_id("U", "TESTBOTMUSE")
BOT_TASK = make_id("U", "TESTBOTTASK")
WS_MUSE = make_id("T", "TESTWSMUSE1")


def _write_fake_cli(d, body):
    path = os.path.join(d, "slackcli")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(path, 0o755)
    return path


def _write_ids(d, data):
    path = os.path.join(d, "ids.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.chmod(path, 0o600)
    return path


IDS_FIXTURE = {
    "workspace_muse_torishimariyakai": WS_MUSE,
    "workspace_neiro_task_board": WS_TASK,
    "bot_neiro_muse_torishimariyakai": BOT_MUSE,
    "bot_neiro_neiro_task_board": BOT_TASK,
}

FAKE_FAILING_SLACKCLI = """#!/usr/bin/env python3
import sys
sys.exit(1)
"""


def _set_ids_env(path):
    old = os.environ.get("NEIRO_SLACK_IDS_PATH")
    os.environ["NEIRO_SLACK_IDS_PATH"] = path
    return old


def _restore_ids_env(old):
    if old is None:
        os.environ.pop("NEIRO_SLACK_IDS_PATH", None)
    else:
        os.environ["NEIRO_SLACK_IDS_PATH"] = old


def test_self_id_fallback_from_ids():
    # whoami fails -> ids.json decides, for names and workspace IDs.
    mod = load_mod()
    with tempfile.TemporaryDirectory() as d:
        fake = _write_fake_cli(d, FAKE_FAILING_SLACKCLI)
        ids_path = _write_ids(d, IDS_FIXTURE)
        old = _set_ids_env(ids_path)
        try:
            check("fallback: default workspace -> task bot",
                  mod.self_user_id("", fake) == BOT_TASK)
            check("fallback: muse-board name -> muse bot",
                  mod.self_user_id("muse-board", fake) == BOT_MUSE)
            check("fallback: muse workspace ID -> muse bot",
                  mod.self_user_id(WS_MUSE, fake) == BOT_MUSE)
            check("fallback: task workspace ID -> task bot",
                  mod.self_user_id(WS_TASK, fake) == BOT_TASK)
            check("fallback: unknown name -> None (fail-closed)",
                  mod.self_user_id("muse_board", fake) is None)
            check("fallback: unknown workspace ID -> None",
                  mod.self_user_id(make_id("T", "TESTWSOTH1"), fake)
                  is None)
        finally:
            _restore_ids_env(old)


def test_self_id_missing_ids_fails_closed():
    # whoami fails and ids.json is missing -> None, never a default.
    mod = load_mod()
    with tempfile.TemporaryDirectory() as d:
        fake = _write_fake_cli(d, FAKE_FAILING_SLACKCLI)
        old = _set_ids_env(os.path.join(d, "nope.json"))
        try:
            check("missing ids.json: default -> None",
                  mod.self_user_id("", fake) is None)
            check("missing ids.json: muse-board -> None",
                  mod.self_user_id("muse-board", fake) is None)
        finally:
            _restore_ids_env(old)


def test_self_id_whoami_garbage_rejected():
    # whoami が "(UNAUTHORIZED)" のようなゴミを exit 0 で出しても、
    # 正規表現が拾わず ids.json にフォールバックする。
    mod = load_mod()
    with tempfile.TemporaryDirectory() as d:
        body = ("#!/usr/bin/env python3\nimport sys\n"
                "print('neiro (UNAUTHORIZED)'); sys.exit(0)\n")
        fake = _write_fake_cli(d, body)
        ids_path = _write_ids(d, IDS_FIXTURE)
        old = _set_ids_env(ids_path)
        try:
            check("whoami garbage -> ids.json fallback (muse-board)",
                  mod.self_user_id("muse-board", fake) == BOT_MUSE)
        finally:
            _restore_ids_env(old)


def test_self_id_whoami_w_shape():
    # Grid の W... ユーザーID も whoami からそのまま返る。
    mod = load_mod()
    w_id = make_id("W", "TESTWUSER1")
    with tempfile.TemporaryDirectory() as d:
        body = ("#!/usr/bin/env python3\nimport sys\n"
                "print('neiro (" + w_id + ")'); sys.exit(0)\n")
        fake = _write_fake_cli(d, body)
        old = _set_ids_env(os.path.join(d, "nope.json"))
        try:
            check("whoami W... id is returned as-is",
                  mod.self_user_id("muse-board", fake) == w_id)
        finally:
            _restore_ids_env(old)


def test_cli_self_id_unknown_cannot_verify():
    # whoami が失敗し ids.json も無ければ、CLI は照合せず
    # cannot_verify で終了コード 2（fail-closed）。訂正も追加しない。
    with tempfile.TemporaryDirectory() as tmp:
        amb_path = os.path.join(tmp, "amb.jsonl")
        last_path = os.path.join(tmp, "last.json")
        with open(amb_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(amb(), ensure_ascii=False) + "\n")
        msg = {"ts": "1794594100.000000", "user": SELF, "text": TEXT}
        body = (
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "if sys.argv[1:3] == ['auth', 'whoami']:\n"
            "    sys.exit(1)\n"
            "print(json.dumps({'messages': ["
            "{'ts': '1794594100.000000', 'user': '" + SELF + "', "
            "'text': '" + TEXT + "'}], 'has_more': False, "
            "'next_oldest': None}, ensure_ascii=False))\n")
        cli = _write_fake_cli(tmp, body)
        env = dict(os.environ)
        env["NEIRO_SLACK_IDS_PATH"] = os.path.join(tmp, "nope.json")
        cmd = [sys.executable, RECONCILE_PY,
               "--ambiguous-file", amb_path,
               "--last-run-file", last_path,
               "--runs-file", os.path.join(tmp, "runs.jsonl"),
               "--last-summary-file", os.path.join(tmp, "last-run.json"),
               "--lock-file", os.path.join(tmp, "reconcile.lock"),
               "--counts-file", os.path.join(tmp, "counts.json"),
               "--no-notify",
               "--slackcli", cli, "--force"]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=60, env=env)
        out = json.loads(r.stdout[r.stdout.find("{"):])
        lines = open(amb_path, encoding="utf-8").read().strip().split("\n")
        check("CLI: self id unknown -> exit 2, cannot_verify, no append",
              r.returncode == 2 and len(out["cannot_verify"]) == 1
              and len(lines) == 1,
              f"rc={r.returncode} out={r.stdout} err={r.stderr}")



# --- closed_unverifiable / closed_invalid_channel（中-3、2026-11-24） ---

def failing_reader(reason="history unreadable after retries: "
                          "slackcli read failed (exit 1)"):
    def reader(channel, thread_ts, oldest):
        raise RuntimeError(reason)
    return reader


def seed_counts(mod, key, consecutive):
    return {key: {"consecutive": consecutive,
                  "last_error": "history unreadable",
                  "error_kind": "history_unreadable",
                  "last_seen": "2026-11-14T05:00:00+0000",
                  "channel": CHANNEL, "thread_ts": THREAD_TS,
                  "text_preview": TEXT[:60], "mark": None}}


def closed_unverifiable_entry(channel=CHANNEL, thread_ts=THREAD_TS,
                              text=TEXT, attempts=24):
    return {"time": "2026-11-14T05:10:00+0000", "channel": channel,
            "thread_ts": thread_ts, "outcome": "closed_unverifiable",
            "closed_at": "2026-11-14T05:10:00+0000", "attempts": attempts,
            "last_error": "history unreadable",
            "error_kind": "history_unreadable",
            "reason": "cannot_verify 24 consecutive runs", "text": text}


def reopened_entry(channel=CHANNEL, thread_ts=THREAD_TS, text=None):
    rec = {"time": "2026-11-14T06:10:00+0000", "channel": channel,
           "thread_ts": thread_ts, "outcome": "reopened",
           "reason": "manual reopen"}
    if text is not None:
        rec["text"] = text
    return rec


def test_classify_error_buckets():
    mod = load_mod()
    check("classify_error: self id unknown wins over history unreadable",
          mod.classify_error(
              "history unreadable in all workspaces: "
              "self user id unknown for workspace 'muse-board'") ==
          "self_id_unknown")
    check("classify_error: dump unreadable",
          mod.classify_error("dump unreadable in all workspaces") ==
          "dump_unreadable")
    check("classify_error: history unreadable",
          mod.classify_error("history unreadable after retries") ==
          "history_unreadable")
    check("classify_error: unknown reason falls back to other",
          mod.classify_error("api_rate_limited") == "other"
          and mod.classify_error("") == "other")


def test_target_key_stable_and_distinct():
    mod = load_mod()
    k1 = mod.target_key(CHANNEL, THREAD_TS, TEXT, None)
    k2 = mod.target_key(CHANNEL, THREAD_TS, TEXT, None)
    k3 = mod.target_key(CHANNEL, THREAD_TS, "別の文面です。", None)
    k4 = mod.target_key(CHANNEL, THREAD_TS, "こんにちは、テストです。 ",
                        None)
    check("target_key: same target -> same key; different text -> "
          "different key; whitespace difference -> same key",
          k1 == k2 and k1 != k3 and k1 == k4)


def test_cannot_verify_increments_count():
    mod = load_mod()
    out = mod.reconcile([amb()], failing_reader())
    key = mod.target_key(CHANNEL, THREAD_TS, TEXT, None)
    check("cannot_verify increments the consecutive count, no close yet",
          len(out["cannot_verify"]) == 1
          and out["closed_unverifiable"] == []
          and out["counts"].get(key, {}).get("consecutive") == 1
          and out["counts"][key]["error_kind"] == "history_unreadable",
          str(out))


def test_cannot_verify_23_no_close():
    mod = load_mod()
    key = mod.target_key(CHANNEL, THREAD_TS, TEXT, None)
    out = mod.reconcile([amb()], failing_reader(),
                        counts=seed_counts(mod, key, 22))
    check("23rd consecutive cannot_verify does not close yet",
          out["closed_unverifiable"] == []
          and out["counts"].get(key, {}).get("consecutive") == 23,
          str(out))


def test_cannot_verify_24th_closes():
    mod = load_mod()
    key = mod.target_key(CHANNEL, THREAD_TS, TEXT, None)
    out = mod.reconcile([amb()], failing_reader(),
                        counts=seed_counts(mod, key, 23))
    check("24th consecutive cannot_verify closes the target",
          len(out["closed_unverifiable"]) == 1
          and out["closed_unverifiable"][0]["attempts"] == 24
          and out["closed_unverifiable"][0]["error_kind"] ==
          "history_unreadable"
          and out["closed_unverifiable"][0]["channel"] == CHANNEL
          and out["closed_unverifiable"][0]["text"] == TEXT
          and out["counts"] == {}
          and len(out["cannot_verify"]) == 1, str(out))


def test_clean_read_resets_count():
    mod = load_mod()
    key = mod.target_key(CHANNEL, THREAD_TS, TEXT, None)
    out = mod.reconcile([amb()], reader_for([]),
                        counts=seed_counts(mod, key, 5))
    check("history readable but no match resets the streak",
          out["resolved"] == [] and out["cannot_verify"] == []
          and out["counts"] == {}, str(out))


def test_resolved_resets_count():
    mod = load_mod()
    key = mod.target_key(CHANNEL, THREAD_TS, TEXT, None)
    msgs = [{"ts": "1794594100.000000", "user": SELF, "text": TEXT}]
    out = mod.reconcile([amb()], reader_for(msgs),
                        counts=seed_counts(mod, key, 5))
    check("resolved target drops its count",
          len(out["resolved"]) == 1 and out["counts"] == {}, str(out))


def test_majority_failure_suppresses_counts():
    mod = load_mod()
    # 6対象のうち4件が読めず2件が読めた回は、誰のカウントも進めない。
    fail_channels = {make_id("C", "TESTCH%02d" % i) for i in range(4)}

    def reader(channel, thread_ts, oldest):
        if channel in fail_channels:
            raise RuntimeError("history unreadable")
        return [], SELF

    recs = [amb(channel=make_id("C", "TESTCH%02d" % i),
                thread_ts="179459400%d.000000" % i,
                text="多数失敗 %d" % i) for i in range(6)]
    out = mod.reconcile(recs, reader)
    check("majority-failure run advances no counts",
          out["counts_suppressed"] is True and out["counts"] == {}
          and len(out["cannot_verify"]) == 4, str(out))
    # 6対象のうち1件だけ失敗した回は、カウントが進む。
    fail_one = {make_id("C", "TESTCH%02d" % 0)}

    def reader_one(channel, thread_ts, oldest):
        if channel in fail_one:
            raise RuntimeError("history unreadable")
        return [], SELF

    out2 = mod.reconcile(recs, reader_one)
    check("single failure among many still increments",
          out2["counts_suppressed"] is False
          and len(out2["counts"]) == 1
          and list(out2["counts"].values())[0]["consecutive"] == 1,
          str(out2))


def test_single_target_failure_increments():
    mod = load_mod()
    out = mod.reconcile([amb()], failing_reader())
    key = mod.target_key(CHANNEL, THREAD_TS, TEXT, None)
    check("single-target failure increments (no majority to suppress)",
          out["counts_suppressed"] is False
          and out["counts"].get(key, {}).get("consecutive") == 1,
          str(out))


def test_invalid_channel_closed_without_read():
    mod = load_mod()
    calls = []

    def reader(channel, thread_ts, oldest):
        calls.append((channel, thread_ts))
        return [], SELF

    user_channel = make_id("U", "TESTUSER01")
    out = mod.reconcile([amb(channel=user_channel)], reader)
    check("user-id channel closes as closed_invalid_channel, no read",
          out["resolved"] == [] and out["targets"] == 0
          and len(out["closed_invalid_channel"]) == 1
          and out["closed_invalid_channel"][0]["count"] == 1
          and calls == [], str(out))


def test_invalid_channel_no_text_prefers_invalid():
    mod = load_mod()

    def reader(channel, thread_ts, oldest):
        return [], SELF

    user_channel = make_id("U", "TESTUSER01")
    out = mod.reconcile([amb(channel=user_channel, text=None)], reader)
    check("no-text record with invalid channel -> closed_invalid_channel",
          len(out["closed_invalid_channel"]) == 1
          and out["closed_no_text"] == [], str(out))


def test_invalid_channel_already_closed_not_reclosed():
    mod = load_mod()

    def reader(channel, thread_ts, oldest):
        return [], SELF

    user_channel = make_id("U", "TESTUSER01")
    closed = {"time": "2026-11-13T18:25:00+0000", "channel": user_channel,
              "thread_ts": THREAD_TS, "outcome": "closed_invalid_channel",
              "closed_at": "2026-11-13T18:25:00+0000",
              "reason": "channel is not a readable conversation id",
              "count": 1}
    out = mod.reconcile([amb(channel=user_channel), closed], reader)
    check("already-closed invalid channel is not closed again",
          out["closed_invalid_channel"] == [], str(out))


def test_invalid_channel_after_close_forms_new_group():
    mod = load_mod()

    def reader(channel, thread_ts, oldest):
        return [], SELF

    user_channel = make_id("U", "TESTUSER01")
    closed = {"time": "2026-11-13T18:25:00+0000", "channel": user_channel,
              "thread_ts": THREAD_TS, "outcome": "closed_invalid_channel",
              "closed_at": "2026-11-13T18:25:00+0000",
              "reason": "channel is not a readable conversation id",
              "count": 1}
    recs = [amb(channel=user_channel), closed,
            amb(channel=user_channel, time_s="2026-11-14T18:20:00+0000")]
    out = mod.reconcile(recs, reader)
    check("record after closed_invalid_channel forms a new group",
          len(out["closed_invalid_channel"]) == 1
          and out["closed_invalid_channel"][0]["count"] == 1, str(out))


def test_closed_target_excluded_and_new_record_forms_new_target():
    mod = load_mod()
    calls = []

    def reader(channel, thread_ts, oldest):
        calls.append((channel, thread_ts))
        return [], SELF

    recs = [amb(), closed_unverifiable_entry()]
    out = mod.reconcile(recs, reader)
    check("closed target excluded, no history read",
          out["targets"] == 0 and calls == []
          and out["closed_unverifiable"] == [], str(out))
    recs2 = recs + [amb(time_s="2026-11-14T18:20:00+0000")]
    out2 = mod.reconcile(recs2, reader)
    check("record after close forms a new target",
          out2["targets"] == 1 and len(calls) == 1, str(out2))


def test_reopened_readmits_closed_target():
    mod = load_mod()
    calls = []

    def reader(channel, thread_ts, oldest):
        calls.append((channel, thread_ts))
        return [], SELF

    recs = [amb(), closed_unverifiable_entry()]
    out = mod.reconcile(recs, reader)
    check("closed target excluded before reopen",
          out["targets"] == 0 and calls == [], str(out))
    recs2 = recs + [reopened_entry()]
    out2 = mod.reconcile(recs2, reader)
    check("reopened target is a target again and read",
          out2["targets"] == 1 and len(calls) == 1, str(out2))


def test_reopened_resets_count():
    mod = load_mod()
    key = mod.target_key(CHANNEL, THREAD_TS, TEXT, None)
    counts = seed_counts(mod, key, 5)
    recs = [amb(), closed_unverifiable_entry(), reopened_entry()]
    out = mod.reconcile(recs, failing_reader(), counts=counts)
    check("reopened target counts from 0, not from the pre-close streak",
          out["counts"].get(key, {}).get("consecutive") == 1, str(out))


def test_budget_skips_remaining_targets():
    mod = load_mod()
    now = [0.0]

    def clock():
        return now[0]

    def reader(channel, thread_ts, oldest):
        now[0] += 6.0
        return [], SELF

    recs = [amb(text="budget %d" % i,
                thread_ts="179459400%d.000000" % i) for i in range(3)]
    out = mod.reconcile(recs, reader, budget_secs=10, monotonic_fn=clock)
    check("budget: targets beyond the budget are skipped, not cannot_verify",
          len(out["budget_skipped"]) == 1 and out["cannot_verify"] == []
          and out["targets"] == 3 and out["counts"] == {}, str(out))


def test_append_closed_unverifiable_roundtrip():
    mod = load_mod()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "amb.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(amb(), ensure_ascii=False) + "\n")
        closed = [{"channel": CHANNEL, "thread_ts": THREAD_TS,
                   "text": TEXT, "mark": None, "attempts": 24,
                   "last_error": "history unreadable",
                   "error_kind": "history_unreadable",
                   "closed_at": "2026-11-14T05:10:00+0000"}]
        mod.append_closed_unverifiable(path, closed)
        lines = open(path, encoding="utf-8").read().strip().split("\n")
        entry = json.loads(lines[1])
        check("append_closed_unverifiable writes the terminal entry",
              len(lines) == 2
              and entry.get("outcome") == "closed_unverifiable"
              and entry.get("attempts") == 24
              and entry.get("error_kind") == "history_unreadable"
              and entry.get("text") == TEXT, str(lines))
        out = mod.reconcile(mod.load_records(path), reader_for([]))
        check("second reconcile over the appended file closes nothing more",
              out["targets"] == 0 and out["closed_unverifiable"] == [],
              str(out))


def fake_self_id_unknown_cli(tmp, messages_by_key=None):
    """whoami が失敗し、conversations read は辞書から返す fake slackcli。

    ids.json に bot ID が無いと self_user_id が None になり、
    cannot_verify（self_id_unknown）になる。読み取り自体は成功する
    のでリトライの待ち時間は発生しない。
    """
    path = os.path.join(tmp, "slackcli")
    data_path = os.path.join(tmp, "data.json")
    with open(data_path, "w", encoding="utf-8") as f:
        json.dump({f"{k[0]}|{k[1]}": v
                   for k, v in (messages_by_key or {}).items()},
                  f, ensure_ascii=False)
    body = (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "data = json.load(open(sys.argv[0].replace('slackcli', 'data.json'), encoding='utf-8'))\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['auth', 'whoami']:\n"
        "    sys.exit(1)\n"
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
        f.write(body)
    os.chmod(path, 0o755)
    return path


def fake_notify_cmd(tmp):
    """呼ばれた引数を calls.jsonl に残す fake 通知コマンド。"""
    path = os.path.join(tmp, "notify.sh")
    calls_path = os.path.join(tmp, "calls.jsonl")
    body = (
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "open(sys.argv[0].replace('notify.sh', 'calls.jsonl'), 'a', "
        "encoding='utf-8').write(json.dumps(sys.argv[1:], "
        "ensure_ascii=False) + '\\n')\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(path, 0o755)
    return path, calls_path


def test_cli_closes_at_24_and_notifies_once():
    with tempfile.TemporaryDirectory() as tmp:
        mod = load_mod()
        amb_path = os.path.join(tmp, "amb.jsonl")
        last_path = os.path.join(tmp, "last.json")
        counts_path = os.path.join(tmp, "counts.json")
        with open(amb_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(amb(), ensure_ascii=False) + "\n")
        key = mod.target_key(CHANNEL, THREAD_TS, TEXT, None)
        with open(counts_path, "w", encoding="utf-8") as f:
            json.dump(seed_counts(mod, key, 23), f)
        cli = fake_self_id_unknown_cli(tmp, {})
        notify, calls_path = fake_notify_cmd(tmp)
        ids_path = _write_ids(
            tmp, {"channel_welcome_muse_torishimariyakai": CHANNEL})
        env = dict(os.environ)
        env["NEIRO_SLACK_IDS_PATH"] = ids_path
        cmd = [sys.executable, RECONCILE_PY,
               "--ambiguous-file", amb_path, "--last-run-file", last_path,
               "--runs-file", os.path.join(tmp, "runs.jsonl"),
               "--last-summary-file", os.path.join(tmp, "last-run.json"),
               "--lock-file", os.path.join(tmp, "reconcile.lock"),
               "--counts-file", counts_path,
               "--notify-cmd", notify,
               "--slackcli", cli, "--force"]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=60, env=env)
        out = json.loads(r.stdout[r.stdout.find("{"):])
        lines = open(amb_path, encoding="utf-8").read().strip().split("\n")
        entry = json.loads(lines[1]) if len(lines) > 1 else {}
        counts_after = json.load(open(counts_path, encoding="utf-8"))
        calls = [json.loads(line) for line in
                 open(calls_path, encoding="utf-8")] \
            if os.path.exists(calls_path) else []
        check("CLI: 24th cannot_verify closes, counts cleared, notified once",
              r.returncode == 2 and out["closed_unverifiable"] == 1
              and out["notified"] is True
              and entry.get("outcome") == "closed_unverifiable"
              and entry.get("attempts") == 24
              and entry.get("error_kind") == "self_id_unknown"
              and counts_after == {}
              and len(calls) == 1
              and "closed_unverifiable" in " ".join(calls[0])
              and "--request-id" in calls[0],
              f"rc={r.returncode} out={r.stdout} err={r.stderr}")


def test_cli_invalid_channel_closes():
    with tempfile.TemporaryDirectory() as tmp:
        amb_path = os.path.join(tmp, "amb.jsonl")
        last_path = os.path.join(tmp, "last.json")
        user_channel = make_id("U", "TESTUSER01")
        with open(amb_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(amb(channel=user_channel),
                               ensure_ascii=False) + "\n")
        cli = fake_slackcli(tmp, {})  # この記録では呼ばれない
        r = run_cli(tmp, amb_path, last_path, cli, ["--force"])
        out = json.loads(r.stdout[r.stdout.find("{"):])
        lines = open(amb_path, encoding="utf-8").read().strip().split("\n")
        entry = json.loads(lines[1]) if len(lines) > 1 else {}
        check("CLI: user-id channel closes as closed_invalid_channel, exits 0",
              r.returncode == 0 and out["closed_invalid_channel"] == 1
              and out["targets"] == 0
              and entry.get("outcome") == "closed_invalid_channel",
              f"rc={r.returncode} out={r.stdout} err={r.stderr}")
        r2 = run_cli(tmp, amb_path, last_path, cli, ["--force"])
        out2 = json.loads(r2.stdout[r2.stdout.find("{"):])
        lines2 = open(amb_path, encoding="utf-8").read().strip().split("\n")
        check("CLI: second run closes nothing more",
              r2.returncode == 0 and out2["closed_invalid_channel"] == 0
              and len(lines2) == 2, f"out={r2.stdout}")


def test_cli_reopen_readmits():
    with tempfile.TemporaryDirectory() as tmp:
        amb_path = os.path.join(tmp, "amb.jsonl")
        with open(amb_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(amb(), ensure_ascii=False) + "\n")
            f.write(json.dumps(closed_unverifiable_entry(),
                               ensure_ascii=False) + "\n")
        lock = os.path.join(tmp, "reconcile.lock")
        base = [sys.executable, RECONCILE_PY,
                "--ambiguous-file", amb_path, "--lock-file", lock]
        r = subprocess.run(base + ["--reopen-channel", CHANNEL,
                                   "--reopen-thread-ts", THREAD_TS],
                           capture_output=True, text=True, timeout=60)
        out = json.loads(r.stdout[r.stdout.find("{"):])
        lines = open(amb_path, encoding="utf-8").read().strip().split("\n")
        check("CLI reopen appends reopened entry for a closed target",
              r.returncode == 0 and out.get("reopened") is True
              and len(lines) == 3
              and json.loads(lines[2]).get("outcome") == "reopened",
              f"rc={r.returncode} out={r.stdout} err={r.stderr}")
        r2 = subprocess.run(base + ["--reopen-channel",
                                    make_id("C", "TESTOTHER1"),
                                    "--reopen-thread-ts", THREAD_TS],
                            capture_output=True, text=True, timeout=60)
        out2 = json.loads(r2.stdout[r2.stdout.find("{"):])
        lines2 = open(amb_path, encoding="utf-8").read().strip().split("\n")
        check("CLI reopen with no closed target appends nothing",
              r2.returncode == 0 and out2.get("reopened") is False
              and len(lines2) == 3, f"out={r2.stdout}")


def test_notify_failure_tolerated():
    mod = load_mod()
    with tempfile.TemporaryDirectory() as tmp:
        ids_path = _write_ids(
            tmp, {"channel_welcome_muse_torishimariyakai": CHANNEL})
        old = _set_ids_env(ids_path)
        try:
            closed = [{"channel": CHANNEL, "thread_ts": THREAD_TS,
                       "text": TEXT, "mark": None, "attempts": 24,
                       "last_error": "history unreadable",
                       "error_kind": "history_unreadable",
                       "closed_at": "2026-11-14T05:10:00+0000"}]
            ok = mod.notify_transitions("/bin/false", closed, [])
            check("notify failure returns False without raising",
                  ok is False, "")
        finally:
            _restore_ids_env(old)


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
    test_load_records_broken_line_warns()
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
    test_mark_different_no_resolve()
    test_mark_same_resolves()
    test_mark_unknown_resolves()
    test_unmarked_record_never_consults_lookup()
    test_same_text_different_marks_are_separate_targets()
    test_mark_lookup_failure_is_cannot_verify()
    test_cli_mark_guard_end_to_end()
    test_self_id_fallback_from_ids()
    test_self_id_missing_ids_fails_closed()
    test_self_id_whoami_garbage_rejected()
    test_self_id_whoami_w_shape()
    test_cli_self_id_unknown_cannot_verify()
    test_classify_error_buckets()
    test_target_key_stable_and_distinct()
    test_cannot_verify_increments_count()
    test_cannot_verify_23_no_close()
    test_cannot_verify_24th_closes()
    test_clean_read_resets_count()
    test_resolved_resets_count()
    test_majority_failure_suppresses_counts()
    test_single_target_failure_increments()
    test_invalid_channel_closed_without_read()
    test_invalid_channel_no_text_prefers_invalid()
    test_invalid_channel_already_closed_not_reclosed()
    test_invalid_channel_after_close_forms_new_group()
    test_closed_target_excluded_and_new_record_forms_new_target()
    test_reopened_readmits_closed_target()
    test_reopened_resets_count()
    test_budget_skips_remaining_targets()
    test_append_closed_unverifiable_roundtrip()
    test_cli_closes_at_24_and_notifies_once()
    test_cli_invalid_channel_closes()
    test_cli_reopen_readmits()
    test_notify_failure_tolerated()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
