#!/usr/bin/env python3
"""Tests for the fail-closed duplicate checks in slack-send-once.sh (中-1).

Yama's review (2026-11-21, muse取締役会 #slack-へようこそ) found that the
seven python3 -c blocks in slack-send-once.sh return 0=found / 1=not found /
2=unreadable, but an uncaught Python exception also exits 1, so any
unexpected exception (e.g. UnicodeDecodeError on a non-UTF-8 message file)
was read as "no duplicate" and the send went ahead (fail-open). The fix
wraps each block in try: ... except Exception: sys.exit(2), so only an
explicit sys.exit(1) means "not found", and passes CLI-derived values
(--event-id / --request-id, --window-mins cutoff) via environment instead
of interpolating them into the Python source (try/except cannot catch a
compile-time SyntaxError from a "'" in the value).

Style follows the neighbours (plain Python, no pytest):
  python3 ~/hooks/tests/test_slack_send_once_fail_closed.py

Cases (stub slackcli, channel posts unless noted):
  1. message file is not valid UTF-8 -> check-only 2 (cannot-verify),
     no send attempted.
  2. history contains a message whose text is not a string (dict) ->
     check-only 2, no send attempted.
  3. normal duplicate still found (check-only 0) and normal absence
     still 1 -- the try-wrap did not change the happy paths.
  4. static: no CLI-derived value is interpolated into a python3 -c
     source string any more ('$EVENT_ID' / '$REQUEST_ID' / 'cutoff =
     $cutoff' must be gone; os.environ reads must be present).
  5. marked-block standalone: with EVENT_ID containing a quote, the
     check runs instead of dying with SyntaxError -- a matching marked
     message is found (exit 0), without a match it reports not-found
     (exit 1).
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

SCRIPT = "/home/hatch/hooks/scripts/slack-send-once.sh"


def make_id(prefix, rest):
    """Build an ID-shaped string from parts (like a real Slack ID)."""
    return prefix + "0" + rest


CHANNEL = make_id("C", "TESTCHAN01")
SELF_ID = make_id("U", "TESTBOTMUSE")
OTHER_ID = make_id("U", "TESTOTHER1")
CANDIDATE = "fail-closed のテスト文面です。"

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


STUB = r"""#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
with open(os.environ["SLACKCLI_LOG"], "a", encoding="utf-8") as f:
    f.write(" ".join(args) + "\n")

if args and args[0] == "auth":
    print("neiro (__SELF_ID__)")
    sys.exit(0)

if args and args[0] == "messages":
    print(json.dumps({"ts": "1794999999.000001"}))
    sys.exit(0)

if args and args[0] == "conversations":
    with open(os.environ["SLACKCLI_HISTORY"], encoding="utf-8") as f:
        msgs = json.load(f)["messages"]
    def opt(name, default=None):
        if name in args:
            return args[args.index(name) + 1]
        return default
    limit = int(opt("--limit", "100"))
    oldest = opt("--oldest")
    if oldest is not None:
        newer = [m for m in msgs if float(m["ts"]) > float(oldest)]
        page = newer[:limit]
        out = {"messages": page, "has_more": len(newer) > limit}
        if page:
            out["next_oldest"] = page[-1]["ts"]
        print(json.dumps(out, ensure_ascii=False))
        sys.exit(0)
    print(json.dumps({"messages": msgs[-limit:], "has_more": False},
                     ensure_ascii=False))
    sys.exit(0)

sys.exit(0)
"""


class Env:
    """Temp HOME with an emulating stub slackcli (script uses $HOME)."""

    def __init__(self):
        self.d = tempfile.TemporaryDirectory()
        self.home = self.d.name
        bindir = os.path.join(self.home, "workspace", "bin")
        os.makedirs(bindir, exist_ok=True)
        os.makedirs(os.path.join(self.home, "hooks", "state"),
                    exist_ok=True)
        self.log = os.path.join(self.home, "slackcli.log")
        self.history_path = os.path.join(self.home, "history.json")
        self.ledger_path = os.path.join(
            self.home, "hooks", "state", "slack-send-log.jsonl")
        stub = os.path.join(bindir, "slackcli")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(STUB.replace("__SELF_ID__", SELF_ID))
        os.chmod(stub, 0o755)
        self.env = dict(os.environ, HOME=self.home,
                        SLACKCLI_LOG=self.log,
                        SLACKCLI_HISTORY=self.history_path)

    def run_check_only(self, history, msg_bytes=None, msg_text=None):
        with open(self.history_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
        if os.path.exists(self.ledger_path):
            os.remove(self.ledger_path)
        if os.path.exists(self.log):
            os.remove(self.log)
        argv = ["bash", SCRIPT, "--channel", CHANNEL]
        if msg_bytes is not None:
            msg_file = os.path.join(self.home, "msg.bin")
            with open(msg_file, "wb") as f:
                f.write(msg_bytes)
            argv += ["--message-file", msg_file]
        else:
            argv += ["--message", msg_text]
        argv.append("--check-only")
        proc = subprocess.run(argv, env=self.env, capture_output=True,
                              timeout=180)
        calls = []
        if os.path.exists(self.log):
            with open(self.log, encoding="utf-8") as f:
                calls = [ln.strip() for ln in f if ln.strip()]
        sends = [c for c in calls if c.startswith("messages")]
        return proc.returncode, sends

    def close(self):
        self.d.cleanup()


def now_ts(offset_secs=0):
    return f"{time.time() + offset_secs:.6f}"


def test_non_utf8_message_file():
    """Case 1: non-UTF-8 message file -> check-only 2, nothing sent."""
    env = Env()
    try:
        history = {"messages": [
            {"ts": now_ts(-60), "user": OTHER_ID, "text": "filler"},
        ]}
        rc, sends = env.run_check_only(history, msg_bytes=b"\xff\xfe bad")
        check("non-UTF-8 message file: check-only 2", rc == 2, f"rc={rc}")
        check("non-UTF-8 message file: no send attempted", sends == [],
              f"sends={sends}")
    finally:
        env.close()


def test_non_string_text_in_history():
    """Case 2: history message with a dict text -> check-only 2."""
    env = Env()
    try:
        history = {"messages": [
            {"ts": now_ts(-60), "user": SELF_ID,
             "text": {"type": "weird", "value": CANDIDATE}},
            {"ts": now_ts(-30), "user": OTHER_ID, "text": "filler"},
        ]}
        rc, sends = env.run_check_only(history, msg_text=CANDIDATE)
        check("non-string text in history: check-only 2", rc == 2,
              f"rc={rc}")
        check("non-string text in history: no send attempted", sends == [],
              f"sends={sends}")
    finally:
        env.close()


def test_happy_paths_unchanged():
    """Case 3: duplicate still found (0), absence still 1."""
    env = Env()
    try:
        history = {"messages": [
            {"ts": now_ts(-60), "user": SELF_ID, "text": CANDIDATE},
        ]}
        rc, _ = env.run_check_only(history, msg_text=CANDIDATE)
        check("duplicate still found (check-only 0)", rc == 0, f"rc={rc}")
        history = {"messages": [
            {"ts": now_ts(-60), "user": OTHER_ID, "text": "filler"},
        ]}
        rc, _ = env.run_check_only(history, msg_text=CANDIDATE)
        check("absence still not-found (check-only 1)", rc == 1, f"rc={rc}")
    finally:
        env.close()


def c_blocks():
    """All python3 -c blocks, found line by line (regex spans are fragile)."""
    with open(SCRIPT, encoding="utf-8") as f:
        lines = f.read().splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        if 'python3 -c "' in lines[i]:
            j = next(k for k in range(i + 1, len(lines))
                     if lines[k].strip().startswith('"'))
            blocks.append("\n".join(lines[i + 1:j]))
            i = j + 1
        else:
            i += 1
    return blocks


def test_no_source_interpolation():
    """Case 4: CLI-derived values are not interpolated into -c source."""
    blocks = c_blocks()
    check("no '$EVENT_ID' in python3 -c source",
          not any("'$EVENT_ID'" in b for b in blocks))
    check("no '$REQUEST_ID' in python3 -c source",
          not any("'$REQUEST_ID'" in b for b in blocks))
    check("no 'cutoff = $cutoff' in python3 -c source",
          not any("cutoff = $cutoff" in b for b in blocks))
    with open(SCRIPT, encoding="utf-8") as f:
        src = f.read()
    check("EVENT_ID read from environment",
          "os.environ.get('EVENT_ID')" in src)
    check("REQUEST_ID read from environment",
          "os.environ.get('REQUEST_ID')" in src)
    check("CUTOFF read from environment",
          "os.environ['CUTOFF']" in src)
    check("seven check blocks wrapped in try/except -> exit 2",
          sum("except Exception:\n    sys.exit(2)" in b
              for b in blocks) == 7,
          f"{len(blocks)} -c blocks total")


def test_marked_block_with_quote():
    """Case 5: marked block runs with a quote in EVENT_ID (standalone)."""
    with open(SCRIPT, encoding="utf-8") as f:
        lines = f.read().splitlines()
    marker = 'EVENT_ID="$EVENT_ID" REQUEST_ID="$REQUEST_ID" python3 -c "'
    si = next(i for i, l in enumerate(lines) if marker in l)
    ei = next(i for i in range(si + 1, len(lines))
              if lines[i].strip().startswith('"'))
    code = "\n".join(lines[si + 1:ei]) + "\n"
    code = code.replace("$TMP_MSG", "__TMP_MSG__").replace("$SELF_ID",
                                                           SELF_ID)
    with tempfile.TemporaryDirectory() as tmp:
        msg_path = os.path.join(tmp, "msg.txt")
        with open(msg_path, "w", encoding="utf-8") as f:
            f.write(CANDIDATE)
        code = code.replace("__TMP_MSG__", msg_path)
        py_path = os.path.join(tmp, "marked.py")
        with open(py_path, "w", encoding="utf-8") as f:
            f.write(code)
        dump_match = {"ok": True, "messages": [
            {"user": SELF_ID, "text": CANDIDATE, "ts": now_ts(-10),
             "mark": {"kind": "event_id", "value": "ev'ent"}}]}
        dump_empty = {"ok": True, "messages": []}
        env = dict(os.environ, EVENT_ID="ev'ent", REQUEST_ID="req1")
        for name, dump, want in [
                ("quote event-id + matching mark: found (0)",
                 dump_match, 0),
                ("quote event-id + no match: not-found (1)",
                 dump_empty, 1)]:
            proc = subprocess.run(
                [sys.executable, py_path], env=env, input=json.dumps(dump),
                capture_output=True, text=True, timeout=30)
            check(name, proc.returncode == want,
                  f"rc={proc.returncode} err={proc.stderr.strip()[:200]}")


def main():
    test_non_utf8_message_file()
    test_non_string_text_in_history()
    test_happy_paths_unchanged()
    test_no_source_interpolation()
    test_marked_block_with_quote()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
