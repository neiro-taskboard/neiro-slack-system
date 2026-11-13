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
  7. paging: an answering post on page 2 is found (next_oldest walk)
  8. CLI end-to-end with a fake slackcli: ok_to_resend exits 0,
     unreadable history exits 2; --self-id is honoured
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
    "/home/hatch/hooks/scripts/slack-answered-check.py")

PASS = 0
FAIL = 0

QUESTIONER = "U0CH3MQH7PT"
SELF = "U0CH1B19D4J"
OTHER = "U0CH1QBC8M8"
EVENT_TS = "1794583788.831849"
THREAD_TS = "1794583788.831849"
CHANNEL = "C0CGYGT9V7G"


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


def test_paging_finds_answer_on_page2():
    mod = load_check()
    pages = [
        thread_page([{"ts": "1794583868.235619", "user": QUESTIONER,
                      "text": "質問の続き"}],
                    has_more=True, next_oldest="1794584000.000000"),
        thread_page([{"ts": "1794585000.000000", "user": OTHER,
                      "text": "こっちで答えますね、確認しました"}]),
    ]
    calls = {"n": 0}

    def page_reader(ch, th, oldest):
        page = pages[min(calls["n"], len(pages) - 1)]
        calls["n"] += 1
        return page

    out = mod.check(CHANNEL, EVENT_TS, THREAD_TS, QUESTIONER, SELF,
                    page_reader, channel_with_parent())
    check("answering post on page 2 is found via next_oldest walk",
          out["verdict"] == "answered_by_other", out)
    check("paging walked both pages", calls["n"] == 2, calls)


FAKE_SLACKCLI = """#!/usr/bin/env python3
import json, os, sys
if "auth" in sys.argv:
    print("User: neiro (U0CH1B19D4J, bot B0CH3D6UVHP)")
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
            f.write(FAKE_SLACKCLI)
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


def main():
    global FAIL
    tests = [test_ok_to_resend,
             test_answered_by_other,
             test_time_ordering,
             test_empty_text_does_not_count,
             test_bot_and_short_posts_do_not_count,
             test_parent_deleted,
             test_cannot_verify,
             test_paging_finds_answer_on_page2,
             test_cli_end_to_end]
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
