#!/usr/bin/env python3
"""Tests for the atomic append-if-absent inbox helper (inbox_append.py),
added 2026-11-21 following Yanayana's burst fix.

The guard under test: the socket listener, backfill and auto-join all
append to the same inbox.jsonl, and each used to "check seen, then
append" without a shared lock. append_events holds an flock on
<inbox>.lock across the read-check-append step, so concurrent writers
cannot land the same post twice.

No real Slack calls; all files live in a temporary directory.
Run with: python3 test_inbox_append.py
"""
import importlib.util
import json
import multiprocessing
import os
import sys
import tempfile
import unittest

LISTENER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module(name, filename):
    path = os.path.join(LISTENER_DIR, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


inbox_append = load_module("inbox_append_under_test", "inbox_append.py")


def ev(ts, channel="C0TEST", text="hello"):
    return {"workspace_id": "T0TEST", "channel": channel, "ts": ts,
            "text": text}


def read_keys(path):
    keys = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                keys.append(inbox_append.event_key(json.loads(line)))
    return keys


def _worker_append(args):
    inbox_path, events = args
    sys.path.insert(0, LISTENER_DIR)
    import inbox_append as ia
    return ia.append_events(inbox_path, events)


class TestAppendEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = os.path.join(self.tmp.name, "inbox.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_new_and_skip_duplicate(self):
        added, skipped = inbox_append.append_events(
            self.inbox, [ev("1.0"), ev("2.0")])
        self.assertEqual(len(added), 2)
        self.assertEqual(skipped, 0)
        added, skipped = inbox_append.append_events(
            self.inbox, [ev("2.0"), ev("3.0")])
        self.assertEqual([e["ts"] for e in added], ["3.0"])
        self.assertEqual(skipped, 1)
        self.assertEqual(read_keys(self.inbox),
                         [("T0TEST", "C0TEST", "1.0"),
                          ("T0TEST", "C0TEST", "2.0"),
                          ("T0TEST", "C0TEST", "3.0")])

    def test_duplicate_within_one_batch(self):
        added, skipped = inbox_append.append_events(
            self.inbox, [ev("1.0"), ev("1.0"), ev("1.0")])
        self.assertEqual(len(added), 1)
        self.assertEqual(skipped, 2)
        self.assertEqual(len(read_keys(self.inbox)), 1)

    def test_empty_batch_creates_nothing(self):
        added, skipped = inbox_append.append_events(self.inbox, [])
        self.assertEqual((added, skipped), ([], 0))
        self.assertFalse(os.path.exists(self.inbox))

    def test_broken_lines_do_not_block_append(self):
        with open(self.inbox, "w", encoding="utf-8") as f:
            f.write(json.dumps(ev("1.0")) + "\n")
            f.write("{not json\n")
            f.write("\n")
        added, skipped = inbox_append.append_events(
            self.inbox, [ev("1.0"), ev("2.0")])
        self.assertEqual([e["ts"] for e in added], ["2.0"])
        self.assertEqual(skipped, 1)

    def test_missing_directory_is_created(self):
        nested = os.path.join(self.tmp.name, "deep", "inbox.jsonl")
        added, skipped = inbox_append.append_events(nested, [ev("1.0")])
        self.assertEqual(len(added), 1)
        self.assertTrue(os.path.exists(nested))

    def test_concurrent_processes_no_duplicates(self):
        # Four processes appending overlapping sets at once: the same
        # shape as listener + backfill racing during a burst. Every
        # key must land exactly once.
        shared = [ev(f"{i}.0") for i in range(20)]
        jobs = [(self.inbox, shared) for _ in range(4)]
        with multiprocessing.get_context("fork").Pool(4) as pool:
            results = pool.map(_worker_append, jobs)
        total_added = sum(len(a) for a, _ in results)
        self.assertEqual(total_added, 20)
        keys = read_keys(self.inbox)
        self.assertEqual(len(keys), 20)
        self.assertEqual(len(set(keys)), 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
