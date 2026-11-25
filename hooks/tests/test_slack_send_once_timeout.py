#!/usr/bin/env python3
"""Tests for the slackcli deadlines in slack-send-once.sh.

Style follows the neighbours (plain Python, no pytest):
  python3 ~/hooks/tests/test_slack_send_once_timeout.py

Each test runs the real slack-send-once.sh as a subprocess with a
temporary HOME holding a stub slackcli, so no real Slack call is made
and the live ledger/state are never touched. The stub can sleep before
answering (via /bin/sleep, so a no-op `sleep` placed earlier in PATH
for the script's own backoff sleeps does not shorten the stub's delay).

Covered (2026-11-25, Yama方式の取り入れ：通信に締切を付ける):
  The metadata path (slack-read-metadata.py / slack-post-metadata.py)
  already had curl --max-time; the direct slackcli calls in
  slack-send-once.sh had no deadline, so a hung slackcli could stall
  a send forever. Reads now go through slackcli_read (timeout,
  SLACK_SEND_ONCE_READ_TIMEOUT_SECS, default 60) and the send through
  timeout (SLACK_SEND_ONCE_SEND_TIMEOUT_SECS, default 120).
  1. a slackcli read that hangs past the read deadline is cut off:
     --check-only reports cannot-verify (exit 2) in bounded time
     instead of waiting for every retry's full hang.
  2. a send that hangs past the send deadline is cut off (exit 124
     inside), and the existing lost-response verification runs and
     concludes the post is absent (exit 1), in bounded time.
  3. control: with no hang, the wrapped calls behave as before
     (a normal send succeeds, exit 0).
"""
import json
import os
import subprocess
import sys
import tempfile
import time

def make_id(prefix, rest):
    """Build an ID-shaped string from parts (like a real Slack ID).

    Fixtures are synthetic (2026-11-21): the real IDs used before were
    other people's and ours; parts-built fakes keep the tests passing
    without leaking real IDs into the public repo.
    """
    return prefix + "0" + rest


CHANNEL = make_id("C", "TESTCHAN01")
SELF_ID = make_id("U", "TESTBOTMUSE")

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
# /bin/sleep is absolute on purpose: the test puts a no-op `sleep`
# earlier in PATH to skip the script's own backoff sleeps, and the
# stub's simulated hang must not be shortened by it.
echo "$@" >> "$SLACKCLI_LOG"
case "$1" in
  auth) echo "neiro (__SELF_ID__)" ;;
  conversations) /bin/sleep "${STUB_READ_SLEEP:-0}"; cat "$SLACKCLI_HISTORY" ;;
  messages) /bin/sleep "${STUB_SEND_SLEEP:-0}"; echo '{"ts":"1795000000.000001"}' ;;
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
            f.write(STUB.replace("__SELF_ID__", SELF_ID))
        os.chmod(stub, 0o755)
        # No-op `sleep` first in PATH: the script's verification
        # backoff sleeps (5s/10s/15s) must not slow the test down.
        noopdir = os.path.join(self.home, "noopbin")
        os.makedirs(noopdir, exist_ok=True)
        noop = os.path.join(noopdir, "sleep")
        with open(noop, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(noop, 0o755)
        self.env = dict(os.environ, HOME=self.home,
                        PATH=noopdir + ":" + os.environ["PATH"],
                        SLACKCLI_LOG=self.log,
                        SLACKCLI_HISTORY=self.history_path)
        with open(self.history_path, "w", encoding="utf-8") as f:
            json.dump({"messages": [], "has_more": False}, f)

    def run(self, args, extra_env, timeout=90):
        env = dict(self.env, **extra_env)
        start = time.monotonic()
        proc = subprocess.run(["bash", SCRIPT] + args, env=env,
                              capture_output=True, text=True,
                              timeout=timeout)
        return proc.returncode, time.monotonic() - start

    def read_log(self):
        if not os.path.exists(self.log):
            return ""
        with open(self.log, encoding="utf-8") as f:
            return f.read()

    def close(self):
        self.d.cleanup()


def main():
    env = Env()
    try:
        # 1. Hanging read, 1s deadline: --check-only must give up as
        #    cannot-verify (exit 2) quickly. Without the deadline each
        #    of the history retries would hang for the stub's 30s.
        rc, elapsed = env.run(
            ["--channel", CHANNEL, "--message", "テスト本文です",
             "--check-only"],
            {"STUB_READ_SLEEP": "30",
             "SLACK_SEND_ONCE_READ_TIMEOUT_SECS": "1"})
        check("hanging read -> check-only exit 2 (cannot-verify)",
              rc == 2, f"rc={rc}")
        check("hanging read -> bounded time (<60s)",
              elapsed < 60, f"elapsed={elapsed:.1f}s")

        # 2. Hanging send, 1s deadline: the send is cut off and the
        #    lost-response verification finds nothing, so the script
        #    reports a plain send failure (exit 1), quickly.
        rc, elapsed = env.run(
            ["--channel", CHANNEL, "--message", "テスト本文です"],
            {"STUB_SEND_SLEEP": "30",
             "SLACK_SEND_ONCE_SEND_TIMEOUT_SECS": "1"})
        check("hanging send -> exit 1 (send failed, verified absent)",
              rc == 1, f"rc={rc}")
        check("hanging send -> bounded time (<60s)",
              elapsed < 60, f"elapsed={elapsed:.1f}s")
        check("hanging send -> the send was actually attempted",
              "messages send" in env.read_log(), env.read_log())

        # 3. Control: no hang, generous deadlines -> normal send.
        rc, elapsed = env.run(
            ["--channel", CHANNEL, "--message", "テスト本文です"],
            {"SLACK_SEND_ONCE_READ_TIMEOUT_SECS": "60",
             "SLACK_SEND_ONCE_SEND_TIMEOUT_SECS": "120"})
        check("control send -> exit 0", rc == 0, f"rc={rc}")
    finally:
        env.close()

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
