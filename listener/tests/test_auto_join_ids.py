#!/usr/bin/env python3
"""Tests for the ids.json loader in auto-join.py (2026-11-21,
real-ID externalization, remaining 4 items: DRY_RUN seed check).

The DRY_RUN seed used to compare against a hard-coded workspace ID.
It now reads workspace_muse_torishimariyakai from ids.json and skips
the seed (fail-closed) when the file is unreadable.

No real Slack calls: only load_ids / ids_value are exercised.
ID-shaped fixture values are assembled at runtime (prefix + "0" +
rest) so no literal ID-shaped string sits in this file.
Run with: python3 test_auto_join_ids.py
"""
import importlib.util
import json
import os
import tempfile
import unittest

LISTENER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_module(name, filename):
    path = os.path.join(LISTENER_DIR, filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


auto_join = load_module("auto_join_ids_under_test", "auto-join.py")


def make_id(prefix, rest):
    return prefix + "0" + rest


class LoadIdsTest(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("NEIRO_SLACK_IDS_PATH")
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "ids.json")
        os.environ["NEIRO_SLACK_IDS_PATH"] = self.path

    def tearDown(self):
        if self._old is None:
            os.environ.pop("NEIRO_SLACK_IDS_PATH", None)
        else:
            os.environ["NEIRO_SLACK_IDS_PATH"] = self._old
        self.tmp.cleanup()

    def _write(self, data, mode=0o600):
        with open(self.path, "w", encoding="utf-8") as f:
            if isinstance(data, str):
                f.write(data)
            else:
                json.dump(data, f)
        os.chmod(self.path, mode)

    def test_reads_board_workspace_id(self):
        ws = make_id("T", "ESTWS1234")
        self._write({"workspace_muse_torishimariyakai": ws})
        ids = auto_join.load_ids()
        self.assertEqual(
            auto_join.ids_value(ids, "workspace_muse_torishimariyakai"),
            ws)

    def test_missing_file_is_none(self):
        self.assertIsNone(auto_join.load_ids())

    def test_bad_json_is_none(self):
        self._write("{not json")
        self.assertIsNone(auto_join.load_ids())

    def test_non_object_is_none(self):
        self._write("[1, 2]")
        self.assertIsNone(auto_join.load_ids())

    def test_readable_by_others_is_none(self):
        self._write({"workspace_muse_torishimariyakai":
                     make_id("T", "ESTWS1234")}, mode=0o644)
        self.assertIsNone(auto_join.load_ids())

    def test_non_id_shape_value_is_none(self):
        self._write({"workspace_muse_torishimariyakai": "not-an-id"})
        ids = auto_join.load_ids()
        self.assertIsNone(
            auto_join.ids_value(ids, "workspace_muse_torishimariyakai"))

    def test_missing_key_is_none(self):
        self._write({"workspace_neiro_task_board": make_id("T", "OTHER1234")})
        ids = auto_join.load_ids()
        self.assertIsNone(
            auto_join.ids_value(ids, "workspace_muse_torishimariyakai"))


if __name__ == "__main__":
    unittest.main()
