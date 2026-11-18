#!/usr/bin/env python3
"""Tests for the inbox.jsonl .bak support in backfill.py
(snapshot_inbox / load_inbox / restore_inbox), added 2026-11-18
following Yanayana's seen_state.py approach.

The guard under test: only a fully clean body (every non-empty line
valid JSON, at least one event) may overwrite a good .bak; a broken
or empty body must leave the .bak alone. The fallback under test:
a missing or unopenable body falls back to the .bak.

No real Slack calls; all files live in a temporary directory.
Run with: python3 test_backfill_inbox_bak.py
"""
import importlib.util
import json
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


backfill = load_module("backfill_under_test", "backfill.py")


def ev(ts, text="hello"):
    return {"workspace_id": "T0TEST", "channel": "C0TEST", "ts": ts,
            "text": text}


class TestSnapshotInbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = os.path.join(self.tmp.name, "inbox.jsonl")
        self.bak = self.inbox + ".bak"

    def tearDown(self):
        self.tmp.cleanup()

    def write_inbox(self, lines):
        with open(self.inbox, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")

    def test_clean_body_creates_bak(self):
        self.write_inbox([json.dumps(ev("1.0")), json.dumps(ev("2.0"))])
        self.assertTrue(backfill.snapshot_inbox(self.inbox))
        with open(self.bak, encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 2)

    def test_broken_line_skips_snapshot(self):
        self.write_inbox([json.dumps(ev("1.0")), "{not json"])
        self.assertFalse(backfill.snapshot_inbox(self.inbox))
        self.assertFalse(os.path.exists(self.bak))

    def test_broken_body_never_clobbers_good_bak(self):
        good = json.dumps(ev("9.0")) + "\n"
        with open(self.bak, "w", encoding="utf-8") as f:
            f.write(good)
        self.write_inbox([json.dumps(ev("1.0")), "{not json"])
        self.assertFalse(backfill.snapshot_inbox(self.inbox))
        with open(self.bak, encoding="utf-8") as f:
            self.assertEqual(f.read(), good)

    def test_empty_body_never_clobbers_good_bak(self):
        good = json.dumps(ev("9.0")) + "\n"
        with open(self.bak, "w", encoding="utf-8") as f:
            f.write(good)
        open(self.inbox, "w", encoding="utf-8").close()
        self.assertFalse(backfill.snapshot_inbox(self.inbox))
        with open(self.bak, encoding="utf-8") as f:
            self.assertEqual(f.read(), good)

    def test_missing_body_skips_snapshot(self):
        self.assertFalse(backfill.snapshot_inbox(self.inbox))
        self.assertFalse(os.path.exists(self.bak))


class TestLoadInbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = os.path.join(self.tmp.name, "inbox.jsonl")
        self.bak = self.inbox + ".bak"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, events):
        with open(path, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e) + "\n")

    def test_normal_body_no_fallback(self):
        self.write(self.inbox, [ev("1.0"), ev("2.0")])
        events, from_bak = backfill.load_inbox(self.inbox)
        self.assertEqual(len(events), 2)
        self.assertFalse(from_bak)

    def test_missing_body_falls_back_to_bak(self):
        self.write(self.bak, [ev("1.0")])
        events, from_bak = backfill.load_inbox(self.inbox)
        self.assertEqual(len(events), 1)
        self.assertTrue(from_bak)

    def test_unopenable_body_falls_back_to_bak(self):
        os.mkdir(self.inbox)  # opening a directory raises OSError
        self.write(self.bak, [ev("1.0"), ev("2.0")])
        events, from_bak = backfill.load_inbox(self.inbox)
        self.assertEqual(len(events), 2)
        self.assertTrue(from_bak)

    def test_nothing_anywhere_starts_empty(self):
        events, from_bak = backfill.load_inbox(self.inbox)
        self.assertEqual(events, [])
        self.assertFalse(from_bak)

    def test_broken_lines_in_body_are_skipped_not_fatal(self):
        with open(self.inbox, "w", encoding="utf-8") as f:
            f.write(json.dumps(ev("1.0")) + "\n{not json\n")
        events, from_bak = backfill.load_inbox(self.inbox)
        self.assertEqual(len(events), 1)
        self.assertFalse(from_bak)


class TestRestoreInbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = os.path.join(self.tmp.name, "inbox.jsonl")
        self.bak = self.inbox + ".bak"

    def tearDown(self):
        self.tmp.cleanup()

    def test_restore_then_body_reads_back(self):
        recovered = [ev("1.0"), ev("2.0", text="two")]
        with open(self.bak, "w", encoding="utf-8") as f:
            for e in recovered:
                f.write(json.dumps(e) + "\n")
        events, from_bak = backfill.load_inbox(self.inbox)
        self.assertTrue(from_bak)
        backfill.restore_inbox(self.inbox, events)
        events2, from_bak2 = backfill.load_inbox(self.inbox)
        self.assertFalse(from_bak2)
        self.assertEqual([e["ts"] for e in events2], ["1.0", "2.0"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
