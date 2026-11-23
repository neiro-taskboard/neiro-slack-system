#!/usr/bin/env python3
"""Tests for slack-reconcile-heal.py — heal-replied (Yanayana ②).

Style follows test_slack_reconcile_ambiguous.py (plain Python, no
pytest): python3 test_slack_reconcile_heal.py

The real slack-event-state.sh is used for the heal itself, with
SLACK_EVENT_STATE_FILE / SLACK_EVENT_STATE_LOCK_FILE pointed at temp
files by the heal script, so no production state is touched.

Covered:
  1. pending + ledger event_id -> healed (done, heal-replied resolution)
  2. failed + ledger event_id -> healed
  3. pending without a ledger record -> stays pending
  4. done with a ledger record -> untouched (not a target)
  5. processing / waiting / dead with a ledger record -> untouched
  6. dry-run heals nothing and does not update last-run
  7. self-gate: run within 1h is skipped; --force runs anyway
  8. ledger file missing -> heals nothing, exits 0
  9. ledger unreadable (a directory) -> exit 2, nothing healed
  10. event-state failure for one event -> cannot_verify, exit 2,
      the other heal is still attempted
  11. lock busy -> gated (lock_busy), nothing healed
  12. CLI end-to-end with the real event-state script: --force heals
      and exits 0, the state file shows done with the resolution
"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


def make_id(prefix, rest):
    """Build an ID-shaped string from parts (like a real Slack ID).

    Fixtures are synthetic (2026-11-21): the real IDs used before were
    other people's and ours; parts-built fakes keep the tests passing
    without leaking real IDs into the public repo.
    """
    return prefix + "0" + rest


WS_MUSE = make_id("T", "TESTWSMUSE1")
CHANNEL = make_id("C", "TESTCHAN01")
OTHER_CHANNEL = make_id("C", "TESTCHAN02")

HEAL_PATH = os.path.expanduser(
    "~/workspace/neiro-slack-system/hooks/scripts/slack-reconcile-heal.py")
EVENT_STATE_PATH = os.path.expanduser(
    "~/workspace/neiro-slack-system/hooks/scripts/slack-event-state.sh")


def load_heal():
    spec = importlib.util.spec_from_file_location("slack_reconcile_heal",
                                                  HEAL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def eid(channel, ts):
    return WS_MUSE + ":" + channel + ":" + ts


class HealTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mod = load_heal()
        self.state = os.path.join(self.tmp.name, "state.jsonl")
        self.ledger = os.path.join(self.tmp.name, "ledger.jsonl")
        self.state_lock = os.path.join(self.tmp.name, "state.lock")
        self.last_run = os.path.join(self.tmp.name, "last.json")
        self.runs = os.path.join(self.tmp.name, "runs.jsonl")
        self.summary = os.path.join(self.tmp.name, "summary.json")
        self.lock = os.path.join(self.tmp.name, "heal.lock")

    def tearDown(self):
        self.tmp.cleanup()

    def write_state(self, recs):
        with open(self.state, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def write_ledger(self, recs):
        with open(self.ledger, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def load_state(self):
        state = {}
        with open(self.state, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                state[rec["event_id"]] = rec
        return state

    def base_args(self, extra=None):
        args = ["--state-file", self.state, "--ledger-file", self.ledger,
                "--state-lock-file", self.state_lock,
                "--last-run-file", self.last_run, "--runs-file", self.runs,
                "--last-summary-file", self.summary, "--lock-file", self.lock,
                "--event-state", EVENT_STATE_PATH, "--force"]
        if extra:
            args += extra
        return args

    def run_main(self, extra=None):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = self.mod.main(self.base_args(extra))
        return code, json.loads(buf.getvalue())

    # 1. pending + ledger event_id -> healed
    def test_pending_with_ledger_record_healed(self):
        target = eid(CHANNEL, "1794476000.000001")
        self.write_state([{"event_id": target, "status": "pending",
                           "attempts": 1}])
        self.write_ledger([{"ts": "1794476100.000001", "channel": CHANNEL,
                            "thread_ts": "", "text": "hi",
                            "event_id": target}])
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(out["healed"], 1)
        self.assertEqual(out["healed_detail"], [target])
        st = self.load_state()
        self.assertEqual(st[target]["status"], "done")
        self.assertIn("heal-replied", st[target].get("resolution", ""))

    # 2. failed + ledger event_id -> healed
    def test_failed_with_ledger_record_healed(self):
        target = eid(CHANNEL, "1794476000.000002")
        self.write_state([{"event_id": target, "status": "failed",
                           "attempts": 2, "last_error": "boom"}])
        self.write_ledger([{"ts": "1794476100.000002", "channel": CHANNEL,
                            "thread_ts": "", "text": "hi",
                            "event_id": target}])
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(out["healed"], 1)
        st = self.load_state()
        self.assertEqual(st[target]["status"], "done")
        self.assertNotIn("last_error", st[target])

    # 3. pending without a ledger record -> stays pending
    def test_pending_without_ledger_record_untouched(self):
        target = eid(CHANNEL, "1794476000.000003")
        self.write_state([{"event_id": target, "status": "pending",
                           "attempts": 1}])
        self.write_ledger([{"ts": "1794476100.000003", "channel": CHANNEL,
                            "thread_ts": "", "text": "other",
                            "event_id": eid(OTHER_CHANNEL,
                                            "1794476000.000009")}])
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(out["healed"], 0)
        self.assertEqual(out["targets"], 1)
        st = self.load_state()
        self.assertEqual(st[target]["status"], "pending")

    # 4. done with a ledger record -> untouched
    def test_done_with_ledger_record_untouched(self):
        target = eid(CHANNEL, "1794476000.000004")
        self.write_state([{"event_id": target, "status": "done",
                           "attempts": 1}])
        self.write_ledger([{"ts": "1794476100.000004", "channel": CHANNEL,
                            "thread_ts": "", "text": "hi",
                            "event_id": target}])
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(out["healed"], 0)
        self.assertEqual(out["targets"], 0)

    # 5. processing / waiting / dead with a ledger record -> untouched
    def test_other_statuses_with_ledger_record_untouched(self):
        targets = {
            "processing": eid(CHANNEL, "1794476000.000005"),
            "waiting": eid(CHANNEL, "1794476000.000006"),
            "dead": eid(CHANNEL, "1794476000.000007"),
        }
        self.write_state([{"event_id": e, "status": s, "attempts": 1}
                          for s, e in targets.items()])
        self.write_ledger([{"ts": "1794476100.00000" + str(i),
                            "channel": CHANNEL, "thread_ts": "",
                            "text": "hi", "event_id": e}
                           for i, e in enumerate(targets.values())])
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(out["healed"], 0)
        self.assertEqual(out["targets"], 0)
        st = self.load_state()
        for s, e in targets.items():
            self.assertEqual(st[e]["status"], s)

    # 6. dry-run heals nothing and does not update last-run
    def test_dry_run_heals_nothing(self):
        target = eid(CHANNEL, "1794476000.000008")
        self.write_state([{"event_id": target, "status": "pending",
                           "attempts": 1}])
        self.write_ledger([{"ts": "1794476100.000008", "channel": CHANNEL,
                            "thread_ts": "", "text": "hi",
                            "event_id": target}])
        code, out = self.run_main(extra=["--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(out["healed"], 1)
        self.assertTrue(out["dry_run"])
        st = self.load_state()
        self.assertEqual(st[target]["status"], "pending")
        self.assertFalse(os.path.exists(self.last_run))

    # 7. self-gate: run within 1h is skipped; --force runs anyway
    def test_self_gate_and_force(self):
        target = eid(CHANNEL, "1794476000.000010")
        self.write_state([{"event_id": target, "status": "pending",
                           "attempts": 1}])
        self.write_ledger([{"ts": "1794476100.000010", "channel": CHANNEL,
                            "thread_ts": "", "text": "hi",
                            "event_id": target}])
        # First --force run heals.
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(out["healed"], 1)
        # Second run without --force is gated (last-run is fresh).
        args = self.base_args()
        args.remove("--force")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code2 = self.mod.main(args)
        out2 = json.loads(buf.getvalue())
        self.assertEqual(code2, 0)
        self.assertTrue(out2["gated"])
        # --force runs anyway (nothing left to heal now).
        code3, out3 = self.run_main()
        self.assertEqual(code3, 0)
        self.assertFalse(out3["gated"])
        self.assertEqual(out3["healed"], 0)

    # 8. ledger file missing -> heals nothing, exits 0
    def test_missing_ledger_file_heals_nothing(self):
        target = eid(CHANNEL, "1794476000.000011")
        self.write_state([{"event_id": target, "status": "pending",
                           "attempts": 1}])
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(out["healed"], 0)
        st = self.load_state()
        self.assertEqual(st[target]["status"], "pending")

    # 9. ledger unreadable (a directory) -> exit 2, nothing healed
    def test_unreadable_ledger_fails_closed(self):
        target = eid(CHANNEL, "1794476000.000012")
        self.write_state([{"event_id": target, "status": "pending",
                           "attempts": 1}])
        args = self.base_args()
        args[args.index("--ledger-file") + 1] = self.tmp.name
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = self.mod.main(args)
        self.assertEqual(code, 2)
        st = self.load_state()
        self.assertEqual(st[target]["status"], "pending")

    # 10. event-state failure for one event -> cannot_verify, exit 2,
    #     the other heal is still attempted
    def test_event_state_failure_marks_cannot_verify(self):
        good = eid(CHANNEL, "1794476000.000013")
        bad = eid(CHANNEL, "1794476000.000014")
        self.write_state([{"event_id": good, "status": "pending",
                           "attempts": 1},
                          {"event_id": bad, "status": "failed",
                           "attempts": 2}])
        self.write_ledger([{"ts": "1794476100.000013", "channel": CHANNEL,
                            "thread_ts": "", "text": "hi", "event_id": good},
                           {"ts": "1794476100.000014", "channel": CHANNEL,
                            "thread_ts": "", "text": "hi", "event_id": bad}])
        fake = os.path.join(self.tmp.name, "fake-event-state.sh")
        with open(fake, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\n")
            f.write("if [ \"$1\" = \"%s\" ]; then exit 1; fi\n" % bad)
            f.write("exec \"%s\" \"$@\"\n" % EVENT_STATE_PATH)
        os.chmod(fake, 0o755)
        args = self.base_args()
        args[args.index("--event-state") + 1] = fake
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = self.mod.main(args)
        out = json.loads(buf.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(out["healed"], 1)
        self.assertEqual(len(out["cannot_verify"]), 1)
        self.assertEqual(out["cannot_verify"][0]["event_id"], bad)
        st = self.load_state()
        self.assertEqual(st[good]["status"], "done")
        self.assertEqual(st[bad]["status"], "failed")

    # 11. lock busy -> gated (lock_busy), nothing healed
    def test_lock_busy_gated(self):
        if fcntl is None:
            self.skipTest("fcntl unavailable")
        target = eid(CHANNEL, "1794476000.000015")
        self.write_state([{"event_id": target, "status": "pending",
                           "attempts": 1}])
        self.write_ledger([{"ts": "1794476100.000015", "channel": CHANNEL,
                            "thread_ts": "", "text": "hi",
                            "event_id": target}])
        fp = open(self.lock, "a+", encoding="utf-8")
        try:
            fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, out = self.run_main()
        finally:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            fp.close()
        self.assertEqual(code, 0)
        self.assertTrue(out["gated"])
        self.assertEqual(out.get("reason"), "lock_busy")
        st = self.load_state()
        self.assertEqual(st[target]["status"], "pending")

    # 12. CLI end-to-end with the real event-state script
    def test_cli_end_to_end(self):
        target = eid(CHANNEL, "1794476000.000016")
        self.write_state([{"event_id": target, "status": "pending",
                           "attempts": 1}])
        self.write_ledger([{"ts": "1794476100.000016", "channel": CHANNEL,
                            "thread_ts": "", "text": "hi",
                            "event_id": target}])
        proc = subprocess.run(
            [sys.executable, HEAL_PATH] + self.base_args(),
            capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["healed"], 1)
        st = self.load_state()
        self.assertEqual(st[target]["status"], "done")
        self.assertIn("heal-replied", st[target].get("resolution", ""))
        with open(self.summary, encoding="utf-8") as f:
            summary = json.load(f)
        self.assertEqual(summary["healed"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
