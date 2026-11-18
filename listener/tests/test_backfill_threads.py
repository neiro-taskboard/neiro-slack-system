#!/usr/bin/env python3
"""Tests for thread-following backfill (backfill.py) and seeding
(auto-join.py), added 2026-11-18 with Yanayana's thread-following
approach.

No real Slack calls: fetch_history / fetch_thread / run_slackcli are
stubbed, and all files live in a temporary directory.
Run with: python3 test_backfill_threads.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest

LISTENER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module(name, filename):
    path = os.path.join(LISTENER_DIR, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


backfill = load_module("backfill_under_test", "backfill.py")
auto_join = load_module("auto_join_under_test", "auto-join.py")

WID = "T0TEST"
CHAN = "C0TEST"
OWN = "U0OWN"
OTHER = "U0OTHER"


def ev(ts, user=OTHER, thread_ts="", text="hello", bot_id="B0X"):
    return {
        "workspace_id": WID,
        "workspace_name": "test",
        "identity": "bot",
        "channel": CHAN,
        "channel_type": "channel",
        "user": user,
        "bot_id": bot_id,
        "text": text,
        "ts": ts,
        "thread_ts": thread_ts,
        "permalink": f"https://example.slack.com/archives/{CHAN}/p{ts}",
        "received_at": "2026-11-18T00:00:00+00:00",
        "backfilled": True,
    }


class BackfillThreadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = os.path.join(self.tmp.name, "inbox.jsonl")
        config = {
            "inbox_path": self.inbox,
            "workspaces": [{
                "workspace_id": WID,
                "workspace_name": "test",
                "slackcli_key": "test-key",
            }],
        }
        self.config_path = os.path.join(self.tmp.name, "config.json")
        with open(self.config_path, "w") as f:
            json.dump(config, f)
        self.store_path = os.path.join(self.tmp.name, "workspaces.json")
        with open(self.store_path, "w") as f:
            json.dump({"workspaces": {"test-key": {"user_id": OWN}}}, f)
        backfill.CONFIG_PATH = self.config_path
        backfill.SLACKCLI_STORE = self.store_path
        self.thread_calls = []
        self.history = []

    def tearDown(self):
        self.tmp.cleanup()

    def write_inbox(self, events):
        with open(self.inbox, "w") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def read_inbox(self):
        with open(self.inbox) as f:
            return [json.loads(line) for line in f if line.strip()]

    def run_main(self):
        backfill.fetch_history = lambda key, ch, limit=20, quiet=False: \
            list(self.history)
        backfill.fetch_thread_messages = lambda ws, ch, pts, oldest=None: (
            self.thread_calls.append((pts, oldest)),
            self.threads.get(pts, []),
        )[1]
        self.assertEqual(backfill.main(), 0)

    def test_missed_thread_reply_is_added(self):
        # Baseline newest is 1010; parent P (1000) has one reply at
        # 1020 that the listener never saw.
        self.write_inbox([ev("1000.000000"), ev("1010.000000")])
        parent = {"ts": "1000.000000", "thread_ts": "1000.000000",
                  "user": OTHER, "text": "parent", "reply_count": 1}
        top = {"ts": "1010.000000", "thread_ts": "", "user": OTHER,
               "text": "top"}
        self.history = [parent, top]
        self.threads = {
            "1000.000000": [
                parent,
                {"ts": "1020.000000", "thread_ts": "1000.000000",
                 "user": OTHER, "text": "missed reply"},
            ]
        }
        self.run_main()
        added = [e for e in self.read_inbox()
                 if e["ts"] == "1020.000000"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["thread_ts"], "1000.000000")
        self.assertTrue(added[0]["backfilled"])
        # The thread read is bounded to replies after the baseline.
        self.assertEqual(self.thread_calls,
                         [("1000.000000", "1010.000000")])

    def test_complete_thread_is_not_refetched(self):
        # Inbox already holds the thread's only reply: no fetch.
        self.write_inbox([ev("1000.000000"), ev("1010.000000"),
                          ev("1005.000000", thread_ts="1000.000000")])
        parent = {"ts": "1000.000000", "thread_ts": "1000.000000",
                  "user": OTHER, "text": "parent", "reply_count": 1}
        self.history = [parent]
        self.threads = {}
        self.run_main()
        self.assertEqual(self.thread_calls, [])
        self.assertEqual(len(self.read_inbox()), 3)

    def test_reply_older_than_baseline_is_skipped(self):
        # Reply posted before the newest inbox event: the floor rejects
        # it even though the thread is re-read.
        self.write_inbox([ev("1000.000000"), ev("1010.000000")])
        parent = {"ts": "1000.000000", "thread_ts": "1000.000000",
                  "user": OTHER, "text": "parent", "reply_count": 1}
        self.history = [parent]
        self.threads = {
            "1000.000000": [
                parent,
                {"ts": "1005.000000", "thread_ts": "1000.000000",
                 "user": OTHER, "text": "too old"},
            ]
        }
        self.run_main()
        self.assertEqual(len(self.read_inbox()), 2)

    def test_own_thread_reply_is_skipped(self):
        self.write_inbox([ev("1000.000000"), ev("1010.000000")])
        parent = {"ts": "1000.000000", "thread_ts": "1000.000000",
                  "user": OTHER, "text": "parent", "reply_count": 1}
        self.history = [parent]
        self.threads = {
            "1000.000000": [
                parent,
                {"ts": "1020.000000", "thread_ts": "1000.000000",
                 "user": OWN, "text": "my own reply"},
            ]
        }
        self.run_main()
        self.assertEqual(len(self.read_inbox()), 2)

    def test_only_missing_replies_are_added(self):
        # reply_count 2, one reply already in the inbox: fetch once,
        # add only the missing one.
        self.write_inbox([ev("1000.000000"), ev("1010.000000"),
                          ev("1015.000000", thread_ts="1000.000000")])
        parent = {"ts": "1000.000000", "thread_ts": "1000.000000",
                  "user": OTHER, "text": "parent", "reply_count": 2}
        self.history = [parent]
        self.threads = {
            "1000.000000": [
                parent,
                {"ts": "1015.000000", "thread_ts": "1000.000000",
                 "user": OTHER, "text": "already seen"},
                {"ts": "1020.000000", "thread_ts": "1000.000000",
                 "user": OTHER, "text": "missed"},
            ]
        }
        self.run_main()
        got = self.read_inbox()
        self.assertEqual(self.thread_calls,
                         [("1000.000000", "1015.000000")])
        self.assertEqual(
            sorted(e["ts"] for e in got if e["ts"] == "1020.000000"),
            ["1020.000000"])
        self.assertEqual(len(got), 4)


class SeedThreadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = os.path.join(self.tmp.name, "inbox.jsonl")
        self.ws = {"workspace_id": WID, "workspace_name": "test"}
        self.seen = set()
        self.domains = {WID: "example.slack.com"}
        self.now = time.time()
        auto_join.SEED_WINDOW_SECS = 900

    def tearDown(self):
        self.tmp.cleanup()

    def parent_msg(self, ts, reply_count):
        return {"ts": ts, "thread_ts": ts, "user": OTHER,
                "text": "parent", "reply_count": reply_count}

    def run_seed(self, history, threads):
        def fake_run_slackcli(args):
            if "--thread-ts" in args:
                pts = args[args.index("--thread-ts") + 1]
                return {"messages": threads.get(pts, []),
                        "has_more": False}
            return {"messages": history, "has_more": False}
        auto_join.run_slackcli = fake_run_slackcli
        return auto_join.seed_channel(
            self.ws, CHAN, "test-key", {OWN}, self.seen, self.domains,
            {}, self.inbox, dry=False)

    def read_inbox(self):
        if not os.path.exists(self.inbox):
            return []
        with open(self.inbox) as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_thread_reply_inside_window_is_seeded(self):
        pts = f"{self.now - 120:.6f}"
        rts = f"{self.now - 60:.6f}"
        n = self.run_seed(
            [self.parent_msg(pts, 1)],
            {pts: [self.parent_msg(pts, 1),
                   {"ts": rts, "thread_ts": pts, "user": OTHER,
                    "text": "reply in gap"}]})
        self.assertEqual(n, 2)
        got = self.read_inbox()
        self.assertEqual(sorted(e["ts"] for e in got), sorted([pts, rts]))
        self.assertEqual(
            [e for e in got if e["ts"] == rts][0]["thread_ts"], pts)

    def test_thread_reply_outside_window_is_not_seeded(self):
        pts = f"{self.now - 3600:.6f}"
        rts = f"{self.now - 3500:.6f}"
        n = self.run_seed(
            [self.parent_msg(pts, 1)],
            {pts: [self.parent_msg(pts, 1),
                   {"ts": rts, "thread_ts": pts, "user": OTHER,
                    "text": "old reply"}]})
        self.assertEqual(n, 0)
        self.assertEqual(self.read_inbox(), [])

    def test_complete_thread_is_not_refetched(self):
        pts = f"{self.now - 120:.6f}"
        rts = f"{self.now - 60:.6f}"
        # The reply is already in the inbox: thread count == reply_count.
        self.seen.add((WID, CHAN, rts))
        with open(self.inbox, "w") as f:
            f.write(json.dumps(ev(rts, thread_ts=pts)) + "\n")
        seen_before = set(self.seen)
        calls = []

        def fake_run_slackcli(args):
            calls.append(list(args))
            return {"messages": [self.parent_msg(pts, 1)],
                    "has_more": False}
        auto_join.run_slackcli = fake_run_slackcli
        # Rebuild thread_counts the way load_inbox_state does.
        thread_counts = {(WID, CHAN, pts): 1}
        n = auto_join.seed_channel(
            self.ws, CHAN, "test-key", {OWN}, self.seen, self.domains,
            thread_counts, self.inbox, dry=False)
        # Only the parent itself is seeded; the thread is not re-read.
        self.assertEqual(n, 1)
        self.assertFalse(any("--thread-ts" in c for c in calls))
        self.assertEqual(len(self.read_inbox()), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
