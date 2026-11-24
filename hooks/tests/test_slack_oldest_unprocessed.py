#!/usr/bin/env python3
"""E2E tests for slack-oldest-unprocessed-check.sh — real script,
isolated state files.

Style follows the neighbours (Yanayana: E2E with isolated state files
instead of fake clients; Yama: one plain Python script, no pytest):
  python3 ~/hooks/tests/test_slack_oldest_unprocessed.py

Each test runs the real check script as a subprocess with
SLACK_OLDEST_UNPROCESSED_STATE_FILE pointed at a temp file, so the
live state is never touched.

Covered:
  1. state file missing -> exit 0, 0 unprocessed, oldest null
  2. all done/dead -> exit 0, 0 unprocessed
  3. young processing (60s) -> exit 0, counted, not warned
  4. processing below its own threshold (900s < 1800s) -> exit 0
  5. boundary for pending: 179s -> exit 0; 181s -> exit 1
  6. oldest wins among several unprocessed events
  7. malformed lines are skipped
  8. missing first_wake_at falls back to updated_at
  9. waiting counts as unprocessed
 10. threshold env override is respected
 11. processing above its own threshold (650s) -> exit 1
 12. processing boundary: 599s -> exit 0; 601s -> exit 1
 13. mixed: processing 400s (under) + pending 200s (over) -> exit 1,
      oldest is the pending one that exceeded its threshold
 14. processing threshold env override is respected
 15. processing_count is reported (2026-11-24, Yanayana's suggestion):
     mixed processing + pending, and all-pending -> 0
"""
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

def make_id(prefix, rest):
    """Build an ID-shaped string from parts (like a real Slack ID).

    Fixtures are synthetic (2026-11-21): the real IDs used before were
    other people's and ours; parts-built fakes keep the tests passing
    without leaking real IDs into the public repo.
    """
    return prefix + "0" + rest


WS_MUSE = make_id("T", "TESTWSMUSE1")
CHANNEL = make_id("C", "TESTCHAN01")


CHECK_PATH = os.path.expanduser(
    "~/hooks/scripts/slack-oldest-unprocessed-check.sh")
EID = WS_MUSE + ":" + CHANNEL + ":1794476000.000001"
EID2 = WS_MUSE + ":" + CHANNEL + ":1794476001.000001"


def iso(dt):
    return dt.isoformat()


class OldestUnprocessedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "slack-events.jsonl")
        self.now = datetime.now(timezone.utc)
        self.env = dict(os.environ)
        self.env["SLACK_OLDEST_UNPROCESSED_STATE_FILE"] = self.state
        self.env.pop("SLACK_OLDEST_UNPROCESSED_THRESHOLD_SECS", None)
        self.env.pop(
            "SLACK_OLDEST_UNPROCESSED_PROCESSING_THRESHOLD_SECS", None)

    def tearDown(self):
        self.tmp.cleanup()

    def write_state(self, recs):
        with open(self.state, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def run_check(self):
        proc = subprocess.run(
            ["bash", CHECK_PATH], env=self.env,
            capture_output=True, text=True, timeout=30)
        return proc.returncode, json.loads(proc.stdout)

    def rec(self, eid=EID, status="processing", age_secs=0,
            first_wake=True):
        r = {"event_id": eid, "status": status, "attempts": 1,
             "updated_at": iso(self.now - timedelta(seconds=age_secs))}
        if first_wake:
            r["first_wake_at"] = iso(self.now - timedelta(seconds=age_secs))
        return r

    # 1. state file missing -> exit 0, 0 unprocessed
    def test_missing_state_file(self):
        code, out = self.run_check()
        self.assertEqual(code, 0)
        self.assertEqual(out["unprocessed_count"], 0)
        self.assertIsNone(out["oldest_event_id"])
        self.assertIsNone(out["oldest_age_secs"])

    # 2. all done/dead -> exit 0, 0 unprocessed
    def test_all_done(self):
        self.write_state([self.rec(status="done", age_secs=900),
                          self.rec(eid=EID2, status="dead", age_secs=900)])
        code, out = self.run_check()
        self.assertEqual(code, 0)
        self.assertEqual(out["unprocessed_count"], 0)

    # 3. young processing (60s) -> exit 0, counted
    def test_young_processing_no_warning(self):
        self.write_state([self.rec(age_secs=60)])
        code, out = self.run_check()
        self.assertEqual(code, 0)
        self.assertEqual(out["unprocessed_count"], 1)
        self.assertEqual(out["oldest_event_id"], EID)
        self.assertGreaterEqual(out["oldest_age_secs"], 60)

    # 4. processing below its own threshold (900s < 1800s) -> exit 0.
    # A worker holding an event while it works through a busy queue
    # (2026-11-24: 10 events held, oldest 770s, all processing, six
    # alerts in an afternoon) must not warn.
    def test_processing_below_own_threshold_no_warning(self):
        self.write_state([self.rec(age_secs=900)])
        code, out = self.run_check()
        self.assertEqual(code, 0)
        self.assertEqual(out["unprocessed_count"], 1)
        self.assertEqual(out["oldest_event_id"], EID)
        self.assertGreaterEqual(out["oldest_age_secs"], 900)
        self.assertEqual(out["oldest_threshold_secs"], 1800)

    # 5. boundary for pending: 179s -> exit 0; 181s -> exit 1
    def test_boundary(self):
        self.write_state([self.rec(status="pending", age_secs=179)])
        code, _ = self.run_check()
        self.assertEqual(code, 0)
        self.write_state([self.rec(status="pending", age_secs=181)])
        code, _ = self.run_check()
        self.assertEqual(code, 1)

    # 6. oldest wins among several unprocessed events
    def test_oldest_wins(self):
        self.write_state([self.rec(status="pending", age_secs=60),
                          self.rec(eid=EID2, status="pending", age_secs=300),
                          self.rec(eid=WS_MUSE + ":" + CHANNEL + ":1794476002.000001",
                                   status="done", age_secs=900)])
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(out["unprocessed_count"], 2)
        self.assertEqual(out["oldest_event_id"], EID2)
        self.assertGreaterEqual(out["oldest_age_secs"], 300)

    # 7. malformed lines are skipped
    def test_malformed_lines_skipped(self):
        with open(self.state, "w", encoding="utf-8") as f:
            f.write("{broken\n")
            f.write(json.dumps(self.rec(status="pending", age_secs=200),
                               ensure_ascii=False) + "\n")
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(out["unprocessed_count"], 1)

    # 8. missing first_wake_at falls back to updated_at
    def test_first_wake_missing_falls_back(self):
        self.write_state([self.rec(status="pending", age_secs=200,
                                   first_wake=False)])
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(out["oldest_event_id"], EID)

    # 9. waiting counts as unprocessed
    def test_waiting_counts(self):
        self.write_state([self.rec(status="waiting", age_secs=200)])
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(out["unprocessed_count"], 1)

    # 10. threshold env override is respected
    def test_threshold_override(self):
        self.write_state([self.rec(status="pending", age_secs=90)])
        code, _ = self.run_check()
        self.assertEqual(code, 0)
        self.env["SLACK_OLDEST_UNPROCESSED_THRESHOLD_SECS"] = "60"
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(out["threshold_secs"], 60)

    # 11. processing above its own threshold (1900s) -> exit 1.
    # A stuck worker still alerts, just later.
    def test_old_processing_warns(self):
        self.write_state([self.rec(age_secs=1900)])
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(out["unprocessed_count"], 1)
        self.assertEqual(out["oldest_event_id"], EID)
        self.assertGreaterEqual(out["oldest_age_secs"], 1900)
        self.assertEqual(out["oldest_threshold_secs"], 1800)

    # 12. processing boundary: 1799s -> exit 0; 1801s -> exit 1
    def test_processing_boundary(self):
        self.write_state([self.rec(age_secs=1799)])
        code, _ = self.run_check()
        self.assertEqual(code, 0)
        self.write_state([self.rec(age_secs=1801)])
        code, _ = self.run_check()
        self.assertEqual(code, 1)

    # 13. mixed: processing 400s is under its threshold, but a
    # pending 200s event exceeds 180s -> exit 1, and the oldest
    # reported is the pending one that actually exceeded.
    def test_mixed_oldest_is_exceeding_one(self):
        self.write_state([self.rec(age_secs=400),
                          self.rec(eid=EID2, status="pending",
                                   age_secs=200)])
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(out["unprocessed_count"], 2)
        self.assertEqual(out["oldest_event_id"], EID2)
        self.assertEqual(out["oldest_threshold_secs"], 180)

    # 14. processing threshold env override is respected
    def test_processing_threshold_override(self):
        self.write_state([self.rec(age_secs=400)])
        code, _ = self.run_check()
        self.assertEqual(code, 0)
        self.env["SLACK_OLDEST_UNPROCESSED_PROCESSING_THRESHOLD_SECS"] = "300"
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(out["processing_threshold_secs"], 300)

    # 15. processing_count is reported (2026-11-24, Yanayana's
    # suggestion for the alert text): mixed processing + pending
    # counts only the processing ones, all-pending reports 0, and
    # done/dead records are not counted in either number.
    def test_processing_count_reported(self):
        self.write_state([self.rec(age_secs=650),
                          self.rec(eid=EID2, status="pending",
                                   age_secs=200),
                          self.rec(eid=WS_MUSE + ":" + CHANNEL + ":1794476002.000001",
                                   status="done", age_secs=900)])
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(out["unprocessed_count"], 2)
        self.assertEqual(out["processing_count"], 1)
        self.write_state([self.rec(status="pending", age_secs=200),
                          self.rec(eid=EID2, status="waiting",
                                   age_secs=200)])
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(out["unprocessed_count"], 2)
        self.assertEqual(out["processing_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
