#!/usr/bin/env python3
"""Tests for slack-answered-check.py — DLQ resend answer check.

Style follows test_slack_poll.py (plain Python, no pytest):
  python3 ~/hooks/tests/test_slack_answered_check.py

The module-level check() is tested with stubbed readers (same approach as
Yanayana's reconcile tests, which inject fake readers so no real Slack
call is made). One CLI end-to-end test runs the real script against a
fake slackcli executable in a temp dir.

Covered:
  1. no third-party reply after the event -> ok_to_resend
  2. third-party text reply after the event -> answered_by_other
  3. time ordering: a reply before the event ts does not count
     (Yanayana's _is_reply_after_event, adopted 2026-11-14 from Yama)
  4. questioner / self posts do not count; empty-text posts do not count
  4b. bot posts and short (<=10 chars) posts do not count as answered
      (narrowed condition from Yama, 2026-11-14; Yanayana adopted the same)
  5. parent missing from channel history -> parent_deleted
  6. unreadable thread / channel -> cannot_verify
  7. windowed read (Yama's method, adopted 2026-11-14): the thread read
     starts 60s before the event and reads exactly one page
  8. no answer inside the window but has_more -> cannot_verify
     (fail-closed: the answer may be beyond the window)
  9. an answer inside the window counts even when has_more
 10. CLI end-to-end with a fake slackcli: ok_to_resend exits 0,
     unreadable history exits 2; --self-id is honoured
 11. self_user_id fallback reads ids.json (2026-11-19 externalization):
 12. agy review fixes: whoami garbage output and B... ids are
     rejected, load_ids rejects bad perms/JSON/shapes, and the CLI
     falls back through ids.json end-to-end
     names and workspace IDs resolve, unknown workspaces and a missing
     ids.json fail closed (None), and the CLI reports cannot_verify
     with reason self_id_unknown instead of judging with a wrong self
"""
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile

CHECK_PY = os.environ.get(
    "SLACK_ANSWERED_CHECK_PY",
    os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "scripts",
        "slack-answered-check.py")))

PASS = 0
FAIL = 0

def make_id(prefix, rest):
    """Build an ID-shaped string from parts (like a real Slack ID).

    Fixtures are synthetic (2026-11-19): the real bot/user/channel IDs
    used before were other people's and ours; parts-built fakes keep
    this file free of real IDs without changing what the tests check
    (the module compares IDs as opaque strings).
    """
    return prefix + "0" + rest


QUESTIONER = make_id("U", "TESTQUEST1")
SELF = make_id("U", "TESTSELF01")
OTHER = make_id("U", "TESTOTHER1")
EVENT_TS = "1794583788.831849"
THREAD_TS = "1794583788.831849"
CHANNEL = make_id("C", "TESTCHAN01")
BOT_MUSE = make_id("U", "TESTBOTMUSE")
BOT_TASK = make_id("U", "TESTBOTTASK")
WS_MUSE = make_id("T", "TESTWSMUSE1")
WS_TASK = make_id("T", "TESTWSTASK1")


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


def load_check():
    spec = importlib.util.spec_from_file_location("slack_answered_check",
                                                  CHECK_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def thread_page(messages, has_more=False, next_oldest=None):
    return {"messages": messages, "has_more": has_more,
            "next_oldest": next_oldest}


def channel_with_parent():
    def reader(channel, oldest):
        return thread_page([{"ts": THREAD_TS, "user": QUESTIONER,
                             "text": "parent"}])
    return reader


def channel_without_parent():
    def reader(channel, oldest):
        return thread_page([{"ts": "1794584000.000000", "user": OTHER,
                             "text": "something else"}])
    return reader


def failing_reader(*args):
    raise RuntimeError("boom: history unreadable")


def run_check(mod, messages, channel_reader=None, page_reader=None):
    """check() with a single-page thread reader unless overridden."""
    if page_reader is None:
        def page_reader(ch, th, oldest):
            return thread_page(messages)
    return mod.check(CHANNEL, EVENT_TS, THREAD_TS, QUESTIONER, SELF,
                     page_reader, channel_reader or channel_with_parent())


def test_ok_to_resend():
    mod = load_check()
    msgs = [
        {"ts": "1794583868.235619", "user": QUESTIONER, "text": "質問の続き"},
        {"ts": "1794584770.752989", "user": SELF, "text": "ねいろの返信"},
    ]
    out = run_check(mod, msgs)
    check("questioner/self posts only -> ok_to_resend",
          out["verdict"] == "ok_to_resend", out)
    check("ok_to_resend reports checked count",
          out.get("checked_messages") == 2, out)


def test_answered_by_other():
    mod = load_check()
    msgs = [
        {"ts": "1794583868.235619", "user": QUESTIONER, "text": "質問の続き"},
        {"ts": "1794585000.000000", "user": OTHER, "text": "こっちで答えますね、確認しました"},
    ]
    out = run_check(mod, msgs)
    check("third-party text reply after event -> answered_by_other",
          out["verdict"] == "answered_by_other", out)
    check("answered_by_other reports the answering ts",
          out.get("answered_by_ts") == "1794585000.000000", out)


def test_time_ordering():
    mod = load_check()
    # 他者の投稿でも、イベントより前なら答え済みにしない（時刻順序）。
    # 親 ts より前の投稿はスレッド走査にはそもそも入らないが、check() に
    # 渡された場合は is_reply_after_event ではじく。
    msgs = [
        {"ts": "1794583000.000000", "user": OTHER, "text": "前の話題への返信"},
    ]
    out = run_check(mod, msgs)
    check("reply before the event ts does not count as answered",
          out["verdict"] == "ok_to_resend", out)
    check("is_reply_after_event: equal ts is not after",
          mod.is_reply_after_event(EVENT_TS, EVENT_TS) is False)
    check("is_reply_after_event: missing ts never matches",
          mod.is_reply_after_event(None, EVENT_TS) is False)
    check("is_reply_after_event: later ts counts",
          mod.is_reply_after_event("1794584000.000000", EVENT_TS) is True)


def test_empty_text_does_not_count():
    mod = load_check()
    msgs = [
        {"ts": "1794585000.000000", "user": OTHER, "text": "   "},
        {"ts": "1794585100.000000", "user": OTHER, "text": ""},
    ]
    out = run_check(mod, msgs)
    check("empty-text posts do not count as answered",
          out["verdict"] == "ok_to_resend", out)


def test_bot_and_short_posts_do_not_count():
    mod = load_check()
    # 他の bot の自動応答（本文が長くても）は答え済みにしない。
    msgs = [
        {"ts": "1794585000.000000", "user": OTHER, "bot_id": "B0CH1QBBNUA",
         "text": "こっちで答えますね、確認しました"},
    ]
    out = run_check(mod, msgs)
    check("bot post does not count as answered",
          out["verdict"] == "ok_to_resend", out)
    # 人の投稿でも本文が 10 文字以下なら答え済みにしない（短い相槌）。
    msgs = [
        {"ts": "1794585000.000000", "user": OTHER, "text": "ありがとう"},
        {"ts": "1794585100.000000", "user": OTHER, "text": "あいうえおかきくけこ"},
    ]
    out = run_check(mod, msgs)
    check("short posts (<=10 chars) do not count as answered",
          out["verdict"] == "ok_to_resend", out)
    # 境界：11 文字なら答え済みになる。
    msgs = [
        {"ts": "1794585000.000000", "user": OTHER, "text": "あいうえおかきくけこさ"},
    ]
    out = run_check(mod, msgs)
    check("11-char post counts as answered",
          out["verdict"] == "answered_by_other", out)


def test_parent_deleted():
    mod = load_check()
    out = run_check(mod, [], channel_reader=channel_without_parent())
    check("parent missing from channel history -> parent_deleted",
          out["verdict"] == "parent_deleted", out)
    check("parent_deleted reports the parent ts",
          out.get("parent_ts") == THREAD_TS, out)


def test_cannot_verify():
    mod = load_check()
    out = run_check(mod, [], channel_reader=failing_reader)
    check("unreadable channel history -> cannot_verify",
          out["verdict"] == "cannot_verify", out)
    out2 = mod.check(CHANNEL, EVENT_TS, THREAD_TS, QUESTIONER, SELF,
                     failing_reader, channel_with_parent())
    check("unreadable thread -> cannot_verify",
          out2["verdict"] == "cannot_verify", out2)


def test_window_single_read_from_event_minus_60():
    mod = load_check()
    calls = []

    def page_reader(ch, th, oldest):
        calls.append(oldest)
        return thread_page([{"ts": "1794583868.235619", "user": QUESTIONER,
                             "text": "質問の続き"}])

    out = mod.check(CHANNEL, EVENT_TS, THREAD_TS, QUESTIONER, SELF,
                    page_reader, channel_with_parent())
    check("windowed read: no answer, no more pages -> ok_to_resend",
          out["verdict"] == "ok_to_resend", out)
    check("windowed read: thread is read exactly once",
          len(calls) == 1, calls)
    expected = float(EVENT_TS) - mod.SCAN_ORIGIN_LEAD_SECS
    check("windowed read: origin is 60s before the event",
          len(calls) == 1 and abs(float(calls[0]) - expected) < 1e-6,
          calls)
    check("windowed read: origin lead is 60 seconds",
          mod.SCAN_ORIGIN_LEAD_SECS == 60, mod.SCAN_ORIGIN_LEAD_SECS)
    check("windowed read: page limit is 100",
          mod.SCAN_PAGE_LIMIT == 100, mod.SCAN_PAGE_LIMIT)


def test_answer_beyond_window_is_fail_closed():
    mod = load_check()
    # 窓の中に答えはなく、まだ後続がある -> 断定できないので cannot_verify。
    def page_reader(ch, th, oldest):
        return thread_page([{"ts": "1794583868.235619", "user": QUESTIONER,
                             "text": "質問の続き"}],
                           has_more=True, next_oldest="1794584000.000000")

    out = mod.check(CHANNEL, EVENT_TS, THREAD_TS, QUESTIONER, SELF,
                    page_reader, channel_with_parent())
    check("no answer in window + has_more -> cannot_verify (fail-closed)",
          out["verdict"] == "cannot_verify", out)
    check("fail-closed reason names the window",
          out.get("reason") == "answer_may_be_beyond_window", out)


def test_answer_within_window_counts_even_with_more():
    mod = load_check()
    # 窓の中に答えがあれば、後続があっても答え済みで確定できる。
    def page_reader(ch, th, oldest):
        return thread_page(
            [{"ts": "1794585000.000000", "user": OTHER,
              "text": "こっちで答えますね、確認しました"}],
            has_more=True, next_oldest="1794586000.000000")

    out = mod.check(CHANNEL, EVENT_TS, THREAD_TS, QUESTIONER, SELF,
                    page_reader, channel_with_parent())
    check("answer inside window + has_more -> answered_by_other",
          out["verdict"] == "answered_by_other", out)


FAKE_SLACKCLI = """#!/usr/bin/env python3
import json, os, sys
if "auth" in sys.argv:
    print("User: neiro (__SELF_ID__, bot __BOT_ID__)")
    sys.exit(0)
if os.environ.get("FAKE_READ_FAIL") == "1":
    print("boom", file=sys.stderr)
    sys.exit(1)
kind = "thread" if "--thread-ts" in sys.argv else "channel"
path = os.environ.get("FAKE_" + kind.upper() + "_JSON", "")
with open(path, encoding="utf-8") as f:
    print(f.read())
"""


def test_cli_end_to_end():
    with tempfile.TemporaryDirectory() as d:
        fake = os.path.join(d, "slackcli")
        with open(fake, "w", encoding="utf-8") as f:
            f.write(FAKE_SLACKCLI.replace("__SELF_ID__", SELF)
                    .replace("__BOT_ID__", make_id("B", "TESTBOT001")))
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR)
        thread_path = os.path.join(d, "thread.json")
        channel_path = os.path.join(d, "channel.json")
        with open(thread_path, "w", encoding="utf-8") as f:
            json.dump({"messages": [
                {"ts": "1794583868.235619", "user": QUESTIONER,
                 "text": "質問の続き"},
                {"ts": "1794585000.000000", "user": OTHER,
                 "text": "こっちで答えますね、確認しました"}],
                "has_more": False, "next_oldest": None}, f)
        with open(channel_path, "w", encoding="utf-8") as f:
            json.dump({"messages": [{"ts": THREAD_TS, "user": QUESTIONER,
                                     "text": "parent"}],
                       "has_more": False, "next_oldest": None}, f)
        env = dict(os.environ)
        env["FAKE_THREAD_JSON"] = thread_path
        env["FAKE_CHANNEL_JSON"] = channel_path
        env.pop("FAKE_READ_FAIL", None)
        r = subprocess.run(
            [sys.executable, CHECK_PY, "--channel", CHANNEL,
             "--event-ts", EVENT_TS, "--questioner", QUESTIONER,
             "--workspace", "muse-board", "--slackcli", fake],
            capture_output=True, text=True, env=env, timeout=30)
        out = json.loads(r.stdout)
        check("CLI: answered thread -> exit 0 with answered_by_other",
              r.returncode == 0 and out["verdict"] == "answered_by_other",
              (r.returncode, r.stdout, r.stderr))
        # 読み取り失敗 -> cannot_verify、終了コード 2。
        env["FAKE_READ_FAIL"] = "1"
        r2 = subprocess.run(
            [sys.executable, CHECK_PY, "--channel", CHANNEL,
             "--event-ts", EVENT_TS, "--questioner", QUESTIONER,
             "--workspace", "muse-board", "--slackcli", fake],
            capture_output=True, text=True, env=env, timeout=120)
        out2 = json.loads(r2.stdout)
        check("CLI: unreadable history -> exit 2 with cannot_verify",
              r2.returncode == 2 and out2["verdict"] == "cannot_verify",
              (r2.returncode, r2.stdout, r2.stderr))
        # 引数不足 -> 終了コード 2。
        r3 = subprocess.run(
            [sys.executable, CHECK_PY, "--channel", CHANNEL],
            capture_output=True, text=True, timeout=30)
        check("CLI: missing required args -> exit 2", r3.returncode == 2,
              (r3.returncode, r3.stdout, r3.stderr))


FAKE_FAILING_SLACKCLI = """#!/usr/bin/env python3
import sys
sys.exit(1)
"""


def _write_fake_cli(d, body):
    path = os.path.join(d, "slackcli")
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


def _write_ids(d, data):
    path = os.path.join(d, "ids.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.chmod(path, 0o600)
    return path


IDS_FIXTURE = {
    "workspace_muse_torishimariyakai": WS_MUSE,
    "workspace_neiro_task_board": WS_TASK,
    "bot_neiro_muse_torishimariyakai": BOT_MUSE,
    "bot_neiro_neiro_task_board": BOT_TASK,
}


def test_self_id_fallback_from_ids():
    # whoami fails -> ids.json decides, for names and workspace IDs.
    mod = load_check()
    with tempfile.TemporaryDirectory() as d:
        fake = _write_fake_cli(d, FAKE_FAILING_SLACKCLI)
        ids_path = _write_ids(d, IDS_FIXTURE)
        old = os.environ.get("NEIRO_SLACK_IDS_PATH")
        os.environ["NEIRO_SLACK_IDS_PATH"] = ids_path
        try:
            check("fallback: default workspace -> task bot",
                  mod.self_user_id("", fake) == BOT_TASK)
            check("fallback: muse-board name -> muse bot",
                  mod.self_user_id("muse-board", fake) == BOT_MUSE)
            check("fallback: muse workspace ID -> muse bot",
                  mod.self_user_id(WS_MUSE, fake) == BOT_MUSE)
            check("fallback: task workspace ID -> task bot",
                  mod.self_user_id(WS_TASK, fake) == BOT_TASK)
            check("fallback: unknown name -> None (fail-closed)",
                  mod.self_user_id("muse_board", fake) is None)
            check("fallback: unknown workspace ID -> None",
                  mod.self_user_id(make_id("T", "TESTWSOTH1"), fake)
                  is None)
        finally:
            if old is None:
                os.environ.pop("NEIRO_SLACK_IDS_PATH", None)
            else:
                os.environ["NEIRO_SLACK_IDS_PATH"] = old


def test_self_id_missing_ids_fails_closed():
    # whoami fails and ids.json is missing -> None, never a default.
    mod = load_check()
    with tempfile.TemporaryDirectory() as d:
        fake = _write_fake_cli(d, FAKE_FAILING_SLACKCLI)
        old = os.environ.get("NEIRO_SLACK_IDS_PATH")
        os.environ["NEIRO_SLACK_IDS_PATH"] = os.path.join(d, "nope.json")
        try:
            check("missing ids.json: default -> None",
                  mod.self_user_id("", fake) is None)
            check("missing ids.json: muse-board -> None",
                  mod.self_user_id("muse-board", fake) is None)
        finally:
            if old is None:
                os.environ.pop("NEIRO_SLACK_IDS_PATH", None)
            else:
                os.environ["NEIRO_SLACK_IDS_PATH"] = old


FAKE_ABNORMAL_SLACKCLI = """#!/usr/bin/env python3
import sys
if "auth" in sys.argv:
    # exit 1, but stdout still carries an error-looking "(...)" string.
    print("Error: (UNAUTHORIZED) please log in")
    sys.exit(1)
sys.exit(1)
"""

FAKE_GRID_SLACKCLI = """#!/usr/bin/env python3
import sys
if "auth" in sys.argv:
    print("User: neiro (__GRID_ID__, bot __BOT_ID__)")
    sys.exit(0)
sys.exit(1)
"""


def _set_ids_env(d, data=None, raw=None, mode=0o600):
    """Point NEIRO_SLACK_IDS_PATH at a temp ids file; returns old value."""
    path = os.path.join(d, "ids.json")
    if raw is not None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw)
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    os.chmod(path, mode)
    old = os.environ.get("NEIRO_SLACK_IDS_PATH")
    os.environ["NEIRO_SLACK_IDS_PATH"] = path
    return old


def _restore_ids_env(old):
    if old is None:
        os.environ.pop("NEIRO_SLACK_IDS_PATH", None)
    else:
        os.environ["NEIRO_SLACK_IDS_PATH"] = old


def test_whoami_garbage_output_falls_back():
    # whoami exits 1 but prints "(UNAUTHORIZED)": the regex would
    # match U... without the returncode/shape checks, returning a
    # bogus self id. It must fall back to ids.json instead (agy).
    mod = load_check()
    with tempfile.TemporaryDirectory() as d:
        fake = _write_fake_cli(d, FAKE_ABNORMAL_SLACKCLI)
        old = _set_ids_env(d, IDS_FIXTURE)
        try:
            check("whoami garbage output -> ids.json fallback",
                  mod.self_user_id("muse-board", fake) == BOT_MUSE)
        finally:
            _restore_ids_env(old)


def test_whoami_grid_user_id_is_used():
    # A W-prefixed Enterprise Grid user ID from whoami is used as-is.
    mod = load_check()
    with tempfile.TemporaryDirectory() as d:
        grid = make_id("W", "TESTGRID01")
        fake = _write_fake_cli(
            d, FAKE_GRID_SLACKCLI.replace("__GRID_ID__", grid)
            .replace("__BOT_ID__", make_id("B", "TESTBOT001")))
        check("whoami W... id is used",
              mod.self_user_id("muse-board", fake) == grid)


def test_load_ids_rejects_bad_inputs():
    # 0644 perms, bad JSON, empty file, non-dict JSON, and a
    # non-ID-shaped value must all fail closed (None).
    mod = load_check()
    with tempfile.TemporaryDirectory() as d:
        old = _set_ids_env(d, IDS_FIXTURE, mode=0o644)
        try:
            check("load_ids: 0644 file -> None",
                  mod.load_ids() is None)
        finally:
            _restore_ids_env(old)
        for raw, name in (("{not json", "bad JSON"),
                          ("", "empty file"),
                          ("[]", "non-dict JSON"),
                          ('"just a string"', "string JSON")):
            old = _set_ids_env(d, None, raw=raw)
            try:
                check(f"load_ids: {name} -> None",
                      mod.load_ids() is None)
            finally:
                _restore_ids_env(old)
        old = _set_ids_env(d, {"bot_neiro_muse_torishimariyakai": "abc"})
        try:
            check("ids_value: bad shape -> None",
                  mod.ids_value(mod.load_ids(),
                                "bot_neiro_muse_torishimariyakai",
                                mod.USER_ID_SHAPE) is None)
            check("ids_value: B... bot id rejected as user id",
                  mod.ids_value({"k": make_id("B", "TESTBOT001")}, "k",
                                mod.USER_ID_SHAPE) is None)
        finally:
            _restore_ids_env(old)


def test_cli_fallback_completes_verdict():
    # whoami fails but ids.json is fine: the CLI must fall back and
    # finish the verdict instead of failing closed.
    with tempfile.TemporaryDirectory() as d:
        fake = _write_fake_cli(d, FAKE_FAILING_SLACKCLI)
        thread_path = os.path.join(d, "thread.json")
        channel_path = os.path.join(d, "channel.json")
        with open(thread_path, "w", encoding="utf-8") as f:
            json.dump({"messages": [
                {"ts": "1794583868.235619", "user": QUESTIONER,
                 "text": "質問の続き"},
                {"ts": "1794585000.000000", "user": OTHER,
                 "text": "こっちで答えますね、確認しました"}],
                "has_more": False, "next_oldest": None}, f)
        with open(channel_path, "w", encoding="utf-8") as f:
            json.dump({"messages": [{"ts": THREAD_TS, "user": QUESTIONER,
                                     "text": "parent"}],
                       "has_more": False, "next_oldest": None}, f)
        env = dict(os.environ)
        env["FAKE_THREAD_JSON"] = thread_path
        env["FAKE_CHANNEL_JSON"] = channel_path
        # The failing stub exits 1 for every call, so the history
        # reads would fail too; reuse the working fake for reads and
        # make only auth fail by pointing at the failing stub via a
        # wrapper is overkill — instead pass --self-id-free flow with
        # the failing stub replaced by an auth-failing, read-working
        # stub written inline.
        stub = os.path.join(d, "slackcli2")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(FAKE_SLACKCLI.replace("__SELF_ID__", SELF)
                    .replace("__BOT_ID__", make_id("B", "TESTBOT001")))
        os.chmod(stub, os.stat(stub).st_mode | stat.S_IXUSR)
        # Make auth fail in that stub: wrap it so "auth" exits 1.
        wrapper = os.path.join(d, "slackcli")
        with open(wrapper, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\nif [ "$1" = auth ]; then exit 1; fi\n'
                    'exec "' + stub + '" "$@"\n')
        os.chmod(wrapper, os.stat(wrapper).st_mode | stat.S_IXUSR)
        ids_path = os.path.join(d, "ids.json")
        with open(ids_path, "w", encoding="utf-8") as f:
            json.dump(IDS_FIXTURE, f)
        os.chmod(ids_path, 0o600)
        env["NEIRO_SLACK_IDS_PATH"] = ids_path
        r = subprocess.run(
            [sys.executable, CHECK_PY, "--channel", CHANNEL,
             "--event-ts", EVENT_TS, "--questioner", QUESTIONER,
             "--workspace", "muse-board", "--slackcli", wrapper],
            capture_output=True, text=True, env=env, timeout=30)
        out = json.loads(r.stdout)
        check("CLI: whoami fails + ids.json ok -> answered_by_other",
              r.returncode == 0 and out["verdict"] == "answered_by_other",
              (r.returncode, r.stdout, r.stderr))


def test_cli_invalid_self_id_fails_closed():
    # A typo'd --self-id must not be trusted for the verdict.
    with tempfile.TemporaryDirectory() as d:
        fake = _write_fake_cli(d, FAKE_SLACKCLI.replace(
            "__SELF_ID__", SELF).replace(
            "__BOT_ID__", make_id("B", "TESTBOT001")))
        env = dict(os.environ)
        r = subprocess.run(
            [sys.executable, CHECK_PY, "--channel", CHANNEL,
             "--event-ts", EVENT_TS, "--questioner", QUESTIONER,
             "--workspace", "muse-board", "--self-id", "not-an-id",
             "--slackcli", fake],
            capture_output=True, text=True, env=env, timeout=30)
        out = json.loads(r.stdout)
        check("CLI: invalid --self-id -> exit 2 self_id_unknown",
              r.returncode == 2 and out["verdict"] == "cannot_verify"
              and out.get("reason") == "self_id_unknown",
              (r.returncode, r.stdout, r.stderr))


def test_cli_self_id_unknown_fails_closed():
    # No --self-id, whoami fails, ids.json missing -> cannot_verify,
    # exit 2, reason self_id_unknown (never judge with a wrong self).
    with tempfile.TemporaryDirectory() as d:
        fake = _write_fake_cli(d, FAKE_FAILING_SLACKCLI)
        env = dict(os.environ)
        env["NEIRO_SLACK_IDS_PATH"] = os.path.join(d, "nope.json")
        r = subprocess.run(
            [sys.executable, CHECK_PY, "--channel", CHANNEL,
             "--event-ts", EVENT_TS, "--questioner", QUESTIONER,
             "--workspace", "muse-board", "--slackcli", fake],
            capture_output=True, text=True, env=env, timeout=30)
        out = json.loads(r.stdout)
        check("CLI: self id unknown -> exit 2 cannot_verify",
              r.returncode == 2 and out["verdict"] == "cannot_verify"
              and out.get("reason") == "self_id_unknown",
              (r.returncode, r.stdout, r.stderr))


def main():
    global FAIL
    tests = [test_ok_to_resend,
             test_answered_by_other,
             test_time_ordering,
             test_empty_text_does_not_count,
             test_bot_and_short_posts_do_not_count,
             test_parent_deleted,
             test_cannot_verify,
             test_window_single_read_from_event_minus_60,
             test_answer_beyond_window_is_fail_closed,
             test_answer_within_window_counts_even_with_more,
             test_cli_end_to_end,
             test_self_id_fallback_from_ids,
             test_self_id_missing_ids_fails_closed,
             test_cli_self_id_unknown_fails_closed,
             test_whoami_garbage_output_falls_back,
             test_whoami_grid_user_id_is_used,
             test_load_ids_rejects_bad_inputs,
             test_cli_fallback_completes_verdict,
             test_cli_invalid_self_id_fails_closed]
    for t in tests:
        try:
            t()
        except Exception as e:
            FAIL += 1
            print(f"[FAIL] {t.__name__} raised {e!r}")
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
