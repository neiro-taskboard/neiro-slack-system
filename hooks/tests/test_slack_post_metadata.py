#!/usr/bin/env python3
"""Tests for hooks/scripts/slack-post-metadata.py.

Yama さんの metadata 照合の設計（2026-11-14）を取り込む送信経路のテスト。
実際の Slack 呼び出しはしない。送信は curl 経路が既定なので
subprocess.run を差し替えて curl の標準出力（JSON 本体＋
``\\n__STATUS__:<code>``）を模す。urllib フォールバック経路は
shutil.which を None にして urlopen 差し替えで確かめる。
認証ストアは差し替える。
Run with: python3 -m unittest test_slack_post_metadata -v
"""
import http.client
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

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


def curl_stdout(body, status=200):
    """curl -w で付く ``\\n__STATUS__:<code>`` 付きの標準出力を作る。"""
    return (json.dumps(body).encode("utf-8")
            + ("\n__STATUS__:%d" % status).encode("ascii"))


class FakeResponse:
    def __init__(self, body, headers=None, status=200):
        self._body = json.dumps(body).encode("utf-8")
        self.headers = headers or {}
        self.status = status

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
        self.calls = []       # urlopen 経由の呼び出し（フォールバック用）
        self.curl_calls = []  # curl 経由の呼び出し

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

    def fake_curl_run(self, responses):
        """subprocess.run の差し替え。responses は1回ずつ消費する。

        各要素は (body_dict, status)・body_dict（status 200）・
        stdout そのものの bytes・raise する例外のいずれか。
        呼び出しごとに argv・-K 設定ファイルから読んだトークン・
        --data ファイルから読んだ JSON body・URL を記録する。
        一時ファイルは _post_curl が呼んだ後に消すので、読むのは
        この中で済ませる。
        """
        idx = {"i": 0}

        def _run(argv, **kw):
            rec = {"argv": list(argv)}
            if "-K" in argv:
                cfg = argv[argv.index("-K") + 1]
                with open(cfg, encoding="utf-8") as f:
                    cfg_text = f.read()
                rec["token"] = cfg_text.partition("Bearer ")[2] \
                    .split('"')[0].strip()
            if "--data" in argv:
                data_path = argv[argv.index("--data") + 1]
                assert data_path.startswith("@")
                with open(data_path[1:], "rb") as f:
                    rec["body"] = json.loads(f.read().decode("utf-8"))
            rec["url"] = argv[-1]
            self.curl_calls.append(rec)
            item = responses[min(idx["i"], len(responses) - 1)]
            idx["i"] += 1
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, bytes):
                stdout = item
            else:
                body, status = (item if isinstance(item, tuple)
                                else (item, 200))
                stdout = curl_stdout(body, status)
            return SimpleNamespace(stdout=stdout, returncode=0)

        return _run

    def post(self, payload, token, responses):
        """curl 経路で post() を呼ぶ。urlopen は渡すが使われないはず。"""
        fake = self.fake_curl_run(responses)
        with mock.patch.object(self.mod.subprocess, "run",
                               side_effect=fake):
            return self.mod.post(payload, token,
                                 urlopen=self.fake_urlopen(),
                                 sleep=lambda s: None)

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
        ok, result = self.post(
            payload, FAKE_TOKEN_BOARD,
            [{"ok": True, "ts": "1794660000.000001",
              "channel": "C0CGYGT9V7G"}])
        self.assertTrue(ok)
        self.assertEqual(result["ts"], "1794660000.000001")
        self.assertEqual(len(self.curl_calls), 1)
        self.assertEqual(len(self.calls), 0)
        call = self.curl_calls[0]
        self.assertEqual(call["url"], self.mod.SLACK_API)
        self.assertEqual(call["token"], FAKE_TOKEN_BOARD)
        self.assertEqual(call["body"]["metadata"]["event_type"],
                         "neiro_reply")
        self.assertEqual(
            call["body"]["metadata"]["event_payload"]["event_id"],
            "T0CGV6V7161:C0CGYGT9V7G:1794652249.960829")

    # 7. Slack の ok:false はリトライせず失敗を返す
    def test_post_slack_error_no_retry(self):
        payload = self.mod.build_payload("C0CGYGT9V7G", "hello",
                                         event_id="T0CGV6V7161:C0CGYGT9V7G:1.1")
        ok, err = self.post(
            payload, FAKE_TOKEN_BOARD,
            [{"ok": False, "error": "channel_not_found"}])
        self.assertFalse(ok)
        self.assertIn("channel_not_found", err)
        self.assertEqual(len(self.curl_calls), 1)

    # 8. ratelimited は1回リトライして成功する
    def test_post_ratelimited_retries_once(self):
        payload = self.mod.build_payload("C0CGYGT9V7G", "hello",
                                         event_id="T0CGV6V7161:C0CGYGT9V7G:1.1")
        ok, result = self.post(
            payload, FAKE_TOKEN_BOARD,
            [{"ok": False, "error": "ratelimited", "retry_after": 1},
             {"ok": True, "ts": "1794660000.000002",
              "channel": "C0CGYGT9V7G"}])
        self.assertTrue(ok)
        self.assertEqual(result["ts"], "1794660000.000002")
        self.assertEqual(len(self.curl_calls), 2)

    # 9. ネットワークエラー（応答ロスト）は再送しない：投稿が届いた
    # かどうか不明のため。2026-11-15 の3重投稿の再現ケース。
    # curl の意味では、接続失敗で標準出力に __STATUS__ が付かない
    # （空出力）場合がこれに当たる。
    def test_post_network_error_no_retry(self):
        payload = self.mod.build_payload("C0CGYGT9V7G", "hello",
                                         event_id="T0CGV6V7161:C0CGYGT9V7G:1.1")
        ok, err = self.post(payload, FAKE_TOKEN_BOARD, [b""])
        self.assertFalse(ok)
        self.assertIn("network", err)
        self.assertEqual(len(self.curl_calls), 1)

    # 10. タイムアウトも応答ロストなので再送しない
    def test_post_timeout_no_retry(self):
        payload = self.mod.build_payload("C0CGYGT9V7G", "hello",
                                         event_id="T0CGV6V7161:C0CGYGT9V7G:1.1")
        ok, err = self.post(
            payload, FAKE_TOKEN_BOARD,
            [subprocess.TimeoutExpired(["curl"], 30)])
        self.assertFalse(ok)
        self.assertIn("network", err)
        self.assertEqual(len(self.curl_calls), 1)

    # 11. HTTP 500 も書き込みの有無が曖昧なため再送しない
    def test_post_http_500_no_retry(self):
        payload = self.mod.build_payload("C0CGYGT9V7G", "hello",
                                         event_id="T0CGV6V7161:C0CGYGT9V7G:1.1")
        ok, err = self.post(
            payload, FAKE_TOKEN_BOARD,
            [({"ok": False, "error": "internal_error"}, 500)])
        self.assertFalse(ok)
        self.assertIn("http_500", err)
        self.assertEqual(len(self.curl_calls), 1)

    # 12. HTTP 429 は未投稿の確定なので、従来通りリトライして成功する
    def test_post_http_429_retries_once(self):
        payload = self.mod.build_payload("C0CGYGT9V7G", "hello",
                                         event_id="T0CGV6V7161:C0CGYGT9V7G:1.1")
        ok, result = self.post(
            payload, FAKE_TOKEN_BOARD,
            [({"ok": False, "error": "ratelimited"}, 429),
             {"ok": True, "ts": "1794660000.000004",
              "channel": "C0CGYGT9V7G"}])
        self.assertTrue(ok)
        self.assertEqual(result["ts"], "1794660000.000004")
        self.assertEqual(len(self.curl_calls), 2)

    # 13. main --dry-run は送らず、payload を出す。トークンは出力に出ない
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
        self.assertEqual(len(self.curl_calls), 0)

    # 14. main 成功時の出力にトークンは出ない
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

    # 15. トークンが見つからないワークスペースは失敗（送らない）
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


    # 16. 応答の途中切断は応答ロストなので再送しない。
    # POST は冪等でないため、GET の fetch_page とは扱いが違う
    # （リトライしない、429 のみ再送の方針のまま）。
    # curl の意味では、__STATUS__ の付かない途切れた出力がこれに当たる。
    # 2026-11-15：read 側の IncompleteRead が送信前チェックを
    # fail-closed させたのと同じ抜けが post() にもあった。
    def test_post_incomplete_read_no_retry(self):
        payload = self.mod.build_payload("C0CGYGT9V7G", "hello",
                                         event_id="T0CGV6V7161:C0CGYGT9V7G:1.1")
        ok, err = self.post(payload, FAKE_TOKEN_BOARD, [b'{"ok": true'])
        self.assertFalse(ok)
        self.assertIn("network", err)
        self.assertEqual(len(self.curl_calls), 1)

    # 17. curl が無ければ urllib へフォールバックする（旧経路の温存）。
    def test_post_urllib_fallback_when_no_curl(self):
        payload = self.mod.build_payload(
            "C0CGYGT9V7G", "hello",
            event_id="T0CGV6V7161:C0CGYGT9V7G:1794652249.960829")
        with mock.patch.object(self.mod.shutil, "which",
                               return_value=None):
            ok, result = self.mod.post(
                payload, FAKE_TOKEN_BOARD,
                urlopen=self.fake_urlopen(), sleep=lambda s: None)
        self.assertTrue(ok)
        self.assertEqual(result["ts"], "1794660000.000001")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.curl_calls), 0)
        req = self.calls[0]
        self.assertEqual(req.full_url, self.mod.SLACK_API)
        self.assertEqual(req.get_header("Authorization"),
                         "Bearer " + FAKE_TOKEN_BOARD)


if __name__ == "__main__":
    unittest.main(verbosity=2)
