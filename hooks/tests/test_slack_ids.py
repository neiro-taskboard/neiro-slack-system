#!/usr/bin/env python3
"""Tests for hooks/scripts/slack-ids.sh — the shared Slack-ID loader.

The loader is fail-closed: a missing file, bad JSON, a missing key,
or a non-ID-shaped value must all exit non-zero and print nothing to
stdout, so a caller can never send to a wrong or empty channel.

Fixtures are built from parts (like test_blanket_id_pattern.py), never
written as full ID-shaped literals, so this file itself does not trip
the blanket scan gate. The real ids.json lives outside the repo at
~/.config/neiro-slack/ids.json and is never read by these tests; the
tests point NEIRO_SLACK_IDS_PATH at a temporary file instead.

Style follows the neighbours (plain unittest, no pytest):
  python3 ~/workspace/neiro-slack-system/hooks/tests/test_slack_ids.py
"""
import json
import os
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "slack-ids.sh",
)
EXAMPLE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "ids.example.json",
)
KEY = "channel_welcome_muse_torishimariyakai"


def make_id(prefix, rest="A1B2C3D4E5"):
    """Build an ID-shaped string from parts (11 chars, like a real ID)."""
    return prefix + "0" + rest


def run_loader(key, ids_path=None):
    env = dict(os.environ)
    if ids_path is None:
        env["NEIRO_SLACK_IDS_PATH"] = "/nonexistent/neiro-slack/ids.json"
    else:
        env["NEIRO_SLACK_IDS_PATH"] = ids_path
    return subprocess.run(
        ["bash", SCRIPT, key], capture_output=True, text=True, env=env,
    )


class TestSlackIdsLoader(unittest.TestCase):
    def test_valid_file_returns_value(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ids.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({KEY: make_id("C")}, f)
            result = run_loader(KEY, path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), make_id("C"))

    def test_missing_file_fails_closed(self):
        result = run_loader(KEY, "/nonexistent/neiro-slack/ids.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_missing_key_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ids.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"some_other_key": make_id("C")}, f)
            result = run_loader(KEY, path)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

    def test_bad_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ids.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            result = run_loader(KEY, path)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

    def test_non_id_shaped_value_fails_closed(self):
        # The example file's placeholders must never be usable as IDs.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ids.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({KEY: "EXAMPLE_CHANNEL_WELCOME"}, f)
            result = run_loader(KEY, path)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")

    def test_missing_key_argument_fails(self):
        result = run_loader("", "/nonexistent/neiro-slack/ids.json")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


class TestIdsExample(unittest.TestCase):
    def test_example_has_same_keys_as_loader_expects(self):
        with open(EXAMPLE, encoding="utf-8") as f:
            example = json.load(f)
        self.assertIn(KEY, example)
        self.assertIn("workspace_muse_torishimariyakai", example)
        self.assertIn("bot_neiro_muse_torishimariyakai", example)
        self.assertIn("bot_neiro_neiro_task_board", example)

    def test_example_values_are_not_id_shaped(self):
        import re

        blanket = re.compile(r"[UWBCTGDA]0[A-Z0-9]{8,}")
        with open(EXAMPLE, encoding="utf-8") as f:
            example = json.load(f)
        for key, value in example.items():
            self.assertIsNone(
                blanket.search(value),
                f"example value for {key} must not look like a real ID",
            )


if __name__ == "__main__":
    unittest.main()
