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
  4. old processing (200s) -> exit 1, oldest id + age reported
  5. boundary: 179s -> exit 0; 181s -> exit 1
  6. oldest wins among several unprocessed events
  7. malformed lines are skipped
  8. missing first_wake_at falls back to updated_at
  9. waiting counts as unprocessed
 10. threshold env override is respected
"""
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

CHECK_PATH = os.path.expanduser(
    "~/hooks/scripts/slack-oldest-unprocessed-check.sh")
EID = "T0CGV6V7161:C0CGYGT9V7G:1794476000.000001"
EID2 = "T0CGV6V7161:C0CGYGT9V7G:1794476001.000001"


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

    # 4. old processing (200s) -> exit 1, oldest reported
    def test_old_processing_warns(self):
        self.write_state([self.rec(age_secs=200)])
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(out["unprocessed_count"], 1)
        self.assertEqual(out["oldest_event_id"], EID)
        self.assertGreaterEqual(out["oldest_age_secs"], 200)

    # 5. boundary: 179s -> exit 0; 181s -> exit 1
    def test_boundary(self):
        self.write_state([self.rec(age_secs=179)])
        code, _ = self.run_check()
        self.assertEqual(code, 0)
        self.write_state([self.rec(age_secs=181)])
        code, _ = self.run_check()
        self.assertEqual(code, 1)

    # 6. oldest wins among several unprocessed events
    def test_oldest_wins(self):
        self.write_state([self.rec(age_secs=60),
                          self.rec(eid=EID2, age_secs=300),
                          self.rec(eid="T0CGV6V7161:C0CGYGT9V7G:1794476002.000001",
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
            f.write(json.dumps(self.rec(age_secs=200),
                               ensure_ascii=False) + "\n")
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(out["unprocessed_count"], 1)

    # 8. missing first_wake_at falls back to updated_at
    def test_first_wake_missing_falls_back(self):
        self.write_state([self.rec(age_secs=200, first_wake=False)])
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
        self.write_state([self.rec(age_secs=90)])
        code, _ = self.run_check()
        self.assertEqual(code, 0)
        self.env["SLACK_OLDEST_UNPROCESSED_THRESHOLD_SECS"] = "60"
        code, out = self.run_check()
        self.assertEqual(code, 1)
        self.assertEqual(out["threshold_secs"], 60)


if __name__ == "__main__":
    unittest.main(verbosity=2)
