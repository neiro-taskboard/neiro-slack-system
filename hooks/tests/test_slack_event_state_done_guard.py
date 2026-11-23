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

    def tearDown(self):
        self.tmp.cleanup()

    def mark(self, status, error=""):
        args = [SCRIPT, EVENT_ID, status]
        if error:
            args.append(error)
        proc = subprocess.run(args, env=self.env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
