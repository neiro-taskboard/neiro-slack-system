#!/usr/bin/env python3
"""Tests for hooks/scripts/slack-post-metadata.py.

Yama さんの metadata 照合の設計（2026-11-14）を取り込む送信経路のテスト。
実際の Slack 呼び出しはしない。urlopen と認証ストアは差し替える。
Run with: python3 -m unittest test_slack_post_metadata -v
"""
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout

SCRIPT_PATH = os.path.expanduser(
    "~/workspace/neiro-slack-system/hooks/scripts/slack-post-metadata.py")


def load_mod():
    spec = importlib.util.spec_from_file_location("slack_post_metadata",
                                                  SCRIPT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FAKE_TOKEN_BOARD = "FAKE-BOARD-TOKEN"
FAKE_TOKEN_DEFAULT = "FAKE-DEFAULT-TOKEN"


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


class PostMetadataTest(unittest.TestCase):
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

    def fake_urlopen(self, body=None, status_ok=True):
        def _open(req, timeout=30):
            self.calls.append(req)
            return FakeResponse(body if body is not None else
                                {"ok": True, "ts": "1794660000.000001",
                                 "channel": "C0CGYGT9V7G"})
        return _open

    def request_json(self, idx=0):
        return json.loads(self.calls[idx].data.decode("utf-8"))

    # 1. event_id 指定の payload の形（Yama さんと同じ形）
    def test_build_payload_event_id(self):
        p = self.mod.build_payload("C0CGYGT9V7G", "hello",
                                   event_id="T0CGV6V7161:C0CGYGT9V7G:1794652249.960829")
        self.assertEqual(p["metadata"]["event_type"], "neiro_reply")
        self.assertEqual(p["metadata"]["event_payload"],
                         {"event_id": "T0CGV6V7161:C0CGYGT9V7G:1794652249.960829"})
        self.assertNotIn("thread_ts", p)

    # 2. request_id 指定の payload の形（手動投稿）
    def test_build_payload_request_id(self):
        p = self.mod.build_payload("C0CGYGT9V7G", "hello",
                                   request_id="123e4567-e89b-12d3-a456-426614174000")
        self.assertEqual(p["metadata"]["event_payload"],
                         {"request_id": "123e4567-e89b-12d3-a456-426614174000"})

    # 3. thread_ts は指定したときだけ入る
    def test_build_payload_thread_ts(self):
        p = self.mod.build_payload("C0CGYGT9V7G", "hello",
                                   thread_ts="1794583788.831849",
                                   event_id="T0CGV6V7161:C0CGYGT9V7G:1.1")
        self.assertEqual(p["thread_ts"], "1794583788.831849")

    # 4. event_id / request_id はどちらか一方だけ
    def test_build_payload_both_or_neither_rejected(self):
        with self.assertRaises(ValueError):
            self.mod.build_payload("C0CGYGT9V7G", "x", event_id="a",
                                   request_id="b")
        with self.assertRaises(ValueError):
            self.mod.build_payload("C0CGYGT9V7G", "x")

    # 5. トークン解決：プロファイル名・ワークスペースID・デフォルト
    def test_resolve_token(self):
        self.assertEqual(self.mod.resolve_token("muse-board"),
                         FAKE_TOKEN_BOARD)
        self.assertEqual(self.mod.resolve_token("T0CGV6V7161"),
                         FAKE_TOKEN_BOARD)
        self.assertEqual(self.mod.resolve_token(None), FAKE_TOKEN_DEFAULT)
        self.assertIsNone(self.mod.resolve_token("unknown-workspace"))

    # 6. post 成功：URL・Authorization・body の metadata が正しい
    def test_post_success(self):
        payload = self.mod.build_payload(
            "C0CGYGT9V7G", "hello",
            event_id="T0CGV6V7161:C0CGYGT9V7G:1794652249.960829")
        ok, result = self.mod.post(payload, FAKE_TOKEN_BOARD,
                                   urlopen=self.fake_urlopen(),
                                   sleep=lambda s: None)
        self.assertTrue(ok)
        self.assertEqual(result["ts"], "1794660000.000001")
        self.assertEqual(len(self.calls), 1)
        req = self.calls[0]
        self.assertEqual(req.full_url, self.mod.SLACK_API)
        self.assertEqual(req.get_header("Authorization"),
                         "Bearer " + FAKE_TOKEN_BOARD)
        body = self.request_json()
        self.assertEqual(body["metadata"]["event_type"], "neiro_reply")
        self.assertEqual(body["metadata"]["event_payload"]["event_id"],
                         "T0CGV6V7161:C0CGYGT9V7G:1794652249.960829")

    # 7. Slack の ok:false はリトライせず失敗を返す
    def test_post_slack_error_no_retry(self):
        payload = self.mod.build_payload("C0CGYGT9V7G", "hello",
                                         event_id="T0CGV6V7161:C0CGYGT9V7G:1.1")
        ok, err = self.mod.post(
            payload, FAKE_TOKEN_BOARD,
            urlopen=self.fake_urlopen({"ok": False, "error": "channel_not_found"}),
            sleep=lambda s: None)
        self.assertFalse(ok)
        self.assertIn("channel_not_found", err)
        self.assertEqual(len(self.calls), 1)

    # 8. ratelimited は1回リトライして成功する
    def test_post_ratelimited_retries_once(self):
        bodies = [{"ok": False, "error": "ratelimited", "retry_after": 1},
                  {"ok": True, "ts": "1794660000.000002",
                   "channel": "C0CGYGT9V7G"}]
        idx = {"i": 0}

        def _open(req, timeout=30):
            self.calls.append(req)
            body = bodies[idx["i"]]
            idx["i"] += 1
            return FakeResponse(body)

        payload = self.mod.build_payload("C0CGYGT9V7G", "hello",
                                         event_id="T0CGV6V7161:C0CGYGT9V7G:1.1")
        ok, result = self.mod.post(payload, FAKE_TOKEN_BOARD, urlopen=_open,
                                   sleep=lambda s: None)
        self.assertTrue(ok)
        self.assertEqual(result["ts"], "1794660000.000002")
        self.assertEqual(len(self.calls), 2)

    # 9. main --dry-run は送らず、payload を出す。トークンは出力に出ない
    def test_main_dry_run(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = self.mod.main(["--channel", "C0CGYGT9V7G",
                                  "--message", "hello",
                                  "--event-id",
                                  "T0CGV6V7161:C0CGYGT9V7G:1794652249.960829",
                                  "--dry-run"])
        self.assertEqual(code, 0)
        out = json.loads(buf.getvalue())
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["payload"]["metadata"]["event_payload"]["event_id"],
                         "T0CGV6V7161:C0CGYGT9V7G:1794652249.960829")
        self.assertNotIn(FAKE_TOKEN_BOARD, buf.getvalue())
        self.assertNotIn(FAKE_TOKEN_DEFAULT, buf.getvalue())
        self.assertEqual(len(self.calls), 0)

    # 10. main 成功時の出力にトークンは出ない
    def test_main_success_output_has_no_token(self):
        orig = self.mod.post

        def _post(payload, token, **kw):
            self.assertEqual(token, FAKE_TOKEN_BOARD)
            return True, {"ok": True, "ts": "1794660000.000003",
                          "channel": "C0CGYGT9V7G"}

        self.mod.post = _post
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = self.mod.main(["--channel", "C0CGYGT9V7G",
                                      "--message", "hello",
                                      "--workspace", "muse-board",
                                      "--event-id",
                                      "T0CGV6V7161:C0CGYGT9V7G:1.1"])
            self.assertEqual(code, 0)
            out = json.loads(buf.getvalue())
            self.assertEqual(out["ts"], "1794660000.000003")
            self.assertNotIn(FAKE_TOKEN_BOARD, buf.getvalue())
        finally:
            self.mod.post = orig

    # 11. トークンが見つからないワークスペースは失敗（送らない）
    def test_main_token_not_found(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = self.mod.main(["--channel", "C0CGYGT9V7G",
                                  "--message", "hello",
                                  "--workspace", "unknown-workspace",
                                  "--event-id",
                                  "T0CGV6V7161:C0CGYGT9V7G:1.1"])
        self.assertEqual(code, 1)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["error"], "token_not_found")


if __name__ == "__main__":
    unittest.main(verbosity=2)
