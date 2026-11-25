#!/usr/bin/env python3
"""Tests for the done-as-final guard in slack-event-state.sh.

done is a terminal state: once an event is marked done, a later
failed/waiting write must not revive it (2026-11-23, Yanayana's
stagnation fix #4, same shape as the hole Yama's audit found in
Yanayana's mark: a FAILED commit landing on DONE brings it back).
A stale retry that fails after the reply already went out would
otherwise flip a resolved event back to failed and wake the worker
again for nothing.

Style follows the neighbours (plain Python, no pytest):
  python3 ~/hooks/tests/test_slack_event_state_done_guard.py

No real Slack calls, temporary files only. The script under test
reads its state path from SLACK_EVENT_STATE_FILE /
SLACK_EVENT_STATE_LOCK_FILE (test overrides; production defaults
are unchanged).

Cases (EVENT_ID is a synthetic ID, built from parts like the
neighbours' fixtures so no real ID leaks into the public repo):
  1. done then failed -> stays done, last_error not set
  2. done then waiting -> stays done
  3. done then done with a resolution -> stays done, resolution kept
  4. failed then done -> becomes done, last_error cleared (existing)
  5. failed then failed -> stays failed, last_error updated
  6. done then failed -> one rejection-log line (at/key/attempted/
     current/reason); dead then failed -> stays dead and is logged;
     dead then done -> allowed (heal path)
  7. --force done -> waiting with a reason -> succeeds, records
     corrected_from/correction_reason/corrected_at
  8. --force without a reason -> rejected (exit 2), state unchanged
  9. rejection log unwritable -> mark still succeeds (fail-open)
 10. --force correction resets the poller's wait_wakes / reclaim_count
     (2026-11-25: counting restarts only on human re-injection, the
     terminal principle; automatic paths never reset them)
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPT = "/home/hatch/hooks/scripts/slack-event-state.sh"


def make_id(prefix, rest):
    """Build an ID-shaped string from parts (like a real Slack ID).

    Fixtures are synthetic (2026-11-21): the real IDs used before were
    other people's and ours; parts-built fakes keep the tests passing
    without leaking real IDs into the public repo.
    """
    return prefix + "0" + rest


WS = make_id("T", "TESTWSMUSE1")
CHANNEL = make_id("C", "TESTCHAN01")
EVENT_ID = WS + ":" + CHANNEL + ":1795421000.000001"


class DoneGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ)
        self.env["SLACK_EVENT_STATE_FILE"] = os.path.join(
            self.tmp.name, "slack-events.jsonl")
        self.env["SLACK_EVENT_STATE_LOCK_FILE"] = os.path.join(
            self.tmp.name, "slack-events.lock")
        self.env["SLACK_EVENT_MARK_REJECTED_FILE"] = os.path.join(
            self.tmp.name, "mark_rejected.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def mark(self, status, error="", force=False):
        args = [SCRIPT]
        if force:
            args.append("--force")
        args += [EVENT_ID, status]
        if error:
            args.append(error)
        proc = subprocess.run(args, env=self.env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def mark_raw(self, args):
        return subprocess.run([SCRIPT] + args, env=self.env,
                              capture_output=True, text=True)

    def seed(self, status, attempts=0):
        path = self.env["SLACK_EVENT_STATE_FILE"]
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"event_id": EVENT_ID, "status": status,
                                "attempts": attempts,
                                "updated_at": "2026-11-23T00:00:00+00:00"},
                               ensure_ascii=False) + "\n")

    def load_rejected(self):
        path = self.env["SLACK_EVENT_MARK_REJECTED_FILE"]
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def load_state(self):
        path = self.env["SLACK_EVENT_STATE_FILE"]
        if not os.path.exists(path):
            return {}
        state = {}
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    state[rec["event_id"]] = rec
        return state

    def test_failed_after_done_stays_done(self):
        self.mark("done")
        self.mark("failed", "late worker failed to send")
        rec = self.load_state()[EVENT_ID]
        self.assertEqual(rec["status"], "done")
        self.assertNotIn("last_error", rec)

    def test_waiting_after_done_stays_done(self):
        self.mark("done")
        self.mark("waiting", "late cannot-verify")
        rec = self.load_state()[EVENT_ID]
        self.assertEqual(rec["status"], "done")
        self.assertNotIn("last_error", rec)

    def test_done_after_done_keeps_resolution(self):
        self.mark("done")
        self.mark("done", "answered by someone else first")
        rec = self.load_state()[EVENT_ID]
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec.get("resolution"),
                         "answered by someone else first")

    def test_done_after_failed_clears_error(self):
        self.mark("failed", "send failed")
        self.mark("done")
        rec = self.load_state()[EVENT_ID]
        self.assertEqual(rec["status"], "done")
        self.assertNotIn("last_error", rec)

    def test_failed_after_failed_updates_error(self):
        self.mark("failed", "first failure")
        self.mark("failed", "second failure")
        rec = self.load_state()[EVENT_ID]
        self.assertEqual(rec["status"], "failed")
        self.assertEqual(rec.get("last_error"), "second failure")

    def test_rejected_write_is_logged(self):
        self.mark("done")
        self.mark("failed", "late worker failed to send")
        rows = self.load_rejected()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["key"], EVENT_ID)
        self.assertEqual(row["attempted_status"], "failed")
        self.assertEqual(row["current_status"], "done")
        self.assertIn("at", row)
        self.assertIn("reason", row)

    def test_dead_is_terminal_for_failed_but_done_allowed(self):
        self.seed("dead", attempts=3)
        self.mark("failed", "late worker failed to send")
        rec = self.load_state()[EVENT_ID]
        self.assertEqual(rec["status"], "dead")
        rows = self.load_rejected()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["current_status"], "dead")
        self.mark("done", "healed: reply already sent")
        rec = self.load_state()[EVENT_ID]
        self.assertEqual(rec["status"], "done")

    def test_force_correction_from_done_needs_reason_and_records_it(self):
        self.mark("done")
        proc = self.mark_raw(["--force", EVENT_ID, "waiting"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.load_state()[EVENT_ID]["status"], "done")
        out = self.mark("waiting", "mistaken done, reopening", force=True)
        self.assertIn("CORRECTION", out)
        rec = self.load_state()[EVENT_ID]
        self.assertEqual(rec["status"], "waiting")
        self.assertEqual(rec.get("corrected_from"), "done")
        self.assertEqual(rec.get("correction_reason"),
                         "mistaken done, reopening")
        self.assertIn("corrected_at", rec)
        self.assertEqual(rec.get("last_error"), "mistaken done, reopening")
        self.assertEqual(self.load_rejected(), [])

    def test_force_correction_resets_poller_counters(self):
        # 人間の再投入（--force 訂正）ではポーラーのカウンタを数え直す。
        # waiting 段階化（wait_wakes）と取り戻し打ち切り（reclaim_count）
        # のカウンタは、自動経路では決してリセットされない（終端原則）。
        path = self.env["SLACK_EVENT_STATE_FILE"]
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"event_id": EVENT_ID, "status": "done",
                                "attempts": 1, "wait_wakes": 4,
                                "reclaim_count": 7,
                                "updated_at": "2026-11-25T00:00:00+00:00"},
                               ensure_ascii=False) + "\n")
        self.mark("waiting", "reopening by hand", force=True)
        rec = self.load_state()[EVENT_ID]
        self.assertEqual(rec["status"], "waiting")
        self.assertNotIn("wait_wakes", rec)
        self.assertNotIn("reclaim_count", rec)

    def test_rejected_log_failure_does_not_break_mark(self):
        self.mark("done")
        # Point the log at a directory: the append must fail open.
        self.env["SLACK_EVENT_MARK_REJECTED_FILE"] = self.tmp.name
        out = self.mark("failed", "late worker failed to send")
        self.assertIn("already done", out)
        self.assertEqual(self.load_state()[EVENT_ID]["status"], "done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
