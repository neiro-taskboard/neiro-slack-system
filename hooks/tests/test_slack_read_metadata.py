#!/usr/bin/env python3
"""Tests for hooks/scripts/slack-read-metadata.py.

Yama さんの metadata 照合の設計（2026-11-14）の読み取り側のテスト。
実際の Slack 呼び出しはしない。urlopen と認証ストアは差し替える。
Run with: python3 -m unittest test_slack_read_metadata -v
"""
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

SCRIPT_PATH = os.path.expanduser(
    "~/workspace/neiro-slack-system/hooks/scripts/slack-read-metadata.py")


def load_mod():
    spec = importlib.util.spec_from_file_location("slack_read_metadata",
                                                  SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FAKE_TOKEN_BOARD = "FAKE-BOARD-TOKEN"
FAKE_TOKEN_DEFAULT = "FAKE-DEFAULT-TOKEN"

EVENT_ID = "T0CGV6V7161:C0CGYGT9V7G:1794652249.960829"
OTHER_EVENT_ID = "T0CGV6V7161:C0CGYGT9V7G:1794650000.000000"
REQUEST_ID = "123e4567-e89b-12d3-a456-426614174000"


class FakeResponse:
    def __init__(self, body, headers=None):
        self._body = json.dumps(body).encode("utf-8")
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def marked_msg(ts, user, event_id=None, request_id=None,
               event_type="neiro_reply"):
    payload = {}
    if event_id is not None:
        payload["event_id"] = event_id
    if request_id is not None:
        payload["request_id"] = request_id
    return {"ts": ts, "user": user, "text": "hello",
            "metadata": {"event_type": event_type,
                         "event_payload": payload}}


class ReadMetadataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mod = load_mod()
        self.store = os.path.join(self.tmp.name, "workspaces.json")
        with open(self.store, "w", encoding="utf-8") as f:
            json.dump({
                "default_workspace": "T0CGB25D6H5",
                "workspaces": {
                    "T0CGB25D6H5": {
                        "workspace_id": "T0CGB25D6H5",
                        "workspace_name": "Neiro Task Board",
                        "token": FAKE_TOKEN_DEFAULT},
                    "muse-board": {
                        "workspace_id": "T0CGV6V7161",
                        "workspace_name": "muse取締役会",
                        "token": FAKE_TOKEN_BOARD},
                },
            }, f)
        self.mod.WORKSPACES_JSON = self.store
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def fake_urlopen(self, bodies):
        """bodies: 返す応答のリスト（1回ずつ消費する）。"""
        idx = {"i": 0}

        def _open(req, timeout=30):
            self.calls.append(req)
            body = bodies[min(idx["i"], len(bodies) - 1)]
            idx["i"] += 1
            return FakeResponse(body)

        return _open

    def find(self, **kw):
        channel = kw.pop("channel", "C0CGYGT9V7G")
        token = kw.pop("token", FAKE_TOKEN_BOARD)
        urlopen = kw.pop("urlopen", None) or self.fake_urlopen(
            kw.pop("bodies", [{"ok": True, "messages": []}]))
        return self.mod.find_by_metadata(
            channel, **kw, token=token, urlopen=urlopen,
            sleep=lambda s: None)

    # 1. event_id 一致の投稿が見つかる（スレッド読み取り）
    def test_find_by_event_id_in_thread(self):
        bodies = [{"ok": True, "messages": [
            {"ts": "1794660000.000001", "user": "U0CH3MQH7PT",
             "text": "other"},
            marked_msg("1794660000.000002", "U0CH1B19D4J", event_id=EVENT_ID),
        ]}]
        out = self.find(thread_ts="1794583788.831849", event_id=EVENT_ID,
                        self_id="U0CH1B19D4J", bodies=bodies)
        self.assertTrue(out["found"])
        self.assertEqual(out["ts"], "1794660000.000002")
        self.assertEqual(out["checked"], 2)
        req = self.calls[0]
        self.assertIn("conversations.replies", req.full_url)
        self.assertIn("include_all_metadata=true", req.full_url)
        self.assertIn("ts=1794583788.831849", req.full_url)
        self.assertEqual(req.get_header("Authorization"),
                         "Bearer " + FAKE_TOKEN_BOARD)

    # 2. request_id 一致の投稿が見つかる（チャンネル読み取り）
    def test_find_by_request_id_in_channel(self):
        bodies = [{"ok": True, "messages": [
            marked_msg("1794660000.000003", "U0CH1B19D4J",
                       request_id=REQUEST_ID),
        ]}]
        out = self.find(request_id=REQUEST_ID, self_id="U0CH1B19D4J",
                        bodies=bodies)
        self.assertTrue(out["found"])
        self.assertEqual(out["ts"], "1794660000.000003")
        req = self.calls[0]
        self.assertIn("conversations.history", req.full_url)
        self.assertIn("include_all_metadata=true", req.full_url)

    # 3. 一致なし → found False
    def test_not_found(self):
        bodies = [{"ok": True, "messages": [
            marked_msg("1794660000.000001", "U0CH1B19D4J",
                       event_id=OTHER_EVENT_ID),
            {"ts": "1794660000.000002", "user": "U0CH1B19D4J",
             "text": "no metadata"},
        ]}]
        out = self.find(event_id=EVENT_ID, self_id="U0CH1B19D4J",
                        bodies=bodies)
        self.assertFalse(out["found"])
        self.assertIsNone(out["ts"])
        self.assertEqual(out["checked"], 2)

    # 4. 他者の同一 event_id は拾わない（self_id 指定時）
    def test_other_user_mark_ignored(self):
        bodies = [{"ok": True, "messages": [
            marked_msg("1794660000.000001", "U0CH3MQH7PT", event_id=EVENT_ID),
        ]}]
        out = self.find(event_id=EVENT_ID, self_id="U0CH1B19D4J",
                        bodies=bodies)
        self.assertFalse(out["found"])

    # 5. event_type が違う印は拾わない
    def test_other_event_type_ignored(self):
        bodies = [{"ok": True, "messages": [
            marked_msg("1794660000.000001", "U0CH1B19D4J", event_id=EVENT_ID,
                       event_type="other_bot_reply"),
        ]}]
        out = self.find(event_id=EVENT_ID, self_id="U0CH1B19D4J",
                        bodies=bodies)
        self.assertFalse(out["found"])

    # 6. event_id / request_id はどちらか一方だけ
    def test_both_or_neither_rejected(self):
        with self.assertRaises(ValueError):
            self.find(event_id="a", request_id="b", bodies=[])
        with self.assertRaises(ValueError):
            self.find(bodies=[])

    # 7. トークンが無ければ RuntimeError（出力にトークンは出ない）
    def test_token_not_found(self):
        with self.assertRaises(RuntimeError):
            self.mod.find_by_metadata(
                "C0CGYGT9V7G", event_id=EVENT_ID, workspace="no-such",
                urlopen=self.fake_urlopen([{"ok": True, "messages": []}]),
                sleep=lambda s: None)

    # 8. ページング：1ページ目になければ cursor を辿る
    def test_paging_follows_cursor(self):
        bodies = [
            {"ok": True, "has_more": True,
             "response_metadata": {"next_cursor": "abc"},
             "messages": [marked_msg("1794660000.000001", "U0CH1B19D4J",
                                     event_id=OTHER_EVENT_ID)]},
            {"ok": True, "has_more": False,
             "messages": [marked_msg("1794660000.000002", "U0CH1B19D4J",
                                     event_id=EVENT_ID)]},
        ]
        out = self.find(event_id=EVENT_ID, self_id="U0CH1B19D4J",
                        bodies=bodies)
        self.assertTrue(out["found"])
        self.assertEqual(out["ts"], "1794660000.000002")
        self.assertEqual(out["checked"], 2)
        self.assertEqual(len(self.calls), 2)
        self.assertIn("cursor=abc", self.calls[1].full_url)

    # 9. ratelimited はリトライして成功する
    def test_ratelimited_retries_once(self):
        bodies = [{"ok": False, "error": "ratelimited", "retry_after": 1},
                  {"ok": True, "messages": [
                      marked_msg("1794660000.000001", "U0CH1B19D4J",
                                 event_id=EVENT_ID)]}]
        out = self.find(event_id=EVENT_ID, self_id="U0CH1B19D4J",
                        bodies=bodies)
        self.assertTrue(out["found"])
        self.assertEqual(len(self.calls), 2)

    # 10. Slack の ok:false は RuntimeError（リトライしない）
    def test_slack_error_raises(self):
        bodies = [{"ok": False, "error": "channel_not_found"}]
        with self.assertRaises(RuntimeError):
            self.find(event_id=EVENT_ID, bodies=bodies)
        self.assertEqual(len(self.calls), 1)

    # 11. main 成功時の出力にトークンは出ない
    def test_main_output_has_no_token(self):
        orig = self.mod.find_by_metadata

        def _find(*a, **kw):
            return {"found": True, "ts": "1794660000.000001", "checked": 5}

        self.mod.find_by_metadata = _find
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = self.mod.main(["--channel", "C0CGYGT9V7G",
                                      "--event-id", EVENT_ID,
                                      "--workspace", "muse-board"])
        finally:
            self.mod.find_by_metadata = orig
        self.assertEqual(code, 0)
        out = json.loads(buf.getvalue())
        self.assertTrue(out["found"])
        self.assertEqual(out["ts"], "1794660000.000001")
        self.assertNotIn(FAKE_TOKEN_BOARD, buf.getvalue())
        self.assertNotIn(FAKE_TOKEN_DEFAULT, buf.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
