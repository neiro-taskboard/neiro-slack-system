#!/usr/bin/env python3
"""Tests for resend-header normalisation in slack-send-once.sh.

Style follows the neighbours (plain Python, no pytest):
  python3 ~/hooks/tests/test_slack_send_once_resend.py

Each test runs the real slack-send-once.sh as a subprocess with a
temporary HOME holding a stub slackcli, so no real Slack call is made
and the live ledger/state are never touched. The stub answers
`conversations read` with a canned history.

Covered (2026-11-14, resend-header wording unification):
  The resend header changed from "遅れての再送（最初の試行：…）" to
  "遅れての再送（元の失敗時刻：…）" (3-party agreement, Yama's share
  2026-11-14). The self-duplicate checks (window / bounded / ledger)
  must treat both wordings as the same post, so a re-woken resend
  does not double-send just because the header wording changed
  (same idea as Yama's and Yanayana's both-wording recognition).
  1. history has OLD-wording resend, candidate is NEW-wording, same
     body -> duplicate (window check)
  2. history has NEW-wording resend, candidate is OLD-wording, same
     body -> duplicate (window check)
  3. history has OLD-wording resend, candidate is NEW-wording with a
     different body -> no duplicate
  4. plain message, identical text, no header -> duplicate (sanity:
     normalisation did not break plain dedup)
  5. history has OLD-wording resend, candidate identical OLD-wording
     -> duplicate (same-wording still matches)
  6. history has OLD-wording resend older than the 15-min window,
     candidate is NEW-wording, same body -> duplicate (bounded check)
"""
import json
import os
import subprocess
import sys
import tempfile
import time

SCRIPT = "/home/hatch/hooks/scripts/slack-send-once.sh"

PASS = 0
FAIL = 0

BODY = "これはテストの本文です。\n2行目もあります。"
OLD_HEAD = "遅れての再送（最初の試行：2026-11-14 09:12 JST）"
NEW_HEAD = "遅れての再送（元の失敗時刻：2026-11-14 09:12 JST）"
SELF_ID = "U0CH1B19D4J"


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


STUB = """#!/bin/sh
# Log argv (one line per call), then answer like slackcli.
echo "$@" >> "$SLACKCLI_LOG"
case "$1" in
  auth) echo "neiro (U0CH1B19D4J)" ;;
  conversations) cat "$SLACKCLI_HISTORY" ;;
esac
exit 0
"""


class Env:
    """Temp HOME with a stub slackcli; the script finds it via $HOME."""

    def __init__(self):
        self.d = tempfile.TemporaryDirectory()
        self.home = self.d.name
        bindir = os.path.join(self.home, "workspace", "bin")
        os.makedirs(bindir, exist_ok=True)
        self.log = os.path.join(self.home, "slackcli.log")
        self.history_path = os.path.join(self.home, "history.json")
        stub = os.path.join(bindir, "slackcli")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(STUB)
        os.chmod(stub, 0o755)
        self.env = dict(os.environ, HOME=self.home,
                        SLACKCLI_LOG=self.log,
                        SLACKCLI_HISTORY=self.history_path)

    def run(self, message, history_text, history_ts=None):
        """Run --check-only; returns exit code (0 = duplicate, 1 = none)."""
        if history_ts is None:
            history_ts = time.time()
        history = {"messages": [{"ts": str(history_ts), "user": SELF_ID,
                                 "text": history_text}],
                   "has_more": False}
        with open(self.history_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
        rc = subprocess.run(
            ["bash", SCRIPT, "--channel", "C0CGYGT9V7G",
             "--message", message, "--check-only"],
            env=self.env, capture_output=True, text=True, timeout=120,
        ).returncode
        return rc

    def close(self):
        self.d.cleanup()


def main():
    env = Env()
    try:
        old_msg = OLD_HEAD + "\n" + BODY
        new_msg = NEW_HEAD + "\n" + BODY

        # 1. 旧文言の再送が履歴にあり、新文言の同じ本文 -> 重複
        rc = env.run(new_msg, old_msg)
        check("old-wording history vs new-wording candidate: duplicate",
              rc == 0, f"rc={rc}")

        # 2. 逆方向（新文言の履歴・旧文言の候補）も重複
        rc = env.run(old_msg, new_msg)
        check("new-wording history vs old-wording candidate: duplicate",
              rc == 0, f"rc={rc}")

        # 3. 本文が違えば重複ではない
        rc = env.run(NEW_HEAD + "\n別の本文です。", old_msg)
        check("different body: no duplicate", rc == 1, f"rc={rc}")

        # 4. ヘッダーなしの同一文面は従来通り重複
        rc = env.run(BODY, BODY)
        check("plain identical text: duplicate", rc == 0, f"rc={rc}")

        # 5. 旧文言同士も重複
        rc = env.run(old_msg, old_msg)
        check("old-wording identical: duplicate", rc == 0, f"rc={rc}")

        # 6. 窓（15分）を過ぎた旧文言の再送は bounded チェックで重複
        rc = env.run(new_msg, old_msg, history_ts=time.time() - 20 * 60)
        check("old-wording history past window vs new-wording: duplicate (bounded)",
              rc == 0, f"rc={rc}")
    finally:
        env.close()

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
