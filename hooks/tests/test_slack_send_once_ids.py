#!/usr/bin/env python3
"""Tests for the slack-send-once.sh real-ID externalization (2026-11-19).

The bot-ID fallback in self_user_id() used to be hardcoded in the script
(U0CH... for muse-board, U0CH... for the default workspace). It now reads
the IDs from ids.json via slack-ids.sh, and fails closed (exit 3) when
neither `slackcli auth whoami` nor ids.json can identify the bot — an
empty SELF_ID would let the duplicate checks pass everything through.

Fixtures are built from parts, never written as full ID-shaped literals,
so this file itself does not trip the blanket scan gate (see
test_blanket_id_pattern.py).

Style follows the neighbours (plain unittest, no pytest):
  python3 ~/hooks/tests/test_slack_send_once_ids.py
"""
import json
import os
import re
import stat
import subprocess
import tempfile
import time
import unittest

SCRIPT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "scripts", "slack-send-once.sh"))
BLANKET = re.compile(r"[UWBCTGDA]0[A-Z0-9]{8,}")


def make_id(prefix, rest):
    """Build an ID-shaped string from parts (like a real Slack ID)."""
    return prefix + "0" + rest


# Synthetic IDs, built from parts so no real ID appears in this file.
BOT_MUSE_BOARD = make_id("U", "TESTBOTMUSE")
BOT_TASK_BOARD = make_id("U", "TESTBOTTASK")
WORKSPACE_MUSE_BOARD = make_id("T", "TESTWSMUSE1")
WORKSPACE_TASK_BOARD = make_id("T", "TESTWSTASK1")
WHOAMI_BOT = make_id("U", "TESTWHOIAMI")
CHANNEL = make_id("C", "TESTCHANNE1")
BODY = "これはテストの本文です。"


STUB_FAILING_AUTH = """#!/bin/sh
# Log argv, then answer like slackcli — except auth always fails.
echo "$@" >> "$SLACKCLI_LOG"
case "$1" in
  auth) exit 1 ;;
  conversations) cat "$SLACKCLI_HISTORY" ;;
esac
exit 0
"""

STUB_GARBAGE_AUTH = """#!/bin/sh
# auth fails, but stdout still carries an error-looking "(...)" string.
echo "$@" >> "$SLACKCLI_LOG"
case "$1" in
  auth) echo "Error: (UNAUTHORIZED) please log in"; exit 1 ;;
  conversations) cat "$SLACKCLI_HISTORY" ;;
esac
exit 0
"""

STUB_WORKING_AUTH = """#!/bin/sh
# Log argv, then answer like slackcli — auth whoami succeeds and
# reports the WHOAMI_BOT id (filled in by Env).
echo "$@" >> "$SLACKCLI_LOG"
case "$1" in
  auth) echo "Logged in as neiro (__WHOAMI_BOT__)"; exit 0 ;;
  conversations) cat "$SLACKCLI_HISTORY" ;;
esac
exit 0
"""


class Env:
    """Temp HOME with a stub slackcli plus an ids.json.

    stub: STUB_FAILING_AUTH (whoami fails) or STUB_WORKING_AUTH.
    ids_data: dict to write as ids.json; None writes nothing (missing
    file). ids_raw: raw text to write instead (e.g. malformed JSON).
    """

    def __init__(self, with_ids=True, stub=STUB_FAILING_AUTH,
                 ids_data=None, ids_raw=None):
        self.d = tempfile.TemporaryDirectory()
        self.home = self.d.name
        bindir = os.path.join(self.home, "workspace", "bin")
        os.makedirs(bindir, exist_ok=True)
        self.log = os.path.join(self.home, "slackcli.log")
        self.history_path = os.path.join(self.home, "history.json")
        stub_path = os.path.join(bindir, "slackcli")
        with open(stub_path, "w", encoding="utf-8") as f:
            f.write(stub.replace("__WHOAMI_BOT__", WHOAMI_BOT))
        os.chmod(stub_path, 0o755)
        self.ids_path = os.path.join(self.home, "ids.json")
        if with_ids:
            if ids_raw is not None:
                with open(self.ids_path, "w", encoding="utf-8") as f:
                    f.write(ids_raw)
            else:
                data = ids_data if ids_data is not None else {
                    "workspace_muse_torishimariyakai": WORKSPACE_MUSE_BOARD,
                    "workspace_neiro_task_board": WORKSPACE_TASK_BOARD,
                    "channel_welcome_muse_torishimariyakai": CHANNEL,
                    "bot_neiro_muse_torishimariyakai": BOT_MUSE_BOARD,
                    "bot_neiro_neiro_task_board": BOT_TASK_BOARD,
                }
                with open(self.ids_path, "w", encoding="utf-8") as f:
                    json.dump(data, f)
            os.chmod(self.ids_path, 0o600)
        self.env = dict(os.environ, HOME=self.home,
                        SLACKCLI_LOG=self.log,
                        SLACKCLI_HISTORY=self.history_path,
                        NEIRO_SLACK_IDS_PATH=self.ids_path)

    def run_check_only(self, message, history_user, history_text,
                       extra_args=None):
        history = {"messages": [{"ts": str(time.time()),
                                  "user": history_user,
                                  "text": history_text}],
                   "has_more": False}
        with open(self.history_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
        args = ["bash", SCRIPT, "--channel", CHANNEL,
                "--message", message, "--check-only"]
        if extra_args:
            args += extra_args
        return subprocess.run(args, env=self.env, capture_output=True,
                              text=True, timeout=120).returncode

    def close(self):
        self.d.cleanup()


class TestSendOnceIdsFallback(unittest.TestCase):
    def test_fallback_bot_id_used_when_whoami_fails(self):
        # whoami fails, ids.json supplies the muse-board bot ID, and the
        # history holds that bot's identical post -> duplicate (exit 0).
        env = Env()
        try:
            rc = env.run_check_only(
                BODY, BOT_MUSE_BOARD, BODY,
                extra_args=["--workspace", "muse-board"])
            self.assertEqual(rc, 0, "fallback bot ID should match own post")
        finally:
            env.close()

    def test_fallback_does_not_match_other_bot(self):
        # Same setup, but the history post is the other workspace's bot:
        # it must not count as our own post -> no duplicate (exit 1).
        env = Env()
        try:
            rc = env.run_check_only(
                BODY, BOT_TASK_BOARD, BODY,
                extra_args=["--workspace", "muse-board"])
            self.assertEqual(rc, 1, "other bot's post must not match")
        finally:
            env.close()

    def test_workspace_id_argument_selects_muse_board_bot(self):
        # --workspace may be passed as the workspace ID itself; the
        # fallback must still pick the muse-board bot ID.
        env = Env()
        try:
            rc = env.run_check_only(
                BODY, BOT_MUSE_BOARD, BODY,
                extra_args=["--workspace", WORKSPACE_MUSE_BOARD])
            self.assertEqual(rc, 0, "workspace ID should select muse bot")
        finally:
            env.close()

    def test_default_workspace_uses_task_board_bot(self):
        # No --workspace at all: the default workspace's bot ID must
        # be used, so its own post counts as a duplicate (exit 0).
        env = Env()
        try:
            rc = env.run_check_only(BODY, BOT_TASK_BOARD, BODY)
            self.assertEqual(rc, 0, "default workspace should use task bot")
        finally:
            env.close()

    def test_task_board_workspace_id_selects_task_board_bot(self):
        # --workspace as the task-board workspace ID itself.
        env = Env()
        try:
            rc = env.run_check_only(
                BODY, BOT_TASK_BOARD, BODY,
                extra_args=["--workspace", WORKSPACE_TASK_BOARD])
            self.assertEqual(rc, 0, "task-board workspace ID should match")
        finally:
            env.close()

    def test_unknown_workspace_name_fails_closed(self):
        # A typo or unknown name must not silently fall through to
        # the default bot (that would let duplicate checks pass).
        env = Env()
        try:
            rc = env.run_check_only(
                BODY, BOT_TASK_BOARD, BODY,
                extra_args=["--workspace", "muse_board"])
            self.assertEqual(rc, 3, "unknown workspace must fail closed")
        finally:
            env.close()

    def test_unknown_workspace_id_fails_closed(self):
        # A workspace ID that matches neither known workspace.
        env = Env()
        try:
            rc = env.run_check_only(
                BODY, BOT_TASK_BOARD, BODY,
                extra_args=["--workspace", make_id("T", "TESTWSOTHER")])
            self.assertEqual(rc, 3, "unknown workspace ID must fail closed")
        finally:
            env.close()

    def test_whoami_success_uses_whoami_id(self):
        # whoami succeeds: its ID is used, and the ids.json fallback
        # bot must NOT count as our own post.
        env = Env(stub=STUB_WORKING_AUTH)
        try:
            rc = env.run_check_only(BODY, WHOAMI_BOT, BODY,
                                    extra_args=["--workspace", "muse-board"])
            self.assertEqual(rc, 0, "whoami ID should match own post")
            rc = env.run_check_only(BODY, BOT_MUSE_BOARD, BODY,
                                    extra_args=["--workspace", "muse-board"])
            self.assertEqual(rc, 1, "fallback bot must not match")
        finally:
            env.close()

    def test_partial_ids_failures_fail_closed(self):
        # Key missing, empty value, and malformed JSON must all refuse
        # to send (exit 3), not proceed with a wrong or empty SELF_ID.
        cases = [
            # Bot key missing entirely.
            {"workspace_muse_torishimariyakai": WORKSPACE_MUSE_BOARD},
            # Bot key present but empty.
            {"workspace_muse_torishimariyakai": WORKSPACE_MUSE_BOARD,
             "bot_neiro_muse_torishimariyakai": ""},
        ]
        for data in cases:
            env = Env(ids_data=data)
            try:
                rc = env.run_check_only(
                    BODY, BOT_MUSE_BOARD, BODY,
                    extra_args=["--workspace", "muse-board"])
                self.assertEqual(rc, 3, f"partial ids must fail: {data}")
            finally:
                env.close()
        env = Env(ids_raw="{not json")
        try:
            rc = env.run_check_only(
                BODY, BOT_MUSE_BOARD, BODY,
                extra_args=["--workspace", "muse-board"])
            self.assertEqual(rc, 3, "malformed ids.json must fail closed")
        finally:
            env.close()

    def test_workspace_id_with_missing_workspace_key_fails_closed(self):
        # B-2: --workspace as the muse-board ID, but ids.json lacks
        # the workspace key to match it against. The old code fell
        # through to the task-board bot; it must now refuse (exit 3).
        env = Env(ids_data={
            "bot_neiro_muse_torishimariyakai": BOT_MUSE_BOARD,
            "bot_neiro_neiro_task_board": BOT_TASK_BOARD,
        })
        try:
            rc = env.run_check_only(
                BODY, BOT_MUSE_BOARD, BODY,
                extra_args=["--workspace", WORKSPACE_MUSE_BOARD])
            self.assertEqual(rc, 3, "unmatchable workspace ID must fail")
        finally:
            env.close()

    def test_whoami_garbage_output_falls_back(self):
        # whoami exits 1 but prints "(UNAUTHORIZED)": the old code's
        # grep matched U... and returned it as the self ID. It must
        # fall back to ids.json instead (same hole as answered-check).
        env = Env(stub=STUB_GARBAGE_AUTH)
        try:
            rc = env.run_check_only(
                BODY, BOT_MUSE_BOARD, BODY,
                extra_args=["--workspace", "muse-board"])
            self.assertEqual(rc, 0, "garbage whoami must fall back")
        finally:
            env.close()

    def test_missing_ids_file_fails_closed(self):
        # whoami fails and ids.json is missing: SELF_ID cannot be
        # determined, so the script must refuse to send (exit 3),
        # not proceed with an empty SELF_ID.
        env = Env(with_ids=False)
        try:
            rc = env.run_check_only(
                BODY, BOT_MUSE_BOARD, BODY,
                extra_args=["--workspace", "muse-board"])
            self.assertEqual(rc, 3, "missing ids.json must fail closed")
        finally:
            env.close()


class TestNoHardcodedIdsInScript(unittest.TestCase):
    def test_script_contains_no_id_shaped_literals(self):
        with open(SCRIPT, encoding="utf-8") as f:
            text = f.read()
        found = BLANKET.findall(text)
        self.assertEqual(found, [], f"hardcoded IDs remain: {found}")


if __name__ == "__main__":
    unittest.main()
