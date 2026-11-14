#!/usr/bin/env python3
"""Tests for slack-send-locked.sh (flock wrapper around slack-send-once.sh).

Yanayana's send_reply_locked.py (2026-11-14) closes the check-then-send
race: two workers can both pass the pre-send history check and then both
post. The locked wrapper puts the whole check -> send -> record run of
slack-send-once.sh inside one flock section, so the second worker's check
only starts after the first worker has sent and recorded.

Style follows the neighbours (plain Python, no pytest):
  python3 ~/hooks/tests/test_slack_send_locked.py

The stub slackcli keeps posted messages in a JSONL state file:
  - channel read (no --oldest) -> newest --limit messages, after a
    0.5s sleep that widens the race window so two parallel unserialized
    runs reliably overlap in their check phase
  - messages send -> appends one line (atomic O_APPEND) and returns a ts
No real Slack calls, temporary files only.

Cases (channel post, same text from two parallel workers):
  1. locked x2 in parallel -> exactly 1 posted message, both exit 0
  2. a third locked send of the same text afterwards -> still 1 post
  3. control: unlocked (slack-send-once.sh) x2 in parallel -> 2 posts,
     demonstrating the race the lock closes
  4. exit-code passthrough: locked send whose text exactly matches
     another user's recent post -> exit 4, nothing sent
"""
import json
import os
import subprocess
import sys
import tempfile
import threading

LOCKED = "/home/hatch/hooks/scripts/slack-send-locked.sh"
UNLOCKED = "/home/hatch/hooks/scripts/slack-send-once.sh"

PASS = 0
FAIL = 0

SELF_ID = "U0CH1B19D4J"
OTHER_ID = "U0CH1QBC8M8"
CHANNEL = "C0CGUA5LH5X"
CANDIDATE = "直列化ラッパーの並行テスト文面です。"


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


STUB = r"""#!/usr/bin/env python3
import json, os, sys, time

args = sys.argv[1:]
state_path = os.environ["SLACKCLI_STATE"]

if args and args[0] == "auth":
    print("neiro (U0CH1B19D4J)")
    sys.exit(0)

def read_state():
    msgs = []
    try:
        with open(state_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    msgs.append(json.loads(line))
    except OSError:
        pass
    return msgs

if args and args[0] == "messages":
    # --message-file <path> carries the text; append one JSON line.
    msg_file = args[args.index("--message-file") + 1]
    with open(msg_file, encoding="utf-8") as f:
        text = f.read()
    ts = f"{time.time():.6f}"
    rec = {"ts": ts, "user": "U0CH1B19D4J", "text": text}
    with open(state_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps({"ts": ts}))
    sys.exit(0)

if args and args[0] == "conversations":
    # Widen the race window: every history read takes 0.5s, so two
    # parallel unserialized runs overlap in check phase for sure.
    time.sleep(0.5)
    msgs = read_state()
    def opt(name, default=None):
        if name in args:
            return args[args.index(name) + 1]
        return default
    limit = int(opt("--limit", "100"))
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
        self.state_path = os.path.join(self.home, "state.jsonl")
        stub = os.path.join(bindir, "slackcli")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(STUB)
        os.chmod(stub, 0o755)
        self.env = dict(os.environ, HOME=self.home,
                        SLACKCLI_STATE=self.state_path)

    def reset(self, seed=None):
        """Empty posted state (and send ledger) before a case."""
        with open(self.state_path, "w", encoding="utf-8") as f:
            for m in (seed or []):
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
        ledger = os.path.join(self.home, "hooks", "state",
                              "slack-send-log.jsonl")
        if os.path.exists(ledger):
            os.remove(ledger)

    def posted(self, text):
        msgs = []
        try:
            with open(self.state_path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        msgs.append(json.loads(line))
        except OSError:
            pass
        return [m for m in msgs if m.get("text") == text
                and m.get("user") == SELF_ID]

    def run_one(self, script, text=CANDIDATE):
        argv = ["bash", script, "--channel", CHANNEL,
                "--message", text]
        return subprocess.run(argv, env=self.env, capture_output=True,
                              text=True, timeout=180).returncode

    def run_parallel(self, script, n=2, text=CANDIDATE):
        results = [None] * n

        def work(i):
            results[i] = self.run_one(script, text)

        threads = [threading.Thread(target=work, args=(i,))
                   for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return results

    def close(self):
        self.d.cleanup()


def main():
    env = Env()
    try:
        # 1. locked x2 in parallel -> exactly 1 post, both exit 0
        env.reset()
        rcs = env.run_parallel(LOCKED, n=2)
        posts = env.posted(CANDIDATE)
        check("locked parallel x2: both exit 0", rcs == [0, 0],
              f"rcs={rcs}")
        check("locked parallel x2: exactly 1 post", len(posts) == 1,
              f"posts={len(posts)}")

        # 2. a third locked send afterwards -> still 1 post
        rc = env.run_one(LOCKED)
        posts = env.posted(CANDIDATE)
        check("locked re-run: exit 0", rc == 0, f"rc={rc}")
        check("locked re-run: still 1 post", len(posts) == 1,
              f"posts={len(posts)}")

        # 3. control: unlocked x2 in parallel -> 2 posts (the race)
        env.reset()
        rcs = env.run_parallel(UNLOCKED, n=2)
        posts = env.posted(CANDIDATE)
        check("unlocked parallel x2: both exit 0", rcs == [0, 0],
              f"rcs={rcs}")
        check("unlocked parallel x2: 2 posts (race shown)",
              len(posts) == 2, f"posts={len(posts)}")

        # 4. exit-code passthrough: text matches another user's post
        env.reset(seed=[{"ts": "1794700000.000001", "user": OTHER_ID,
                         "text": CANDIDATE}])
        rc = env.run_one(LOCKED)
        posts = env.posted(CANDIDATE)
        check("other user's identical post: exit 4", rc == 4,
              f"rc={rc}")
        check("other user's identical post: nothing sent",
              len(posts) == 0, f"posts={len(posts)}")
    finally:
        env.close()

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
