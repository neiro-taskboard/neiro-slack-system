#!/usr/bin/env python3
"""Tests for DM readability in backfill.py (fetch_history_status).

2026-11-27: the DM branch logged "neither bot nor user key can read
it" whenever both keys returned no new messages, because it treated
an empty result as a failed fetch. A quiet DM — no posts since the
inbox baseline, such as the 2026-11-14 test DMs — therefore looked
unreadable on every backfill run even when the read had succeeded.
The fix distinguishes a failed fetch from a successful empty one:
only both keys failing counts as unreadable.

No real Slack calls: subprocess.run is stubbed to return canned
slackcli JSON pages, and main() runs against a temporary inbox.
Run with: python3 test_backfill_dm_readable.py
"""
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

LISTENER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module(name, filename):
    path = os.path.join(LISTENER_DIR, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


backfill = load_module("backfill_under_test", "backfill.py")

WID = "T0TEST"
DM = "D0TEST"
OWN = "U0OWN"
OTHER = "U0OTHER"
UNREADABLE = "neither bot nor user key can read it"


class FakeProc:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def page(messages, has_more=False, next_oldest=None):
    body = {"messages": messages, "has_more": has_more}
    if next_oldest is not None:
        body["next_oldest"] = next_oldest
    return FakeProc(json.dumps(body))


def msg(ts, user=OTHER):
    return {"ts": ts, "thread_ts": "", "user": user, "text": f"m{ts}"}


class FetchHistoryStatusTest(unittest.TestCase):
    def setUp(self):
        self.orig_run = backfill.subprocess.run

    def tearDown(self):
        backfill.subprocess.run = self.orig_run

    def test_success_empty_is_not_a_failure(self):
        backfill.subprocess.run = lambda *a, **k: page([])
        got, failed = backfill.fetch_history_status(
            "k", "C0TEST", oldest="1000.000000")
        self.assertEqual(got, [])
        self.assertFalse(failed)

    def test_success_with_messages_is_not_a_failure(self):
        backfill.subprocess.run = lambda *a, **k: page([msg("1010.000000")])
        got, failed = backfill.fetch_history_status(
            "k", "C0TEST", oldest="1000.000000")
        self.assertEqual([m["ts"] for m in got], ["1010.000000"])
        self.assertFalse(failed)

    def test_subprocess_error_is_a_failure(self):
        backfill.subprocess.run = lambda *a, **k: (_ for _ in ()).throw(
            OSError("boom"))
        got, failed = backfill.fetch_history_status(
            "k", "C0TEST", oldest="1000.000000")
        self.assertEqual(got, [])
        self.assertTrue(failed)

    def test_no_json_is_a_failure(self):
        backfill.subprocess.run = lambda *a, **k: FakeProc("not json")
        got, failed = backfill.fetch_history_status(
            "k", "C0TEST", oldest="1000.000000")
        self.assertEqual(got, [])
        self.assertTrue(failed)

    def test_wrapper_keeps_old_return_shape(self):
        backfill.subprocess.run = lambda *a, **k: page([msg("1010.000000")])
        got = backfill.fetch_history("k", "C0TEST", oldest="1000.000000")
        self.assertEqual([m["ts"] for m in got], ["1010.000000"])


def dm_ev(ts, user=OTHER):
    return {
        "workspace_id": WID,
        "workspace_name": "test",
        "identity": "user",
        "channel": DM,
        "channel_type": "im",
        "user": user,
        "bot_id": "",
        "text": "hello",
        "ts": ts,
        "thread_ts": "",
        "permalink": f"https://example.slack.com/archives/{DM}/p{ts}",
        "received_at": "2026-11-14T00:00:00+00:00",
        "backfilled": False,
    }


class BackfillDmReadableMainTest(unittest.TestCase):
    """End to end through main() for one DM channel with both keys."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.inbox = os.path.join(self.tmp.name, "inbox.jsonl")
        config = {
            "inbox_path": self.inbox,
            "workspaces": [{
                "workspace_id": WID,
                "workspace_name": "test",
                "slackcli_key": "bot-key",
                "slackcli_user_key": "user-key",
            }],
        }
        self.config_path = os.path.join(self.tmp.name, "config.json")
        with open(self.config_path, "w") as f:
            json.dump(config, f)
        self.store_path = os.path.join(self.tmp.name, "workspaces.json")
        with open(self.store_path, "w") as f:
            json.dump({"workspaces": {
                "bot-key": {"user_id": OWN},
                "user-key": {"user_id": "U0USER"},
            }}, f)
        backfill.CONFIG_PATH = self.config_path
        backfill.SLACKCLI_STORE = self.store_path
        backfill.fetch_thread_messages = lambda ws, ch, pts, oldest=None: []
        self.orig_run = backfill.subprocess.run
        self.responses = {}

    def tearDown(self):
        backfill.subprocess.run = self.orig_run
        self.tmp.cleanup()

    def write_inbox(self, events):
        with open(self.inbox, "w") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def read_inbox(self):
        with open(self.inbox) as f:
            return [json.loads(line) for line in f if line.strip()]

    def run_main(self):
        def fake_run(cmd, **kwargs):
            key = cmd[cmd.index("--workspace") + 1]
            return self.responses[key]
        backfill.subprocess.run = fake_run
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = backfill.main()
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_quiet_dm_is_not_logged_unreadable(self):
        # Baseline newest is 1000; both keys read fine but there is
        # nothing newer. Before the fix this logged "neither bot nor
        # user key can read it" on every run.
        self.write_inbox([dm_ev("1000.000000")])
        self.responses = {"bot-key": page([]), "user-key": page([])}
        out = self.run_main()
        self.assertNotIn(UNREADABLE, out)
        self.assertEqual(len(self.read_inbox()), 1)

    def test_both_keys_failing_is_logged_unreadable(self):
        self.write_inbox([dm_ev("1000.000000")])
        self.responses = {"bot-key": FakeProc(""),
                          "user-key": FakeProc("")}
        out = self.run_main()
        self.assertIn(UNREADABLE, out)
        self.assertEqual(len(self.read_inbox()), 1)

    def test_one_key_failing_still_backfills_from_the_other(self):
        # The bot key cannot see a user DM (expected); the user key
        # returns one new message, which must be backfilled without
        # the "neither" warning.
        self.write_inbox([dm_ev("1000.000000")])
        self.responses = {"bot-key": FakeProc(""),
                          "user-key": page([msg("1010.000000")])}
        out = self.run_main()
        self.assertNotIn(UNREADABLE, out)
        got = self.read_inbox()
        self.assertEqual(len(got), 2)
        self.assertEqual(got[1]["ts"], "1010.000000")
        self.assertTrue(got[1]["backfilled"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
