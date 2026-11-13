#!/usr/bin/env python3
"""Tests for ~/hooks/scripts/slack-poll.py.

Mirrors the approach Yama shared in the muse取締役会 thread
(#slack-へようこそ, 2026-11-12): no real Slack calls, temporary
files only. Run with: python3 run_tests.py
"""
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

POLL_PATH = os.path.expanduser("~/hooks/scripts/slack-poll.py")


def load_poll():
    spec = importlib.util.spec_from_file_location("slack_poll", POLL_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def iso(dt):
    return dt.isoformat()


class PollTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mod = load_poll()
        self.mod.INBOX = os.path.join(self.tmp.name, "inbox.jsonl")
        self.mod.STATE_FILE = os.path.join(self.tmp.name, "state.jsonl")
        self.mod.DEAD_FILE = os.path.join(self.tmp.name, "dead.jsonl")
        self.mod.STATS_FILE = os.path.join(self.tmp.name, "stats.json")
        self.now = datetime.now(timezone.utc)
        os.environ.pop("SLACK_POLL_DRY_RUN", None)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("SLACK_POLL_DRY_RUN", None)

    def write_inbox(self, events):
        with open(self.mod.INBOX, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")

    def write_state(self, recs):
        with open(self.mod.STATE_FILE, "w", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def run_main(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.mod.main()
        return json.loads(buf.getvalue())

    def load_state(self):
        state = {}
        with open(self.mod.STATE_FILE, encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                state[rec["event_id"]] = rec
        return state

    def ev(self, ts="1794476000.000001"):
        return {"workspace_id": "T0CGV6V7161", "channel": "C0CGYGT9V7G",
                "ts": ts, "text": "test"}

    # 1. 新規イベント -> processing, attempts=1, wake
    def test_new_event_wakes(self):
        self.write_inbox([self.ev()])
        out = self.run_main()
        self.assertEqual(out["decision"], "wake")
        self.assertEqual(len(out["payload"]["events"]), 1)
        st = self.load_state()
        rec = st["T0CGV6V7161:C0CGYGT9V7G:1794476000.000001"]
        self.assertEqual(rec["status"], "processing")
        self.assertEqual(rec["attempts"], 1)

    # 2. done はスキップ
    def test_done_skipped(self):
        self.write_inbox([self.ev()])
        self.write_state([{"event_id": "T0CGV6V7161:C0CGYGT9V7G:1794476000.000001",
                           "status": "done", "attempts": 1,
                           "updated_at": iso(self.now)}])
        out = self.run_main()
        self.assertEqual(out["decision"], "silent")

    # 3. dead はスキップ
    def test_dead_skipped(self):
        self.write_inbox([self.ev()])
        self.write_state([{"event_id": "T0CGV6V7161:C0CGYGT9V7G:1794476000.000001",
                           "status": "dead", "attempts": 3,
                           "updated_at": iso(self.now)}])
        out = self.run_main()
        self.assertEqual(out["decision"], "silent")

    # 4. stale processing (attempts<3) -> attempts+1 で再 wake
    def test_stale_processing_retried(self):
        self.write_inbox([self.ev()])
        self.write_state([{"event_id": "T0CGV6V7161:C0CGYGT9V7G:1794476000.000001",
                           "status": "processing", "attempts": 1,
                           "updated_at": iso(self.now - timedelta(minutes=20))}])
        out = self.run_main()
        self.assertEqual(out["decision"], "wake")
        st = self.load_state()
        rec = st["T0CGV6V7161:C0CGYGT9V7G:1794476000.000001"]
        self.assertEqual(rec["status"], "processing")
        self.assertEqual(rec["attempts"], 2)

    # 5. stale processing (attempts>=3) -> dead + デッドレター
    def test_stale_processing_dead_after_max(self):
        self.write_inbox([self.ev()])
        self.write_state([{"event_id": "T0CGV6V7161:C0CGYGT9V7G:1794476000.000001",
                           "status": "processing", "attempts": 3,
                           "updated_at": iso(self.now - timedelta(minutes=20))}])
        out = self.run_main()
        self.assertEqual(out["decision"], "wake")
        self.assertEqual(len(out["payload"]["dead_letter"]), 1)
        st = self.load_state()
        self.assertEqual(st["T0CGV6V7161:C0CGYGT9V7G:1794476000.000001"]["status"], "dead")
        with open(self.mod.DEAD_FILE, encoding="utf-8") as f:
            dead = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(dead), 1)

    # 6. failed (stale) -> attempts<3 なら再 wake
    def test_failed_retried(self):
        self.write_inbox([self.ev()])
        self.write_state([{"event_id": "T0CGV6V7161:C0CGYGT9V7G:1794476000.000001",
                           "status": "failed", "attempts": 2,
                           "updated_at": iso(self.now - timedelta(minutes=20)),
                           "last_error": "boom"}])
        out = self.run_main()
        self.assertEqual(out["decision"], "wake")
        st = self.load_state()
        self.assertEqual(st["T0CGV6V7161:C0CGYGT9V7G:1794476000.000001"]["attempts"], 3)

    # 7. processing でタイムアウト内 -> そのまま
    def test_fresh_processing_left_alone(self):
        self.write_inbox([self.ev()])
        self.write_state([{"event_id": "T0CGV6V7161:C0CGYGT9V7G:1794476000.000001",
                           "status": "processing", "attempts": 1,
                           "updated_at": iso(self.now - timedelta(minutes=1))}])
        out = self.run_main()
        self.assertEqual(out["decision"], "silent")

    # 8. 不正な JSON 行はスキップして処理継続
    def test_malformed_lines_skipped(self):
        with open(self.mod.INBOX, "w", encoding="utf-8") as f:
            f.write("not json\n")
            f.write(json.dumps(self.ev(), ensure_ascii=False) + "\n")
        with open(self.mod.STATE_FILE, "w", encoding="utf-8") as f:
            f.write("{broken\n")
        out = self.run_main()
        self.assertEqual(out["decision"], "wake")
        self.assertEqual(len(out["payload"]["events"]), 1)

    # 9. dry_run では state を書き込まない
    def test_dry_run_does_not_write_state(self):
        os.environ["SLACK_POLL_DRY_RUN"] = "1"
        self.write_inbox([self.ev()])
        out = self.run_main()
        self.assertEqual(out["decision"], "wake")
        self.assertFalse(os.path.exists(self.mod.STATE_FILE))

    # 10. event_id の形式 workspace:channel:ts
    def test_event_id_format(self):
        ev = {"workspace_id": "W1", "channel": "C1", "ts": "123.456"}
        self.assertEqual(self.mod.event_id(ev), "W1:C1:123.456")


if __name__ == "__main__":
    unittest.main(verbosity=2)
