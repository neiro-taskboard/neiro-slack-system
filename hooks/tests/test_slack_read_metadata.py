#!/usr/bin/env python3
"""Tests for hooks/scripts/slack-read-metadata.py.

Yama さんの metadata 照合の設計（2026-11-14）の読み取り側のテスト。
実際の Slack 呼び出しはしない。読み取りは curl 経路が既定なので
subprocess.run を差し替えて curl の標準出力（JSON 本体＋
``\\n__STATUS__:<code>``）を模す。urllib フォールバック経路は
shutil.which を None にして urlopen 差し替えで確かめる。
認証ストアは差し替える。
Run with: python3 -m unittest test_slack_read_metadata -v
"""
import http.client
import importlib.util
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

def make_id(prefix, rest):
    """Build an ID-shaped string from parts (like a real Slack ID).

    Fixtures are synthetic (2026-11-21): the real IDs used before were
    other people's and ours; parts-built fakes keep the tests passing
    without leaking real IDs into the public repo.
    """
    return prefix + "0" + rest


WS_MUSE = make_id("T", "TESTWSMUSE1")
WS_TASK = make_id("T", "TESTWSTASK1")
CHANNEL = make_id("C", "TESTCHAN01")
SELF_ID = make_id("U", "TESTBOTMUSE")
USER_ID = make_id("U", "TESTHUMAN01")


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

EVENT_ID = WS_MUSE + ":" + CHANNEL + ":1794652249.960829"
OTHER_EVENT_ID = WS_MUSE + ":" + CHANNEL + ":1794650000.000000"
REQUEST_ID = "123e4567-e89b-12d3-a456-426614174000"


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
                "default_workspace": WS_TASK,
                "workspaces": {
                    WS_TASK: {
                        "workspace_id": WS_TASK,
                        "workspace_name": "Neiro Task Board",
                        "token": FAKE_TOKEN_DEFAULT},
                    "muse-board": {
                        "workspace_id": WS_MUSE,
                        "workspace_name": "muse取締役会",
                        "token": FAKE_TOKEN_BOARD},
                },
            }, f)
        self.mod.WORKSPACES_JSON = self.store
        self.calls = []       # urlopen 経由の呼び出し（フォールバック用）
        self.curl_calls = []  # curl 経由の呼び出し

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

    def fake_curl_run(self, responses):
        """subprocess.run の差し替え。responses は1回ずつ消費する。

        各要素は (body_dict, status)・body_dict（status 200）・
        stdout そのものの bytes・raise する例外のいずれか。
        呼び出しごとに argv・-K 設定ファイルから読んだトークン・
        URL（argv の末尾）を記録する。一時ファイルは
        _get_page_curl が呼んだ後に消すので、読むのはこの中で済ませる。
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

    def _curl_patched(self, responses):
        return mock.patch.object(self.mod.subprocess, "run",
                                 side_effect=self.fake_curl_run(responses))

    def find(self, **kw):
        channel = kw.pop("channel", CHANNEL)
        token = kw.pop("token", FAKE_TOKEN_BOARD)
        responses = kw.pop("responses",
                           kw.pop("bodies", [{"ok": True, "messages": []}]))
        with self._curl_patched(responses):
            return self.mod.find_by_metadata(
                channel, **kw, token=token,
                urlopen=self.fake_urlopen([{"ok": True, "messages": []}]),
                sleep=lambda s: None)

    def dump(self, **kw):
        channel = kw.pop("channel", CHANNEL)
        token = kw.pop("token", FAKE_TOKEN_BOARD)
        responses = kw.pop("responses",
                           kw.pop("bodies", [{"ok": True, "messages": []}]))
        with self._curl_patched(responses):
            return self.mod.dump_messages(
                channel, **kw, token=token,
                urlopen=self.fake_urlopen([{"ok": True, "messages": []}]),
                sleep=lambda s: None)

    # 1. event_id 一致の投稿が見つかる（スレッド読み取り）
    def test_find_by_event_id_in_thread(self):
        bodies = [{"ok": True, "messages": [
            {"ts": "1794660000.000001", "user": USER_ID,
             "text": "other"},
            marked_msg("1794660000.000002", SELF_ID, event_id=EVENT_ID),
        ]}]
        out = self.find(thread_ts="1794583788.831849", event_id=EVENT_ID,
                        self_id=SELF_ID, bodies=bodies)
        self.assertTrue(out["found"])
        self.assertEqual(out["ts"], "1794660000.000002")
        self.assertEqual(out["checked"], 2)
        self.assertEqual(len(self.curl_calls), 1)
        self.assertEqual(len(self.calls), 0)
        call = self.curl_calls[0]
        self.assertIn("conversations.replies", call["url"])
        self.assertIn("include_all_metadata=true", call["url"])
        self.assertIn("ts=1794583788.831849", call["url"])
        self.assertEqual(call["token"], FAKE_TOKEN_BOARD)

    # 2. request_id 一致の投稿が見つかる（チャンネル読み取り）
    def test_find_by_request_id_in_channel(self):
        bodies = [{"ok": True, "messages": [
            marked_msg("1794660000.000003", SELF_ID,
                       request_id=REQUEST_ID),
        ]}]
        out = self.find(request_id=REQUEST_ID, self_id=SELF_ID,
                        bodies=bodies)
        self.assertTrue(out["found"])
        self.assertEqual(out["ts"], "1794660000.000003")
        call = self.curl_calls[0]
        self.assertIn("conversations.history", call["url"])
        self.assertIn("include_all_metadata=true", call["url"])

    # 3. 一致なし → found False
    def test_not_found(self):
        bodies = [{"ok": True, "messages": [
            marked_msg("1794660000.000001", SELF_ID,
                       event_id=OTHER_EVENT_ID),
            {"ts": "1794660000.000002", "user": SELF_ID,
             "text": "no metadata"},
        ]}]
        out = self.find(event_id=EVENT_ID, self_id=SELF_ID,
                        bodies=bodies)
        self.assertFalse(out["found"])
        self.assertIsNone(out["ts"])
        self.assertEqual(out["checked"], 2)

    # 4. 他者の同一 event_id は拾わない（self_id 指定時）
    def test_other_user_mark_ignored(self):
        bodies = [{"ok": True, "messages": [
            marked_msg("1794660000.000001", USER_ID, event_id=EVENT_ID),
        ]}]
        out = self.find(event_id=EVENT_ID, self_id=SELF_ID,
                        bodies=bodies)
        self.assertFalse(out["found"])

    # 5. event_type が違う印は拾わない
    def test_other_event_type_ignored(self):
        bodies = [{"ok": True, "messages": [
            marked_msg("1794660000.000001", SELF_ID, event_id=EVENT_ID,
                       event_type="other_bot_reply"),
        ]}]
        out = self.find(event_id=EVENT_ID, self_id=SELF_ID,
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
                CHANNEL, event_id=EVENT_ID, workspace="no-such",
                urlopen=self.fake_urlopen([{"ok": True, "messages": []}]),
                sleep=lambda s: None)

    # 8. ページング：1ページ目になければ cursor を辿る
    def test_paging_follows_cursor(self):
        bodies = [
            {"ok": True, "has_more": True,
             "response_metadata": {"next_cursor": "abc"},
             "messages": [marked_msg("1794660000.000001", SELF_ID,
                                     event_id=OTHER_EVENT_ID)]},
            {"ok": True, "has_more": False,
             "messages": [marked_msg("1794660000.000002", SELF_ID,
                                     event_id=EVENT_ID)]},
        ]
        out = self.find(event_id=EVENT_ID, self_id=SELF_ID,
                        bodies=bodies)
        self.assertTrue(out["found"])
        self.assertEqual(out["ts"], "1794660000.000002")
        self.assertEqual(out["checked"], 2)
        self.assertEqual(len(self.curl_calls), 2)
        self.assertIn("cursor=abc", self.curl_calls[1]["url"])

    # 9. ratelimited はリトライして成功する
    def test_ratelimited_retries_once(self):
        bodies = [{"ok": False, "error": "ratelimited", "retry_after": 1},
                  {"ok": True, "messages": [
                      marked_msg("1794660000.000001", SELF_ID,
                                 event_id=EVENT_ID)]}]
        out = self.find(event_id=EVENT_ID, self_id=SELF_ID,
                        bodies=bodies)
        self.assertTrue(out["found"])
        self.assertEqual(len(self.curl_calls), 2)

    # 10. Slack の ok:false は RuntimeError（リトライしない）
    def test_slack_error_raises(self):
        bodies = [{"ok": False, "error": "channel_not_found"}]
        with self.assertRaises(RuntimeError):
            self.find(event_id=EVENT_ID, bodies=bodies)
        self.assertEqual(len(self.curl_calls), 1)

    # 11. main 成功時の出力にトークンは出ない
    def test_main_output_has_no_token(self):
        orig = self.mod.find_by_metadata

        def _find(*a, **kw):
            return {"found": True, "ts": "1794660000.000001", "checked": 5}

        self.mod.find_by_metadata = _find
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = self.mod.main(["--channel", CHANNEL,
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

    # 12. dump は投稿一覧を印付きで返す（スレッド読み取り）
    def test_dump_thread_returns_marks(self):
        bodies = [{"ok": True, "messages": [
            {"ts": "1794660000.000001", "user": SELF_ID,
             "text": "plain"},
            marked_msg("1794660000.000002", SELF_ID,
                       event_id=EVENT_ID),
            marked_msg("1794660000.000003", SELF_ID,
                       request_id=REQUEST_ID),
            marked_msg("1794660000.000004", SELF_ID,
                       event_id=EVENT_ID, event_type="yama_reply"),
        ]}]
        out = self.dump(thread_ts="1794583788.831849", bodies=bodies)
        self.assertEqual(out["checked"], 4)
        msgs = out["messages"]
        self.assertIsNone(msgs[0]["mark"])
        self.assertEqual(msgs[1]["mark"],
                         {"kind": "event_id", "value": EVENT_ID})
        self.assertEqual(msgs[2]["mark"],
                         {"kind": "request_id", "value": REQUEST_ID})
        # event_type が違う印は印なし扱い
        self.assertIsNone(msgs[3]["mark"])
        call = self.curl_calls[0]
        self.assertIn("conversations.replies", call["url"])
        self.assertIn("include_all_metadata=true", call["url"])
        self.assertIn("ts=1794583788.831849", call["url"])

    # 13. dump はチャンネル読み取りでは conversations.history を使う
    def test_dump_channel_uses_history(self):
        out = self.dump(bodies=[{"ok": True, "messages": []}])
        self.assertEqual(out["messages"], [])
        self.assertEqual(out["checked"], 0)
        self.assertIn("conversations.history", self.curl_calls[0]["url"])

    # 14. dump の読み取り失敗は RuntimeError
    def test_dump_slack_error_raises(self):
        with self.assertRaises(RuntimeError):
            self.dump(bodies=[{"ok": False, "error": "channel_not_found"}])
        self.assertEqual(len(self.curl_calls), 1)

    # 15. main --dump の出力にトークンは出ない
    def test_main_dump_output_has_no_token(self):
        orig = self.mod.dump_messages

        def _dump(*a, **kw):
            return {"messages": [{"ts": "1794660000.000001",
                                  "user": SELF_ID, "text": "hi",
                                  "mark": None}], "checked": 1}

        self.mod.dump_messages = _dump
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = self.mod.main(["--channel", CHANNEL,
                                      "--dump", "--workspace", "muse-board"])
        finally:
            self.mod.dump_messages = orig
        self.assertEqual(code, 0)
        out = json.loads(buf.getvalue())
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["messages"]), 1)
        self.assertEqual(out["checked"], 1)
        self.assertNotIn(FAKE_TOKEN_BOARD, buf.getvalue())
        self.assertNotIn(FAKE_TOKEN_DEFAULT, buf.getvalue())


    # 16. 応答の途中切断は GET なのでリトライして成功する。
    # curl の意味では、__STATUS__ の付かない途切れた出力がこれに当たる。
    # 2026-11-15：大きなスレッドの dump（約386KB）が途中で切れて
    # IncompleteRead になり、fetch_page から例外が抜けて送信前チェックが
    # fail-closed で返信を止めた。IncompleteRead は OSError / URLError の
    # サブクラスではないため、明示的に拾う必要がある。
    def test_incomplete_read_retries(self):
        responses = [b'{"ok": true',
                     {"ok": True, "messages": [
                         marked_msg("1794660000.000001", SELF_ID,
                                    event_id=EVENT_ID)]}]
        out = self.find(event_id=EVENT_ID, self_id=SELF_ID,
                        responses=responses)
        self.assertTrue(out["found"])
        self.assertEqual(out["ts"], "1794660000.000001")
        self.assertEqual(len(self.curl_calls), 2)

    # 17. curl が無ければ urllib へフォールバックする（旧経路の温存）。
    def test_fetch_page_urllib_fallback_when_no_curl(self):
        bodies = [{"ok": True, "messages": [
            marked_msg("1794660000.000001", SELF_ID,
                       event_id=EVENT_ID)]}]
        with mock.patch.object(self.mod.shutil, "which",
                               return_value=None):
            out = self.mod.find_by_metadata(
                CHANNEL, event_id=EVENT_ID, self_id=SELF_ID,
                token=FAKE_TOKEN_BOARD,
                urlopen=self.fake_urlopen(bodies),
                sleep=lambda s: None)
        self.assertTrue(out["found"])
        self.assertEqual(out["ts"], "1794660000.000001")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.curl_calls), 0)
        req = self.calls[0]
        self.assertIn("conversations.history", req.full_url)
        self.assertEqual(req.get_header("Authorization"),
                         "Bearer " + FAKE_TOKEN_BOARD)


if __name__ == "__main__":
    unittest.main(verbosity=2)
