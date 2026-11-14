#!/usr/bin/env python3
"""Tests for --permalink parsing in slack-send-once.sh.

Style follows the neighbours (plain Python, no pytest):
  python3 ~/hooks/tests/test_slack_send_once_permalink.py

Each test runs the real slack-send-once.sh as a subprocess with a
temporary HOME holding a stub slackcli, so no real Slack call is made
and the live ledger/state are never touched. The stub logs every
invocation; the --thread-ts it receives on `conversations read` is
the parsed thread parent, which is what we assert.

Covered (2026-11-14, --permalink bug fix: the p<ts> part is the
message's own ts, so a reply permalink must take its parent from
?thread_ts=, not from p<ts>):
  1. parent-post permalink (no thread_ts) -> parent is p<ts>
  2. thread-reply permalink (?thread_ts=...&cid=...) -> parent is thread_ts
  3. reply link without thread_ts -> falls back to p<ts>
  4. thread_ts after cid (?cid=...&thread_ts=...) -> parent is thread_ts
  5. URL-encoded dot in thread_ts (thread_ts=1794583788%2E831849)
     -> parent is the decoded ts
"""
import json
import os
import subprocess
import sys
import tempfile

SCRIPT = "/home/hatch/hooks/scripts/slack-send-once.sh"

PASS = 0
FAIL = 0


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
  conversations) printf '%s' '{"messages": [], "has_more": false}' ;;
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
        stub = os.path.join(bindir, "slackcli")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(STUB)
        os.chmod(stub, 0o755)
        self.env = dict(os.environ, HOME=self.home,
                        SLACKCLI_LOG=self.log)

    def run(self, permalink):
        """Run --check-only; returns (exit_code, parsed thread_ts, channel)."""
        if os.path.exists(self.log):
            os.remove(self.log)
        msg = "permalink-test-" + permalink[-12:]
        rc = subprocess.run(
            ["bash", SCRIPT, "--permalink", permalink, "--message", msg,
             "--check-only"],
            env=self.env, capture_output=True, text=True, timeout=60,
        ).returncode
        thread_ts = None
        channel = None
        if os.path.exists(self.log):
            for line in open(self.log, encoding="utf-8"):
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "conversations" \
                        and parts[1] == "read":
                    channel = parts[2]
                    if "--thread-ts" in parts:
                        thread_ts = parts[parts.index("--thread-ts") + 1]
                    break
        return rc, thread_ts, channel

    def close(self):
        self.d.cleanup()


def main():
    env = Env()
    try:
        parent = "1794583788.831849"
        reply = "1794614042.199529"
        base = "https://muse-ulq4476.slack.com/archives/C0CGYGT9V7G"

        # 1. 親投稿のリンク（thread_ts なし）-> 親は p<ts>
        rc, ts, ch = env.run(f"{base}/p1794583788831849")
        check("parent post: thread_ts is p<ts>",
              rc == 1 and ts == parent and ch == "C0CGYGT9V7G",
              f"rc={rc} ts={ts} ch={ch}")

        # 2. スレッド内返信のリンク -> 親は ?thread_ts=（p<ts> ではない）
        rc, ts, ch = env.run(
            f"{base}/p1794614042199529?thread_ts=1794583788.831849"
            "&cid=C0CGYGT9V7G")
        check("thread reply: thread_ts is the ?thread_ts= parent",
              rc == 1 and ts == parent and ch == "C0CGYGT9V7G",
              f"rc={rc} ts={ts} ch={ch}")

        # 3. thread_ts なしの返信リンク -> p<ts> にフォールバック
        rc, ts, ch = env.run(f"{base}/p1794614042199529")
        check("reply link without thread_ts: falls back to p<ts>",
              rc == 1 and ts == reply and ch == "C0CGYGT9V7G",
              f"rc={rc} ts={ts} ch={ch}")

        # 4. thread_ts が cid の後ろ -> 親は thread_ts
        rc, ts, ch = env.run(
            f"{base}/p1794614042199529?cid=C0CGYGT9V7G"
            "&thread_ts=1794583788.831849")
        check("thread_ts after cid: parent is thread_ts",
              rc == 1 and ts == parent and ch == "C0CGYGT9V7G",
              f"rc={rc} ts={ts} ch={ch}")

        # 5. URL エンコードされたドット -> デコードして親にする
        rc, ts, ch = env.run(
            f"{base}/p1794614042199529?thread_ts=1794583788%2E831849"
            "&cid=C0CGYGT9V7G")
        check("encoded dot in thread_ts: decoded parent",
              rc == 1 and ts == parent and ch == "C0CGYGT9V7G",
              f"rc={rc} ts={ts} ch={ch}")
    finally:
        env.close()

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
