#!/usr/bin/env python3
"""Tests for the ledger-based parroting check in slack-send-once.sh.

Yama's proposal (2026-11-14, adopted by Yanayana and Yama, then Neiro):
split the roles -- duplicate-send prevention reads the recent Slack
window, parroting prevention reads the local ledger of one's own past
posts (no API call). Scope is the same thread only; matching is exact
after stripping a resend header and normalising whitespace/newlines.

Before this change the ledger was only consulted when the history read
failed (cannot-verify fallback), so an identical own post older than
the newest 100 thread messages was not caught in the normal flow
(see test_slack_send_once_thread_bounded.py case 2, which documents
the history cap). The ledger check now runs first, before any history
read, for thread replies.

Style follows the neighbours (plain Python, no pytest):
  python3 ~/hooks/tests/test_slack_send_once_ledger_parroting.py

Cases (CANDIDATE contains a space, so whitespace variants collapse
to the same normalised form):
  1. ledger has the identical text, same thread, history empty
     -> duplicate (check-only 0), and no history read at all
  2. ledger text differs only by newlines / double spaces
     -> duplicate (0)
  3. ledger text differs only by a full-width space
     -> duplicate (0)
  4. ledger text is the candidate with a resend header in front
     -> duplicate (0)
  5. ledger entry is in a different thread -> not a duplicate (1),
     history read happens
  6. ledger entry is in the same thread but the history is a long
     thread (150 replies) without the candidate -> duplicate (0)
     via the ledger, no history read (the gap the history cap left)
  7. channel post (no thread): ledger entry with the same text
     -> sends (0); the ledger check is thread-only
  8. no ledger file at all -> not a duplicate (1), history read happens
  9. ledger entry with different text, same thread -> not a duplicate (1)
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
OTHER_THREAD_TS = "1794600001.000001"
CHANNEL = "C0CGYGT9V7G"
CANDIDATE = "重複チェック 上限のテスト文面です。"
LEDGER_TS = "1794600123.000001"


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
    """Parent + n_replies messages, oldest first."""
    special = special or {}
    msgs = [{"ts": THREAD_TS, "user": OTHER_ID, "text": "親の投稿です。"}]
    for i in range(1, n_replies + 1):
        user, text = special.get(i, (OTHER_ID, f"filler {i}"))
        msgs.append({"ts": f"{1794600000 + i}.000001",
                     "user": user, "text": text})
    return {"messages": msgs, "has_more": False}


def ledger_entry(text, thread_ts=THREAD_TS, ts=LEDGER_TS):
    return {"ts": ts, "channel": CHANNEL, "thread_ts": thread_ts,
            "text": text}


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
            f.write(STUB)
        os.chmod(stub, 0o755)
        self.env = dict(os.environ, HOME=self.home,
                        SLACKCLI_LOG=self.log,
                        SLACKCLI_HISTORY=self.history_path)

    def run(self, history, ledger=None, check_only=True, thread=True):
        with open(self.history_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
        if ledger is None:
            if os.path.exists(self.ledger_path):
                os.remove(self.ledger_path)
        else:
            with open(self.ledger_path, "w", encoding="utf-8") as f:
                for rec in ledger:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if os.path.exists(self.log):
            os.remove(self.log)
        argv = ["bash", SCRIPT, "--channel", CHANNEL,
                "--message", CANDIDATE]
        if thread:
            argv += ["--thread-ts", THREAD_TS]
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
        empty = {"messages": [], "has_more": False}

        # 1. identical text in the ledger, same thread, empty history
        rc, reads, sends = env.run(
            empty, ledger=[ledger_entry(CANDIDATE)])
        check("ledger identical: duplicate", rc == 0, f"rc={rc}")
        check("ledger identical: no history read (ledger first)",
              len(reads) == 0, f"reads={reads}")
        check("ledger identical: nothing sent",
              len(sends) == 0, f"sends={sends}")

        # 2. whitespace variants collapse to the same text
        variant = "重複チェック\n\n上限のテスト文面です。"
        rc, _, _ = env.run(empty, ledger=[ledger_entry(variant)])
        check("ledger newline variant: duplicate", rc == 0, f"rc={rc}")
        variant2 = "重複チェック  上限のテスト文面です。"
        rc, _, _ = env.run(empty, ledger=[ledger_entry(variant2)])
        check("ledger double-space variant: duplicate",
              rc == 0, f"rc={rc}")

        # 3. full-width space variant
        variant3 = "重複チェック　上限のテスト文面です。"
        rc, _, _ = env.run(empty, ledger=[ledger_entry(variant3)])
        check("ledger full-width-space variant: duplicate",
              rc == 0, f"rc={rc}")

        # 4. resend header in front of the candidate
        headed = ("遅れての再送（元の失敗時刻：2026-11-14 13:46 JST）\n"
                  + CANDIDATE)
        rc, _, _ = env.run(empty, ledger=[ledger_entry(headed)])
        check("ledger resend-header variant: duplicate",
              rc == 0, f"rc={rc}")

        # 5. same text but a different thread -> not a duplicate
        rc, reads, _ = env.run(
            empty, ledger=[ledger_entry(CANDIDATE,
                                        thread_ts=OTHER_THREAD_TS)])
        check("ledger other thread: not a duplicate", rc == 1, f"rc={rc}")
        check("ledger other thread: history read happens",
              len(reads) == 1, f"reads={reads}")

        # 6. long thread without the candidate; ledger still catches it
        hist = make_thread(150)
        rc, reads, _ = env.run(hist, ledger=[ledger_entry(CANDIDATE)])
        check("ledger catches what the 100-cap misses: duplicate",
              rc == 0, f"rc={rc}")
        check("ledger catches what the 100-cap misses: no history read",
              len(reads) == 0, f"reads={reads}")

        # 7. channel post: ledger check is thread-only, so it sends
        rc, _, sends = env.run(
            empty, ledger=[ledger_entry(CANDIDATE, thread_ts="")],
            check_only=False, thread=False)
        check("channel post: ledger ignored, sends", rc == 0, f"rc={rc}")
        check("channel post: send happened",
              len(sends) == 1, f"sends={sends}")

        # 8. no ledger file at all -> normal flow, history read happens
        rc, reads, _ = env.run(empty, ledger=None)
        check("no ledger file: not a duplicate", rc == 1, f"rc={rc}")
        check("no ledger file: history read happens",
              len(reads) == 1, f"reads={reads}")

        # 9. different text in the ledger, same thread -> not a duplicate
        rc, reads, _ = env.run(
            empty, ledger=[ledger_entry("全く別の文面です。")])
        check("ledger different text: not a duplicate",
              rc == 1, f"rc={rc}")
        check("ledger different text: history read happens",
              len(reads) == 1, f"reads={reads}")
    finally:
        env.close()

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
