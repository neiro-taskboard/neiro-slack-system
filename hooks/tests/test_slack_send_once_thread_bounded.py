#!/usr/bin/env python3
"""Tests for the bounded thread duplicate checks in slack-send-once.sh.

Style follows the neighbours (plain Python, no pytest):
  python3 ~/hooks/tests/test_slack_send_once_thread_bounded.py

Covered (2026-11-14, thread checks bounded to the newest 100):
  Until this change, both thread duplicate checks (own text in
  check_duplicate, other users' text in check_other_duplicate) paged
  through the WHOLE thread from --oldest=<parent ts>, so the re-read
  cost of every send grew with the thread length (the audit thread in
  muse-toberiyaku had ~180 replies). At the owner's direction the checks
  now read the newest THREAD_CHECK_N=100 messages in a single call
  (no --oldest: slackcli returns parent + newest N). Trade-off, taken
  knowingly: an identical post older than the newest 100 is no longer
  caught by the history check. Own sends are still covered by the
  local send ledger, and exact-match parroting accidents happen
  between near-simultaneous posts, i.e. inside the newest window.

The stub slackcli emulates the real read semantics measured on a
live thread (2026-11-14):
  - thread read without --oldest -> parent + newest --limit messages
  - thread read with --oldest=X  -> messages newer than X, oldest
    first, paged by --limit with has_more / next_oldest
  - channel read without --oldest -> newest --limit messages

Cases (thread with 150 replies unless noted):
  1. own identical post inside newest 100 -> duplicate (check-only 0)
  2. own identical post older than newest 100 -> NOT a duplicate
     (check-only 1) -- documents the cap trade-off
  3. other user's identical post inside newest 100 -> exit 4, no send
  4. other user's identical post older than newest 100 -> sends (0)
  5. small thread (10 replies), own identical post -> duplicate (0)
  6. no identical post -> check-only 1, and the thread history is
     read exactly once (no paging), never with --oldest
"""
import json
import os
import subprocess
import sys
import tempfile

SCRIPT = "/home/hatch/hooks/scripts/slack-send-once.sh"

PASS = 0
FAIL = 0

SELF_ID = "U0CH1B19D4J"
OTHER_ID = "U0CH1QBC8M8"
THREAD_TS = "1794600000.000001"
CANDIDATE = "重複チェック上限のテスト文面です。"


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
    print("neiro (U0CH1B19D4J)")
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
    thread = opt("--thread-ts")
    if oldest is not None:
        newer = [m for m in msgs if float(m["ts"]) > float(oldest)]
        page = newer[:limit]
        out = {"messages": page, "has_more": len(newer) > limit}
        if page:
            out["next_oldest"] = page[-1]["ts"]
        print(json.dumps(out, ensure_ascii=False))
        sys.exit(0)
    if thread:
        if len(msgs) > limit + 1:
            out_msgs = msgs[:1] + msgs[-limit:]
            print(json.dumps({"messages": out_msgs, "has_more": True,
                              "next_oldest": out_msgs[-1]["ts"]},
                             ensure_ascii=False))
        else:
            print(json.dumps({"messages": msgs, "has_more": False},
                             ensure_ascii=False))
        sys.exit(0)
    print(json.dumps({"messages": msgs[-limit:], "has_more": False},
                     ensure_ascii=False))
    sys.exit(0)

sys.exit(0)
"""


def make_thread(n_replies, special=None):
    """Parent + n_replies messages, oldest first.

    special: {reply_index (1-based): (user, text)} overrides.
    """
    special = special or {}
    msgs = [{"ts": THREAD_TS, "user": OTHER_ID, "text": "親の投稿です。"}]
    for i in range(1, n_replies + 1):
        user, text = special.get(i, (OTHER_ID, f"filler {i}"))
        msgs.append({"ts": f"{1794600000 + i}.000001",
                     "user": user, "text": text})
    return {"messages": msgs, "has_more": False}


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
        stub = os.path.join(bindir, "slackcli")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(STUB)
        os.chmod(stub, 0o755)
        self.env = dict(os.environ, HOME=self.home,
                        SLACKCLI_LOG=self.log,
                        SLACKCLI_HISTORY=self.history_path)

    def run(self, history, check_only=True):
        with open(self.history_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
        if os.path.exists(self.log):
            os.remove(self.log)
        argv = ["bash", SCRIPT, "--channel", "C0CGYGT9V7G",
                "--thread-ts", THREAD_TS, "--message", CANDIDATE]
        if check_only:
            argv.append("--check-only")
        rc = subprocess.run(argv, env=self.env, capture_output=True,
                            text=True, timeout=120).returncode
        calls = []
        if os.path.exists(self.log):
            with open(self.log, encoding="utf-8") as f:
                calls = [ln.strip() for ln in f if ln.strip()]
        reads = [c for c in calls if c.startswith("conversations")]
        sends = [c for c in calls if c.startswith("messages")]
        return rc, reads, sends

    def close(self):
        self.d.cleanup()


def main():
    env = Env()
    try:
        # 1. own identical post inside newest 100 (reply 120 of 150)
        hist = make_thread(150, {120: (SELF_ID, CANDIDATE)})
        rc, reads, _ = env.run(hist)
        check("own dup inside newest 100: duplicate", rc == 0, f"rc={rc}")
        check("own dup inside newest 100: single read, no --oldest",
              len(reads) == 1 and "--oldest" not in reads[0],
              f"reads={reads}")

        # 2. own identical post older than newest 100 (reply 10 of 150)
        hist = make_thread(150, {10: (SELF_ID, CANDIDATE)})
        rc, _, _ = env.run(hist)
        check("own dup older than newest 100: not a duplicate (cap)",
              rc == 1, f"rc={rc}")

        # 3. other user's identical post inside newest 100 -> exit 4
        hist = make_thread(150, {145: (OTHER_ID, CANDIDATE)})
        rc, reads, sends = env.run(hist, check_only=False)
        check("other dup inside newest 100: exit 4", rc == 4, f"rc={rc}")
        check("other dup inside newest 100: nothing sent",
              len(sends) == 0, f"sends={sends}")
        check("other dup: reads bounded, no --oldest",
              len(reads) == 2
              and all("--oldest" not in r for r in reads),
              f"reads={reads}")

        # 4. other user's identical post older than newest 100 -> sends
        hist = make_thread(150, {10: (OTHER_ID, CANDIDATE)})
        rc, _, sends = env.run(hist, check_only=False)
        check("other dup older than newest 100: sends", rc == 0,
              f"rc={rc}")
        check("other dup older than newest 100: send happened",
              len(sends) == 1, f"sends={sends}")

        # 5. small thread: own identical post still found
        hist = make_thread(10, {3: (SELF_ID, CANDIDATE)})
        rc, _, _ = env.run(hist)
        check("small thread own dup: duplicate", rc == 0, f"rc={rc}")

        # 6. no identical post: no duplicate, exactly one read
        hist = make_thread(150)
        rc, reads, _ = env.run(hist)
        check("no dup: check-only 1", rc == 1, f"rc={rc}")
        check("no dup: single read", len(reads) == 1, f"reads={reads}")
    finally:
        env.close()

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
