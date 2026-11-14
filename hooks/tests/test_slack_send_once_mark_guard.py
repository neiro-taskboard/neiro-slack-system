#!/usr/bin/env python3
"""Tests for the different-mark guard in slack-send-once.sh.

The design (2026-11-14, muse取締役会 #slack-へようこそ 1794621025.648949)
says a post carrying a *different* event_id must be excluded from
text-match candidates, but the code had not caught up: the ledger check
matched on text alone, and the thread text check read through slackcli,
which never returns metadata, so the mark difference was invisible.
The fix (2026-11-15):
  - ledger: a record whose mark (event_id / request_id) differs from
    the current send's mark is skipped as a text-match candidate.
    Records without a mark, and sends without a mark, still match
    on text as before.
  - thread: for a marked send, the text check reads through
    slack-read-metadata.py --dump (include_all_metadata) and skips
    own posts whose mark differs from the current send's mark.
    Posts without a mark still match on text as before.

Style follows the neighbours (plain Python, no pytest):
  python3 ~/hooks/tests/test_slack_send_once_mark_guard.py

The stub slackcli is the same one the neighbours use. The metadata
reader is stubbed too: the script under test is copied into a temp
directory and a stub slack-read-metadata.py is placed next to it
(the real one would call the live Slack API with the real token
store, which tests must never do). The stub is driven by files:
  SLACK_META_FIND  JSON {"found": bool, "ts": ...} for the find mode
  SLACK_META_DUMP  JSON {"messages": [...]} for the --dump mode
  SLACK_META_FIND_FAIL / SLACK_META_DUMP_FAIL == "1" makes that mode
  answer {"ok": false}. Calls are logged to SLACK_META_LOG.

Cases (CANDIDATE is the text being sent; EVENT_ID is the current
event, OTHER_EVENT_ID / OTHER_REQUEST_ID are different marks):
  Ledger (thread reply, --check-only):
  L1. record same text + same event_id -> duplicate (0), no reads
  L2. record same text + different event_id -> not a duplicate (1)
  L3. record same text + request_id, current is event_id -> not (1)
  L4. record same text, no mark -> duplicate (0)
  L5. current --request-id, record same text + same request_id -> (0)
  L6. current --request-id, record same text + other request_id -> (1)
  L7. current --request-id, record same text + event_id -> (1)
  L8. current unmarked, record same text + event_id -> duplicate (0)
  Thread / dump (marked sends, find stub answers found:false):
  T1. dump own post same text + different event_id -> not (1)
  T2. dump own post same text, no mark -> duplicate (0)
  T3. dump own post same text + request_id, current is event_id -> (1)
  T4. find stub answers found:true (same mark) -> duplicate (0)
      via metadata, dump is never called
  T5. find stub answers found:false, dump own post same text + same
      event_id -> duplicate (0) via the dump text match
  T6. dump fails -> check-only exit 2 (cannot-verify);
      T6b. same, actually sending -> exit 3 (fail-closed)
  T7. find fails, dump own post same text, no mark -> duplicate (0)
      (metadata read failure falls back to the text check)
  T8. unmarked send: dump is never called, the slackcli thread read
      is used, own post same text -> duplicate (0)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

SCRIPT_SRC = "/home/hatch/hooks/scripts/slack-send-once.sh"

PASS = 0
FAIL = 0

SELF_ID = "U0CH1B19D4J"
OTHER_ID = "U0CH1QBC8M8"
THREAD_TS = "1794600000.000001"
CHANNEL = "C0CGYGT9V7G"
CANDIDATE = "別の event_id のガードのテスト文面です。"
EVENT_ID = "T0CGV6V7161:C0CGYGT9V7G:1794652249.960829"
OTHER_EVENT_ID = "T0CGV6V7161:C0CGYGT9V7G:1794650000.000000"
REQUEST_ID = "123e4567-e89b-12d3-a456-426614174000"
OTHER_REQUEST_ID = "123e4567-e89b-12d3-a456-426614174001"
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

# Stub for slack-read-metadata.py. The real one would call the live
# Slack API with the real token store; tests must never do that.
# Driven by SLACK_META_FIND / SLACK_META_DUMP (JSON files) and
# SLACK_META_FIND_FAIL / SLACK_META_DUMP_FAIL (== "1" to fail).
META_STUB = r"""#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
with open(os.environ["SLACK_META_LOG"], "a", encoding="utf-8") as f:
    f.write(" ".join(args) + "\n")

if "--dump" in args:
    if os.environ.get("SLACK_META_DUMP_FAIL") == "1":
        print(json.dumps({"ok": False, "error": "boom"}))
        sys.exit(1)
    with open(os.environ["SLACK_META_DUMP"], encoding="utf-8") as f:
        data = json.load(f)
    msgs = data.get("messages", [])
    print(json.dumps({"ok": True, "messages": msgs,
                      "checked": len(msgs)}))
    sys.exit(0)

if os.environ.get("SLACK_META_FIND_FAIL") == "1":
    print(json.dumps({"ok": False, "error": "boom"}))
    sys.exit(1)
with open(os.environ["SLACK_META_FIND"], encoding="utf-8") as f:
    data = json.load(f)
print(json.dumps({"ok": True, "found": data.get("found", False),
                  "ts": data.get("ts"),
                  "checked": data.get("checked", 0)}))
sys.exit(0)
"""


def ledger_entry(text, thread_ts=THREAD_TS, ts=LEDGER_TS,
                 event_id=None, request_id=None):
    rec = {"ts": ts, "channel": CHANNEL, "thread_ts": thread_ts,
           "text": text}
    if event_id is not None:
        rec["event_id"] = event_id
    if request_id is not None:
        rec["request_id"] = request_id
    return rec


def dump_msg(ts, user, text, mark=None):
    return {"ts": ts, "user": user, "text": text, "mark": mark}


def mark_event(event_id):
    return {"kind": "event_id", "value": event_id}


def mark_request(request_id):
    return {"kind": "request_id", "value": request_id}


class Env:
    """Temp HOME with stub slackcli + a copied script and stub metadata reader."""

    def __init__(self):
        self.d = tempfile.TemporaryDirectory()
        self.home = self.d.name
        bindir = os.path.join(self.home, "workspace", "bin")
        os.makedirs(bindir, exist_ok=True)
        os.makedirs(os.path.join(self.home, "hooks", "state"),
                    exist_ok=True)
        scriptdir = os.path.join(self.home, "scripts")
        os.makedirs(scriptdir, exist_ok=True)
        self.log = os.path.join(self.home, "slackcli.log")
        self.history_path = os.path.join(self.home, "history.json")
        self.ledger_path = os.path.join(
            self.home, "hooks", "state", "slack-send-log.jsonl")
        self.meta_log = os.path.join(self.home, "meta.log")
        self.meta_find_path = os.path.join(self.home, "meta-find.json")
        self.meta_dump_path = os.path.join(self.home, "meta-dump.json")
        stub = os.path.join(bindir, "slackcli")
        with open(stub, "w", encoding="utf-8") as f:
            f.write(STUB)
        os.chmod(stub, 0o755)
        # The script under test is copied next to the stub metadata
        # reader, so $SCRIPT_DIR resolves to the temp directory.
        self.script = os.path.join(scriptdir, "slack-send-once.sh")
        shutil.copyfile(SCRIPT_SRC, self.script)
        meta_stub = os.path.join(scriptdir, "slack-read-metadata.py")
        with open(meta_stub, "w", encoding="utf-8") as f:
            f.write(META_STUB)
        os.chmod(meta_stub, 0o755)
        self.env = dict(os.environ, HOME=self.home,
                        SLACKCLI_LOG=self.log,
                        SLACKCLI_HISTORY=self.history_path,
                        SLACK_META_LOG=self.meta_log,
                        SLACK_META_FIND=self.meta_find_path,
                        SLACK_META_DUMP=self.meta_dump_path)

    def run(self, history=None, ledger=None, event_id=None,
            request_id=None, find=None, dump=None,
            find_fail=False, dump_fail=False, check_only=True):
        history = history or {"messages": [], "has_more": False}
        with open(self.history_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
        if ledger is None:
            if os.path.exists(self.ledger_path):
                os.remove(self.ledger_path)
        else:
            with open(self.ledger_path, "w", encoding="utf-8") as f:
                for rec in ledger:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        with open(self.meta_find_path, "w", encoding="utf-8") as f:
            json.dump(find or {"found": False, "ts": None}, f)
        with open(self.meta_dump_path, "w", encoding="utf-8") as f:
            json.dump({"messages": dump or []}, f)
        for path in (self.log, self.meta_log):
            if os.path.exists(path):
                os.remove(path)
        env = dict(self.env)
        env["SLACK_META_FIND_FAIL"] = "1" if find_fail else "0"
        env["SLACK_META_DUMP_FAIL"] = "1" if dump_fail else "0"
        argv = ["bash", self.script, "--channel", CHANNEL,
                "--thread-ts", THREAD_TS, "--message", CANDIDATE]
        if check_only:
            argv.append("--check-only")
        if event_id is not None:
            argv += ["--event-id", event_id]
        if request_id is not None:
            argv += ["--request-id", request_id]
        proc = subprocess.run(argv, env=env, capture_output=True,
                              text=True, timeout=180)
        calls = []
        if os.path.exists(self.log):
            with open(self.log, encoding="utf-8") as f:
                calls = [ln.strip() for ln in f if ln.strip()]
        meta_calls = []
        if os.path.exists(self.meta_log):
            with open(self.meta_log, encoding="utf-8") as f:
                meta_calls = [ln.strip() for ln in f if ln.strip()]
        reads = [c for c in calls if c.startswith("conversations")]
        sends = [c for c in calls if c.startswith("messages")]
        dumps = [c for c in meta_calls if "--dump" in c]
        finds = [c for c in meta_calls if "--dump" not in c]
        return {"rc": proc.returncode, "reads": reads, "sends": sends,
                "dumps": dumps, "finds": finds,
                "stdout": proc.stdout, "stderr": proc.stderr}

    def close(self):
        self.d.cleanup()


def main():
    env = Env()
    try:
        empty = {"messages": [], "has_more": False}

        # L1. ledger: same text + same event_id -> duplicate
        r = env.run(empty, ledger=[ledger_entry(CANDIDATE,
                                                 event_id=EVENT_ID)],
                    event_id=EVENT_ID)
        check("L1 ledger same event_id: duplicate", r["rc"] == 0,
              f"rc={r['rc']} {r['stderr']}")
        check("L1 ledger same event_id: no history read",
              len(r["reads"]) == 0, f"reads={r['reads']}")
        check("L1 ledger same event_id: no metadata read",
              len(r["finds"]) == 0 and len(r["dumps"]) == 0,
              f"finds={r['finds']} dumps={r['dumps']}")

        # L2. ledger: same text + different event_id -> not a duplicate
        r = env.run(empty, ledger=[ledger_entry(CANDIDATE,
                                                 event_id=OTHER_EVENT_ID)],
                    event_id=EVENT_ID)
        check("L2 ledger different event_id: not a duplicate",
              r["rc"] == 1, f"rc={r['rc']} {r['stderr']}")
        check("L2 ledger different event_id: dump was consulted",
              len(r["dumps"]) == 1, f"dumps={r['dumps']}")

        # L3. ledger: same text + request_id, current is event_id
        r = env.run(empty, ledger=[ledger_entry(CANDIDATE,
                                                 request_id=REQUEST_ID)],
                    event_id=EVENT_ID)
        check("L3 ledger request_id vs event_id: not a duplicate",
              r["rc"] == 1, f"rc={r['rc']} {r['stderr']}")

        # L4. ledger: same text, no mark -> duplicate
        r = env.run(empty, ledger=[ledger_entry(CANDIDATE)],
                    event_id=EVENT_ID)
        check("L4 ledger no mark: duplicate", r["rc"] == 0,
              f"rc={r['rc']} {r['stderr']}")

        # L5. current --request-id, ledger same text + same request_id
        r = env.run(empty, ledger=[ledger_entry(CANDIDATE,
                                                 request_id=REQUEST_ID)],
                    request_id=REQUEST_ID)
        check("L5 ledger same request_id: duplicate", r["rc"] == 0,
              f"rc={r['rc']} {r['stderr']}")

        # L6. current --request-id, ledger same text + other request_id
        r = env.run(empty, ledger=[ledger_entry(CANDIDATE,
                                                 request_id=OTHER_REQUEST_ID)],
                    request_id=REQUEST_ID)
        check("L6 ledger different request_id: not a duplicate",
              r["rc"] == 1, f"rc={r['rc']} {r['stderr']}")

        # L7. current --request-id, ledger same text + event_id
        r = env.run(empty, ledger=[ledger_entry(CANDIDATE,
                                                 event_id=EVENT_ID)],
                    request_id=REQUEST_ID)
        check("L7 ledger event_id vs request_id: not a duplicate",
              r["rc"] == 1, f"rc={r['rc']} {r['stderr']}")

        # L8. current unmarked, ledger same text + event_id -> duplicate
        r = env.run(empty, ledger=[ledger_entry(CANDIDATE,
                                                 event_id=EVENT_ID)])
        check("L8 unmarked send, ledger marked record: duplicate",
              r["rc"] == 0, f"rc={r['rc']} {r['stderr']}")

        # T1. dump: own post same text + different event_id -> not (1)
        r = env.run(empty, event_id=EVENT_ID, dump=[
            dump_msg("1794600100.000001", SELF_ID, CANDIDATE,
                     mark_event(OTHER_EVENT_ID))])
        check("T1 dump different event_id: not a duplicate",
              r["rc"] == 1, f"rc={r['rc']} {r['stderr']}")
        check("T1 dump different event_id: slackcli read not used",
              len(r["reads"]) == 0, f"reads={r['reads']}")

        # T2. dump: own post same text, no mark -> duplicate
        r = env.run(empty, event_id=EVENT_ID, dump=[
            dump_msg("1794600100.000001", SELF_ID, CANDIDATE)])
        check("T2 dump no mark: duplicate", r["rc"] == 0,
              f"rc={r['rc']} {r['stderr']}")

        # T3. dump: own post same text + request_id, current event_id
        r = env.run(empty, event_id=EVENT_ID, dump=[
            dump_msg("1794600100.000001", SELF_ID, CANDIDATE,
                     mark_request(REQUEST_ID))])
        check("T3 dump request_id vs event_id: not a duplicate",
              r["rc"] == 1, f"rc={r['rc']} {r['stderr']}")

        # T4. find answers found:true (same mark) -> duplicate via
        # metadata, dump never called
        r = env.run(empty, event_id=EVENT_ID,
                    find={"found": True, "ts": "1794600200.000001"},
                    dump=[dump_msg("1794600100.000001", SELF_ID,
                                   CANDIDATE, mark_event(EVENT_ID))])
        check("T4 metadata match: duplicate", r["rc"] == 0,
              f"rc={r['rc']} {r['stderr']}")
        check("T4 metadata match: dump not called",
              len(r["dumps"]) == 0, f"dumps={r['dumps']}")

        # T5. find misses, dump own post same text + same event_id
        # -> duplicate via the dump text match
        r = env.run(empty, event_id=EVENT_ID, dump=[
            dump_msg("1794600100.000001", SELF_ID, CANDIDATE,
                     mark_event(EVENT_ID))])
        check("T5 dump same event_id: duplicate", r["rc"] == 0,
              f"rc={r['rc']} {r['stderr']}")

        # T6. dump fails -> check-only: exit 2 (cannot-verify).
        # The script's contract: --check-only uses 2 for "could not
        # verify"; the non-check-only path exits 3 (fail-closed, T6b).
        r = env.run(empty, event_id=EVENT_ID, dump_fail=True)
        check("T6 dump unreadable: exit 2 (check-only cannot-verify)",
              r["rc"] == 2, f"rc={r['rc']} {r['stderr']}")
        check("T6 dump unreadable: nothing sent",
              len(r["sends"]) == 0, f"sends={r['sends']}")
        # T6b. same, but actually sending -> exit 3 (fail-closed)
        r = env.run(empty, event_id=EVENT_ID, dump_fail=True,
                    check_only=False)
        check("T6b dump unreadable, sending: exit 3 (fail-closed)",
              r["rc"] == 3, f"rc={r['rc']} {r['stderr']}")
        check("T6b dump unreadable, sending: nothing sent",
              len(r["sends"]) == 0, f"sends={r['sends']}")

        # T7. find fails, dump own post same text, no mark -> duplicate
        r = env.run(empty, event_id=EVENT_ID, find_fail=True, dump=[
            dump_msg("1794600100.000001", SELF_ID, CANDIDATE)])
        check("T7 find fails, dump no mark: duplicate",
              r["rc"] == 0, f"rc={r['rc']} {r['stderr']}")

        # T8. unmarked send: dump never called, slackcli read used
        hist = {"messages": [
            {"ts": THREAD_TS, "user": OTHER_ID, "text": "親の投稿です。"},
            {"ts": "1794600100.000001", "user": SELF_ID,
             "text": CANDIDATE}],
                "has_more": False}
        r = env.run(hist)
        check("T8 unmarked send: duplicate", r["rc"] == 0,
              f"rc={r['rc']} {r['stderr']}")
        check("T8 unmarked send: dump not called",
              len(r["dumps"]) == 0 and len(r["finds"]) == 0,
              f"dumps={r['dumps']} finds={r['finds']}")
        check("T8 unmarked send: slackcli read used",
              len(r["reads"]) == 1, f"reads={r['reads']}")
    finally:
        env.close()

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
