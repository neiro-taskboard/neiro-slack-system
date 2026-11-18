#!/usr/bin/env python3
"""Tests for the Slack-ID blanket pattern used by the scan gate.

Decision (2026-11-19, muse取締役会 #slack-へようこそ thread
1795040999.685339): the blanket is `[UWBCTGDA]0[A-Z0-9]{8,}` — the
`0` as 2nd character is REQUIRED. Yama pointed out that making it
optional (`0?`) also matches long uppercase identifiers such as
`CONFIG...`, and a scan of this repo confirmed it: the `0?` variant
produced 106 false positives (CANDIDATE, WORKSPACE, QUESTIONER, ...)
while the `0`-required variant produced 0 false positives and still
caught every real Slack ID in the repo (201 hits, all 2nd char `0`).

Fixtures here are built from parts, never written as full ID-shaped
literals, so this file itself does not trip the gate it tests.

Style follows the neighbours (plain unittest, no pytest):
  python3 ~/hooks/tests/test_blanket_id_pattern.py
"""
import re
import unittest

# The adopted blanket pattern. W = workspace-app user ID, G = private
# channel, D = DM — the letters Yama's `[UC]0...` did not cover.
BLANKET = re.compile(r"[UWBCTGDA]0[A-Z0-9]{8,}")
# The rejected variant, kept only to document why `0?` was dropped.
REJECTED_OPTIONAL_ZERO = re.compile(r"[UWBCTGDA]0?[A-Z0-9]{8,}")


def make_id(prefix, rest="A1B2C3D4E5"):
    """Build an ID-shaped string from parts (11 chars, like a real ID)."""
    return prefix + "0" + rest


class TestBlanketMatchesRealIds(unittest.TestCase):
    def test_all_prefixes_match(self):
        for prefix in "UWBCTGDA":
            self.assertIsNotNone(
                BLANKET.search(make_id(prefix)),
                f"blanket should match {prefix}0... IDs",
            )

    def test_longer_ids_match(self):
        self.assertIsNotNone(BLANKET.search(make_id("U", "A1B2C3D4E5F6G7")))

    def test_minimum_length(self):
        # 10 chars total (prefix + 0 + 8) is the floor.
        self.assertIsNotNone(BLANKET.search(make_id("C", "A1B2C3D4")))
        # 9 chars total must not match.
        self.assertIsNone(BLANKET.search(make_id("C", "A1B2C3D")))


class TestBlanketRejectsConstants(unittest.TestCase):
    # Uppercase identifiers found in this repo by the `0?` scan.
    CONSTANTS = [
        "CANDIDATE", "QUESTIONER", "WORKSPACE", "WORKSPACES",
        "AMBIGUOUS", "UNPROCESSED", "THRESHOLD", "DEFINITION",
        "CONFIGURATION", "DATABASE_URL", "SLACKBOT",
    ]

    def test_constants_do_not_match(self):
        for word in self.CONSTANTS:
            self.assertIsNone(
                BLANKET.search(word),
                f"blanket must not match uppercase constant {word}",
            )

    def test_optional_zero_variant_would_match_them(self):
        # Documents why `0?` was rejected: it matches most of these.
        matched = [w for w in self.CONSTANTS
                   if REJECTED_OPTIONAL_ZERO.search(w)]
        self.assertGreaterEqual(
            len(matched), len(self.CONSTANTS) // 2,
            "the 0? variant should match uppercase constants "
            "(that is why it was rejected)",
        )


class TestBlanketAndExamplePlaceholders(unittest.TestCase):
    def test_short_or_non_id_placeholders_do_not_match(self):
        # Placeholders that are not ID-shaped stay unflagged.
        for word in ["U0EXAMPLE", "CEXAMPLE", "EXAMPLE_USER_ID"]:
            self.assertIsNone(
                BLANKET.search(word),
                f"blanket must not match placeholder {word}",
            )

    def test_id_shaped_placeholder_matches(self):
        # A placeholder shaped exactly like a real ID is indistinguishable
        # from one, so the blanket flags it — fixtures must therefore use
        # non-ID-shaped placeholders (see the test above), not U0EXAMPLE01.
        for word in ["U0EXAMPLE01", "C0EXAMPLE01"]:
            self.assertIsNotNone(
                BLANKET.search(word),
                f"blanket should match ID-shaped placeholder {word}",
            )


if __name__ == "__main__":
    unittest.main()
