#!/usr/bin/env python3
"""Tests for the auto-join opt-out (auto-join.py), added 2026-11-18
with Yama's "removed channel is never auto-joined again" idea.

Without it, a channel the bot was removed from looks exactly like
a channel it never joined, so the next 5-minute check would rejoin
it within minutes of being kicked.

No real Slack calls: only the pure state functions and the state
file round-trip are exercised.
Run with: python3 test_auto_join_optout.py
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


auto_join = load_module("auto_join_under_test", "auto-join.py")

WID = "T0TEST"
USER = "U0USER"


def ch(cid, member=False, num=3, private=False, archived=False):
    return {"id": cid, "name": cid.lower(), "is_member": member,
            "num_members": num, "is_private": private,
            "is_archived": archived}


class OptOutTest(unittest.TestCase):
    def test_removed_channel_is_not_a_candidate(self):
        # Bot was a member, now is not: recorded as removed, and a
        # later candidate check refuses it even though the user is a
        # member and the channel is public with 3 members.
        joined, removed = auto_join.update_join_state(
            [ch("C1", member=True)], WID, set(), set())
        self.assertIn("T0TEST:C1", joined)
        joined, removed = auto_join.update_join_state(
            [ch("C1", member=False)], WID, joined, removed)
        self.assertNotIn("T0TEST:C1", joined)
        self.assertIn("T0TEST:C1", removed)
        self.assertFalse(auto_join.is_candidate(
            ch("C1", member=False), [USER], USER, removed, WID))

    def test_manual_reinvite_clears_removed(self):
        # Removed once, then invited back by hand: the removal record
        # is cleared, so a deliberate re-invite still works.
        joined, removed = auto_join.update_join_state(
            [ch("C1", member=False)], WID, {"T0TEST:C1"}, set())
        self.assertIn("T0TEST:C1", removed)
        joined, removed = auto_join.update_join_state(
            [ch("C1", member=True)], WID, joined, removed)
        self.assertIn("T0TEST:C1", joined)
        self.assertNotIn("T0TEST:C1", removed)

    def test_never_joined_channel_is_still_a_candidate(self):
        # No history at all: the old behaviour is unchanged.
        joined, removed = auto_join.update_join_state(
            [ch("C1", member=False)], WID, set(), set())
        self.assertEqual(joined, set())
        self.assertEqual(removed, set())
        self.assertTrue(auto_join.is_candidate(
            ch("C1", member=False), [USER], USER, removed, WID))

    def test_baseline_channel_counts_as_removed_from_first_run(self):
        # The listener has a baseline for C1 (it saw messages there),
        # but the bot is no longer a member: removed without needing
        # a run during membership to notice.
        joined, removed = auto_join.update_join_state(
            [ch("C1", member=False)], WID, set(), set(),
            baselines={(WID, "C1")})
        self.assertIn("T0TEST:C1", removed)
        self.assertFalse(auto_join.is_candidate(
            ch("C1", member=False), [USER], USER, removed, WID))

    def test_private_channel_is_not_tracked(self):
        # Private channels are never auto-join candidates, so a
        # removal there must not pollute the state.
        joined, removed = auto_join.update_join_state(
            [ch("C1", member=True, private=True)], WID, set(), set())
        self.assertEqual(joined, set())
        joined, removed = auto_join.update_join_state(
            [ch("C1", member=False, private=True)], WID,
            {"T0TEST:C1"}, set())
        self.assertEqual(removed, set())

    def test_removed_stays_removed_across_runs(self):
        # A second run with the same list does not flip it back.
        joined, removed = auto_join.update_join_state(
            [ch("C1", member=False)], WID, set(), {"T0TEST:C1"})
        self.assertIn("T0TEST:C1", removed)
        joined, removed = auto_join.update_join_state(
            [ch("C1", member=False)], WID, joined, removed)
        self.assertIn("T0TEST:C1", removed)

    def test_other_workspace_channel_is_untouched(self):
        # State keys carry the workspace id, so another workspace's
        # channel with the same id is a different entry.
        joined, removed = auto_join.update_join_state(
            [ch("C1", member=True)], "T0OTHER", set(), set())
        self.assertNotIn("T0TEST:C1", joined)
        self.assertIn("T0OTHER:C1", joined)


class StateFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "auto_join_state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip(self):
        auto_join.save_join_state(
            self.path, {"T0TEST:C1"}, {"T0TEST:C2"})
        joined, removed = auto_join.load_join_state(self.path)
        self.assertEqual(joined, {"T0TEST:C1"})
        self.assertEqual(removed, {"T0TEST:C2"})

    def test_missing_file_starts_empty(self):
        joined, removed = auto_join.load_join_state(self.path)
        self.assertEqual(joined, set())
        self.assertEqual(removed, set())

    def test_corrupt_file_starts_empty(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        joined, removed = auto_join.load_join_state(self.path)
        self.assertEqual(joined, set())
        self.assertEqual(removed, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
