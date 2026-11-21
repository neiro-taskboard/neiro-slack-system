#!/usr/bin/env python3
"""Tests for archive.py flock handling (Yama's Claude Code audit, 2026-11-21).

The guard under test: archive.py's docstring claimed the monthly run
happened "under the same flock as the hook poller (slack-events.lock)",
but LOCK_FILE was defined and never used -- no flock was ever taken.
The monthly cron runs archive.py bare, so its os.replace of inbox.jsonl
and of the dedup state file could race with a listener append (which
holds inbox.jsonl.lock across read-check-append) or with a poller
state update (which holds slack-events.lock). The fix takes both
locks for the whole run and exits 2 without changing anything when
either lock cannot be taken in time.

No real Slack calls; all files live in a temporary directory.
Run with: python3 test_archive_lock.py
"""
import fcntl
import gzip
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

LISTENER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module(name, filename):
    path = os.path.join(LISTENER_DIR, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


archive = load_module("archive_under_test", "archive.py")


def _old_ts(days=40):
    return (datetime.now(timezone.utc)
            - timedelta(days=days)).isoformat()


def _recent_ts():
    return datetime.now(timezone.utc).isoformat()


class ArchiveLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name
        self.events_dir = os.path.join(base, "events")
        os.makedirs(self.events_dir)
        self.inbox = os.path.join(self.events_dir, "inbox.jsonl")
        self.state = os.path.join(base, "slack-events.jsonl")
        self.lock = os.path.join(base, "slack-events.lock")
        self.archive_dir = os.path.join(base, "archive")
        with open(self.inbox, "w", encoding="utf-8") as f:
            f.write(json.dumps({"workspace_id": "W1", "channel": "C1",
                                "ts": "1", "received_at": _old_ts()}) + "\n")
            f.write(json.dumps({"workspace_id": "W1", "channel": "C1",
                                "ts": "2",
                                "received_at": _recent_ts()}) + "\n")
        with open(self.state, "w", encoding="utf-8") as f:
            f.write(json.dumps({"status": "done",
                                "updated_at": _old_ts(days=100)}) + "\n")
            f.write(json.dumps({"status": "done",
                                "updated_at": _recent_ts()}) + "\n")
        archive.BASE = base
        archive.INBOX = self.inbox
        archive.LOG = os.path.join(base, "listener.log")
        archive.ARCHIVE_DIR = self.archive_dir
        archive.STATE_FILE = self.state
        archive.LOCK_FILE = self.lock
        archive.LOCK_TIMEOUT_SECS = 1
        self._argv = sys.argv
        sys.argv = ["archive.py", "--dry-run"]

    def tearDown(self):
        sys.argv = self._argv
        self.tmp.cleanup()

    def _snapshot(self):
        with open(self.inbox, encoding="utf-8") as f:
            inbox = f.read()
        with open(self.state, encoding="utf-8") as f:
            state = f.read()
        return inbox, state

    def _try_lock(self, path):
        f = open(path, "a")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
        finally:
            f.close()

    def test_dry_run_succeeds_and_releases_locks(self):
        before = self._snapshot()
        archive.main()
        self.assertEqual(before, self._snapshot())
        self.assertTrue(self._try_lock(self.lock))
        self.assertTrue(self._try_lock(self.inbox + ".lock"))

    def test_state_lock_busy_exits_2_without_changes(self):
        before = self._snapshot()
        with open(self.lock, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.assertRaises(SystemExit) as cm:
                archive.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(before, self._snapshot())

    def test_inbox_lock_busy_exits_2_without_changes(self):
        before = self._snapshot()
        with open(self.inbox + ".lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.assertRaises(SystemExit) as cm:
                archive.main()
        self.assertEqual(cm.exception.code, 2)
        self.assertEqual(before, self._snapshot())

    def test_real_run_archives_old_event_under_lock(self):
        sys.argv = ["archive.py"]
        archive.main()
        with open(self.inbox, encoding="utf-8") as f:
            remaining = [json.loads(line) for line in f if line.strip()]
        self.assertEqual([ev["ts"] for ev in remaining], ["2"])
        month = _old_ts()[0:7]
        gz_path = os.path.join(self.archive_dir, f"inbox-{month}.jsonl.gz")
        with gzip.open(gz_path, "rt", encoding="utf-8") as gz:
            archived = [json.loads(line) for line in gz if line.strip()]
        self.assertEqual([ev["ts"] for ev in archived], ["1"])
        with open(self.state, encoding="utf-8") as f:
            kept = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(kept), 1)
        self.assertTrue(self._try_lock(self.lock))
        self.assertTrue(self._try_lock(self.inbox + ".lock"))


if __name__ == "__main__":
    unittest.main()
