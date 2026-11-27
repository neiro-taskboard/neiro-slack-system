#!/usr/bin/env python3
"""Tests for history paging in backfill.py (fetch_history).

Agreed 2026-11-21 with Yama / Yanayana: fetch up to 2-3 pages of
channel history instead of a single 20-message page, and warn when
the cap is hit. Before this, a burst of more than 20 missed messages
in one channel silently lost the oldest ones.

No real Slack calls: subprocess.run is stubbed to return canned
slackcli JSON pages, and main() runs against a temporary inbox.
Run with: python3 test_backfill_paging.py
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

WID = "T0TEST"
CHAN = "C0TEST"
OTHER = "U0OTHER"


class FakeProc:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def page(messages, has_more, next_oldest=None):
    body = {"messages": messages, "has_more": has_more}
    if next_oldest is not None:
        body["next_oldest"] = next_oldest
    return FakeProc(json.dumps(body))


def msg(ts):
    return {"ts": ts, "thread_ts": "", "user": OTHER, "text": f"m{ts}"}


class FetchHistoryPagingTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.pages = []
        self.orig_run = backfill.subprocess.run

    def tearDown(self):
        backfill.subprocess.run = self.orig_run

    def stub_run(self):
        def fake_run(cmd, **kwargs):
            self.calls.append(list(cmd))
            idx = len(self.calls) - 1
            return self.pages[idx]
        backfill.subprocess.run = fake_run

    def test_single_page_without_has_more(self):
        self.pages = [page([msg("1010.000000"), msg("1020.000000")], False)]
        self.stub_run()
        got = backfill.fetch_history("k", CHAN, oldest="1000.000000")
        self.assertEqual([m["ts"] for m in got],
                         ["1010.000000", "1020.000000"])
        self.assertEqual(len(self.calls), 1)
        self.assertIn("--oldest", self.calls[0])
        self.assertEqual(self.calls[0][self.calls[0].index("--oldest") + 1],
                         "1000.000000")

    def test_pages_forward_with_next_oldest(self):
        # 25 missed messages over two pages: all come back, and the
        # second page starts where the first left off.
        first = [msg(f"10{i:02d}.000000") for i in range(10, 30)]
        second = [msg(f"10{i:02d}.000000") for i in range(30, 35)]
        self.pages = [page(first, True, "1029.000000"),
                      page(second, False)]
        self.stub_run()
        got = backfill.fetch_history("k", CHAN, oldest="1000.000000")
        self.assertEqual(len(got), 25)
        self.assertEqual(got[0]["ts"], "1010.000000")
        self.assertEqual(got[-1]["ts"], "1034.000000")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[1][self.calls[1].index("--oldest") + 1],
                         "1029.000000")

    def test_stops_at_max_pages_and_warns(self):
        # Every page claims more; only HISTORY_MAX_PAGES are fetched.
        self.pages = [page([msg(f"10{i:02d}.000000")], True,
                           f"10{i:02d}.000000") for i in range(10, 20)]
        self.stub_run()
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            got = backfill.fetch_history("k", CHAN, oldest="1000.000000")
        self.assertEqual(len(self.calls), backfill.HISTORY_MAX_PAGES)
        self.assertEqual(len(got), backfill.HISTORY_MAX_PAGES)
        self.assertIn("page cap", buf.getvalue())

    def test_has_more_without_next_oldest_stops(self):
        self.pages = [page([msg("1010.000000")], True)]
        self.stub_run()
        got = backfill.fetch_history("k", CHAN, oldest="1000.000000")
        self.assertEqual(len(got), 1)
        self.assertEqual(len(self.calls), 1)

    def test_failure_returns_empty(self):
        backfill.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(
            OSError("boom"))
        got = backfill.fetch_history("k", CHAN, oldest="1000.000000")
        self.assertEqual(got, [])


def ev(ts, user=OTHER):
    return {
        "workspace_id": WID,
        "workspace_name": "test",
        "identity": "bot",
        "channel": CHAN,
        "channel_type": "channel",
        "user": user,
        "bot_id": "B0X",
        "text": "hello",
        "ts": ts,
        "thread_ts": "",
        "permalink": f"https://example.slack.com/archives/{CHAN}/p{ts}",
        "received_at": "2026-11-18T00:00:00+00:00",
        "backfilled": True,
    }


class BackfillPagingMainTest(unittest.TestCase):
    """End to end through main(): 25 missed messages over two pages
    are all admitted, and the fetch starts at the inbox baseline."""

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
            json.dump({"workspaces": {"test-key": {"user_id": "U0OWN"}}}, f)
        backfill.CONFIG_PATH = self.config_path
        backfill.SLACKCLI_STORE = self.store_path
        backfill.fetch_thread_messages = lambda ws, ch, pts, oldest=None: []
        self.orig_run = backfill.subprocess.run
        self.calls = []

    def tearDown(self):
        backfill.subprocess.run = self.orig_run
        self.tmp.cleanup()

    def test_burst_over_one_page_is_fully_backfilled(self):
        with open(self.inbox, "w") as f:
            f.write(json.dumps(ev("1000.000000")) + "\n")
        first = [msg(f"10{i:02d}.000000") for i in range(10, 30)]
        second = [msg(f"10{i:02d}.000000") for i in range(30, 35)]
        pages = [page(first, True, "1029.000000"), page(second, False)]

        def fake_run(cmd, **kwargs):
            self.calls.append(list(cmd))
            return pages[len(self.calls) - 1]
        backfill.subprocess.run = fake_run
        self.assertEqual(backfill.main(), 0)
        with open(self.inbox) as f:
            got = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(got), 26)  # baseline + 25 missed
        self.assertEqual(sorted(e["ts"] for e in got)[-1], "1034.000000")
        self.assertTrue(all(e["backfilled"] for e in got[1:]))
        # The first fetch starts at the baseline, not from scratch.
        self.assertEqual(
            self.calls[0][self.calls[0].index("--oldest") + 1],
            "1000.000000")


if __name__ == "__main__":
    unittest.main(verbosity=2)
