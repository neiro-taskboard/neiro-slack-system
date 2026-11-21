#!/usr/bin/env python3
"""Tests for archive.py parse_iso (naive timestamps treated as UTC).

The guard under test: parse_iso used to return datetime.fromisoformat(s)
as-is, so a timezone-less (naive) received_at / updated_at came back
naive and the comparisons against the aware cutoff / state_cutoff in
main() raised TypeError, killing the whole archive run. This is the same
hole Yanayana found in msgstate.py _parse_ts (2026-11-21): our writer
always stamps aware UTC, so no live harm yet, but an external or
old-format naive value would crash the run.

No real Slack calls; all files live in a temporary directory.
Run with: python3 test_archive_parse_iso.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

LISTENER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module(name, filename):
    path = os.path.join(LISTENER_DIR, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


archive = load_module("archive_under_test", "archive.py")


class TestParseIso(unittest.TestCase):
    def test_naive_is_utc(self):
        dt = archive.parse_iso("2026-11-21T14:15:43")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo, timezone.utc)
        self.assertEqual((dt.year, dt.month, dt.day), (2026, 11, 21))

    def test_aware_kept_as_is(self):
        dt = archive.parse_iso("2026-11-21T14:15:43+00:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo, timezone.utc)

    def test_invalid_returns_none(self):
        self.assertIsNone(archive.parse_iso("not-a-time"))
        self.assertIsNone(archive.parse_iso(None))

    def test_naive_compares_with_aware_cutoff(self):
        # The actual crash: naive dt >= aware cutoff raised TypeError.
        dt = archive.parse_iso("2026-11-21T14:15:43")
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        self.assertTrue(dt >= cutoff)


class TestMainWithNaiveTimestamps(unittest.TestCase):
    def test_old_naive_event_archives_and_old_naive_state_prunes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = os.path.join(tmp, "listener")
            events_dir = os.path.join(base, "events")
            os.makedirs(events_dir)
            archive_dir = os.path.join(base, "archive")
            inbox = os.path.join(events_dir, "inbox.jsonl")
            state_file = os.path.join(tmp, "slack-events.jsonl")
            now = datetime.now(timezone.utc)
            old_naive = "2026-09-01T00:00:00"  # naive, older than 30 days
            recent_aware = now.isoformat()
            with open(inbox, "w", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"ts": "1", "received_at": old_naive,
                     "text": "old naive"}, ensure_ascii=False) + "\n")
                f.write(json.dumps(
                    {"ts": "2", "received_at": recent_aware,
                     "text": "recent aware"}, ensure_ascii=False) + "\n")
            with open(state_file, "w", encoding="utf-8") as f:
                f.write(json.dumps(
                    {"status": "done", "updated_at": "2026-08-01T00:00:00",
                     "event_id": "old-naive"}, ensure_ascii=False) + "\n")
                f.write(json.dumps(
                    {"status": "done", "updated_at": recent_aware,
                     "event_id": "recent-aware"}, ensure_ascii=False) + "\n")
            with mock.patch.object(archive, "BASE", base), \
                    mock.patch.object(archive, "INBOX", inbox), \
                    mock.patch.object(archive, "LOG",
                                      os.path.join(base, "listener.log")), \
                    mock.patch.object(archive, "ARCHIVE_DIR", archive_dir), \
                    mock.patch.object(archive, "STATE_FILE", state_file), \
                    mock.patch.object(sys, "argv", ["archive.py"]):
                archive.main()  # must not raise TypeError
            with open(inbox, encoding="utf-8") as f:
                kept = [json.loads(line) for line in f if line.strip()]
            self.assertEqual([e["ts"] for e in kept], ["2"])
            self.assertTrue(os.path.exists(
                os.path.join(archive_dir, "inbox-2026-09.jsonl.gz")))
            with open(state_file, encoding="utf-8") as f:
                kept_state = [json.loads(line) for line in f if line.strip()]
            self.assertEqual([r["event_id"] for r in kept_state],
                             ["recent-aware"])


if __name__ == "__main__":
    unittest.main()
