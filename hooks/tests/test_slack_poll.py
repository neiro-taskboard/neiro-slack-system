#!/usr/bin/env python3
"""E2E tests for slack-poll.py — real script, isolated state files.

Style follows the neighbours' approach (Yanayana: E2E with isolated state
files instead of fake clients; Yama: one plain Python script, no pytest):
  python3 ~/hooks/tests/test_slack_poll.py

Each test runs the real slack-poll.py as a subprocess with
SLACK_POLLER_INBOX / SLACK_POLLER_STATE_FILE / SLACK_POLLER_DEAD_FILE
pointed at temp files, so the live inbox/state are never touched.

Covered:
  1. new event -> wake, attempts=1, status=processing
  2. re-poll inside the timeout -> silent (no double wake)
  3. stale processing -> one re-wake, attempts +1 exactly once
     (the claim consumes the attempt; the sweep itself burns none).
     Seeded first_wake_at is older than the start grace, so the retry
     path is exercised.
  4. stale processing at MAX_ATTEMPTS -> dead + dead-letter line
     (first_wake_at older than the start grace, as in 3)
  5. failed (stale) -> retry; failed at MAX_ATTEMPTS -> dead
  6. done -> silent, never re-woken
  7. malformed inbox lines are skipped
  8. skipped-line counts are recorded as a snapshot (Yama's proposal,
     Yanayana's snapshot approach): counts only, no line contents, and
     re-polling does not double-count
  9. 10 parallel flock-guarded polls -> exactly one wake (claim is single-flight,
     mirroring the flock in slack-new-messages.sh)
 10. waiting (pre-check failure) -> re-woken within 24h with first_wait_at
     unchanged and attempts untouched; past 24h -> dead + dead-letter line
     once, never re-woken (Yama's test 26 boundary pattern: 23h59m vs 24h01m)
 11. waiting re-wake -> payload event carries resend annotation
     (first_wait_at + JST + prefix) for the "遅れての再送" prefix;
     new events don't. prefix はコード側で固定した
     「遅れての再送（元の失敗時刻：YYYY-MM-DD HH:MM JST）」（2026-11-14 の3者合意。
     一時は「最初の試行」に寄せる合意だったが、同日のレビューで元の文言に
     戻すことで3者統一した。Yama さんの共有 2026-11-14）
 12. start grace (2026-11-14): a stale processing event within 3h of its
     first wake is left alone (no attempt burned, no dead-letter, no
     re-wake); past the grace the normal retry/dead clock resumes.
     failed has no grace (the worker did start).
 13. fastpath reclaim (2026-11-14): with SLACK_POLL_FASTPATH=1, a
     processing event older than FASTPATH_RECLAIM_SECS (default 120s)
     is handed to the cron sweeper even inside the start grace, with a
     fastpath_reclaim annotation; attempts and first_wake_at are left
     untouched. Without the env var the grace behaviour is unchanged.
 14. burst aging (2026-11-21): a plain (\"その他\") event waiting over
     5 minutes is promoted to the Neiro-addressed tier, and over
     15 minutes to the DM tier, so lower-priority traffic cannot starve
     while DMs and mentions keep flowing; within a tier, oldest first.
 15. fastpath reclaim keeps the resend note (中-2, 2026-11-22): a
     processing event that went through waiting is re-dispatched with
     the same resend annotation (first_wait_at + JST + prefix); one
     without waiting history gets none.
 16. stale retry keeps the resend note (中-2, 2026-11-22): same shared
     annotation as 15, for the stale processing / failed retry path.
 17. reading limits (2026-11-22, Yama・Yanayana との合意を取り入れ):
     an event longer than MAX_EVENT_TEXT_CHARS (3,000) is cut in the
     wake payload, with text_truncated carrying the original length;
     a short event is untouched and gets no annotation.
 18. wake total budget (2026-11-22): when the dispatched events' texts
     exceed MAX_WAKE_TEXT_CHARS, the oldest events' texts are trimmed
     first (newest kept longest), each keeping at least the floor, and
     no event is dropped.
 19. waiting staged intervals (2026-11-25, Yama方式): the re-wake
     interval widens in stages 2m/5m/15m/30m/60m (then stays 60m),
     counted in wait_wakes; the first sight of a waiting record
     schedules stage 1 without waking.
 20. reclaim cutoff and backoff (2026-11-25, Yama方式): fastpath
     reclaim stops at first_wake + 3h and at RECLAIM_MAX_COUNT, and
     the required processing age widens with reclaim_count
     (120s -> 5m -> 15m -> 30m); the count is recorded and never
     reset by an automatic path.
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

def make_id(prefix, rest):
    """Build an ID-shaped string from parts (like a real Slack ID).

    Fixtures are synthetic (2026-11-21): the real IDs used before were
    other people's and ours; parts-built fakes keep the tests passing
    without leaking real IDs into the public repo.
    """
    return prefix + "0" + rest


WS_MUSE = make_id("T", "TESTWSMUSE1")
CHANNEL = make_id("C", "TESTCHAN01")
USER_ID = make_id("U", "TESTHUMAN01")


POLL_PY = "/home/hatch/hooks/scripts/slack-poll.py"
TIMEOUT_SECS = 600

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


def now_iso(dt=None):
    return (dt or datetime.now(timezone.utc)).isoformat()


def make_event(ts="1794476892.695469", channel=CHANNEL,
               workspace=WS_MUSE):
    return {"workspace_id": workspace, "channel": channel, "ts": ts,
            "user": USER_ID, "text": "test"}


class Env:
    """Temp inbox/state/dead files + env for one poller run."""

    def __init__(self):
        self.d = tempfile.TemporaryDirectory()
        self.inbox = os.path.join(self.d.name, "inbox.jsonl")
        self.state = os.path.join(self.d.name, "state.jsonl")
        self.dead = os.path.join(self.d.name, "dead.jsonl")
        self.stats = os.path.join(self.d.name, "stats.json")
        self.lock = os.path.join(self.d.name, "poll.lock")
        self.env = dict(os.environ)
        self.env.update({
            "SLACK_POLLER_INBOX": self.inbox,
            "SLACK_POLLER_STATE_FILE": self.state,
            "SLACK_POLLER_DEAD_FILE": self.dead,
            "SLACK_POLLER_STATS_FILE": self.stats,
        })
        self.env.pop("SLACK_POLL_DRY_RUN", None)

    def write_inbox(self, events, raw_lines=None):
        with open(self.inbox, "w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
            for line in (raw_lines or []):
                f.write(line + "\n")

    def write_state(self, records):
        with open(self.state, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def read_state(self):
        state = {}
        if os.path.exists(self.state):
            with open(self.state, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rec = json.loads(line)
                        state[rec["event_id"]] = rec
        return state

    def read_dead(self):
        if not os.path.exists(self.dead):
            return []
        with open(self.dead, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    def read_stats(self):
        if not os.path.exists(self.stats):
            return None
        with open(self.stats, encoding="utf-8") as f:
            return json.load(f)

    def run(self, use_flock=False):
        cmd = [sys.executable, POLL_PY]
        if use_flock:
            cmd = ["flock", self.lock] + cmd
        r = subprocess.run(cmd, capture_output=True, text=True,
                           env=self.env, timeout=30)
        if r.returncode != 0:
            raise RuntimeError(f"poller failed: {r.stderr.strip()}")
        return json.loads(r.stdout)


def eid(ev):
    return f"{ev['workspace_id']}:{ev['channel']}:{ev['ts']}"


def test_new_event_wakes_once():
    env = Env()
    ev = make_event()
    env.write_inbox([ev])
    out = env.run()
    check("new event -> wake", out["decision"] == "wake", out)
    rec = env.read_state()[eid(ev)]
    check("new event -> attempts=1, processing",
          rec["attempts"] == 1 and rec["status"] == "processing", rec)
    check("new event -> first_wake_at recorded",
          bool(rec.get("first_wake_at")), rec)
    out2 = env.run()
    check("re-poll inside timeout -> silent (no double wake)",
          out2["decision"] == "silent", out2)
    rec2 = env.read_state()[eid(ev)]
    check("re-poll does not touch attempts", rec2["attempts"] == 1, rec2)


def test_stale_processing_rewakes_once():
    env = Env()
    ev = make_event()
    old = datetime.now(timezone.utc) - timedelta(seconds=TIMEOUT_SECS + 60)
    # first_wake_at is older than the 3h start grace, so the retry path applies.
    first = datetime.now(timezone.utc) - timedelta(hours=4)
    env.write_inbox([ev])
    env.write_state([{"event_id": eid(ev), "status": "processing",
                      "attempts": 1, "updated_at": now_iso(old),
                      "first_wake_at": now_iso(first),
                      "last_error": ""}])
    out = env.run()
    check("stale processing -> wake", out["decision"] == "wake", out)
    rec = env.read_state()[eid(ev)]
    check("sweep burns no attempt without a claim: attempts 1 -> 2",
          rec["attempts"] == 2 and rec["status"] == "processing", rec)


def test_stale_at_max_attempts_dead():
    env = Env()
    ev = make_event()
    old = datetime.now(timezone.utc) - timedelta(seconds=TIMEOUT_SECS + 60)
    # first_wake_at is older than the 3h start grace, so the dead path applies.
    first = datetime.now(timezone.utc) - timedelta(hours=4)
    env.write_inbox([ev])
    env.write_state([{"event_id": eid(ev), "status": "processing",
                      "attempts": 3, "updated_at": now_iso(old),
                      "first_wake_at": now_iso(first),
                      "last_error": ""}])
    out = env.run()
    check("stale at max attempts -> reported as dead-letter wake, no event",
          out["decision"] == "wake"
          and not out["payload"].get("events")
          and len(out["payload"].get("dead_letter", [])) == 1, out)
    rec = env.read_state()[eid(ev)]
    check("stale at max attempts -> dead", rec["status"] == "dead", rec)
    dead = env.read_dead()
    check("dead-letter line appended exactly once", len(dead) == 1, dead)
    check("newly dead reported in payload",
          len(out["payload"].get("dead_letter", [])) == 1, out["payload"])
    out2 = env.run()
    check("dead event never re-woken", out2["decision"] == "silent", out2)
    check("no duplicate dead-letter line", len(env.read_dead()) == 1)


def test_failed_retries_then_dead():
    env = Env()
    ev = make_event()
    old = datetime.now(timezone.utc) - timedelta(seconds=TIMEOUT_SECS + 60)
    env.write_inbox([ev])
    env.write_state([{"event_id": eid(ev), "status": "failed",
                      "attempts": 2, "updated_at": now_iso(old),
                      "last_error": "boom"}])
    out = env.run()
    rec = env.read_state()[eid(ev)]
    check("stale failed -> retry wake, attempts 2 -> 3",
          out["decision"] == "wake" and rec["attempts"] == 3, rec)
    # Worker fails again; next stale poll must dead-letter it.
    old2 = datetime.now(timezone.utc) - timedelta(seconds=TIMEOUT_SECS + 60)
    env.write_state([{"event_id": eid(ev), "status": "failed",
                      "attempts": 3, "updated_at": now_iso(old2),
                      "last_error": "boom"}])
    env.run()
    rec2 = env.read_state()[eid(ev)]
    check("failed at max attempts -> dead", rec2["status"] == "dead", rec2)


def test_done_never_rewoken():
    env = Env()
    ev = make_event()
    old = datetime.now(timezone.utc) - timedelta(days=1)
    env.write_inbox([ev])
    env.write_state([{"event_id": eid(ev), "status": "done",
                      "attempts": 1, "updated_at": now_iso(old),
                      "last_error": ""}])
    out = env.run()
    check("done -> silent even when old", out["decision"] == "silent", out)


def test_malformed_lines_skipped():
    env = Env()
    ev = make_event()
    env.write_inbox([ev], raw_lines=["not json {", "", "  "])
    out = env.run()
    check("malformed inbox lines skipped, good event still wakes",
          out["decision"] == "wake", out)


def test_skipped_lines_counted_snapshot():
    env = Env()
    ev = make_event()
    env.write_inbox([ev], raw_lines=["not json {", "", "  "])
    # Seed a broken line into the state file as well.
    with open(env.state, "w", encoding="utf-8") as f:
        f.write("{broken\n")
    env.run()
    stats = env.read_stats()
    check("skipped inbox lines counted (3: unparsable + 2 blank)",
          stats and stats.get("inbox_skipped_lines") == 3, stats)
    check("skipped state lines counted (1)",
          stats and stats.get("state_skipped_lines") == 1, stats)
    check("stats holds counts only, no line contents",
          stats is not None and "not json" not in json.dumps(stats)
          and "{broken" not in json.dumps(stats), stats)
    env.run()
    stats2 = env.read_stats()
    check("re-poll does not double-count (snapshot)",
          stats2 and stats2.get("inbox_skipped_lines") == 3, stats2)
    check("state count drops to 0: save_state rewrote the file clean",
          stats2 and stats2.get("state_skipped_lines") == 0, stats2)
    # Fix the broken lines: counts drop back to 0.
    env.write_inbox([ev])
    with open(env.state, "w", encoding="utf-8") as f:
        pass
    env.run()
    stats3 = env.read_stats()
    check("fixed lines -> counts drop to 0",
          stats3 and stats3.get("inbox_skipped_lines") == 0
          and stats3.get("state_skipped_lines") == 0, stats3)


def test_waiting_boundary_24h():
    # Yamaさんのテスト26（DLQ改訂）の境界パターンを、うちの waiting にも。
    # 23時間59分ではまだ起こし直され、窓の起点 first_wait_at は変わらず、
    # attempts も消費されない。24時間01分では dead になってデッドレターに
    # 1行だけ残り、二度と起こされない。
    env = Env()
    ev = make_event()
    now = datetime.now(timezone.utc)
    first = now - timedelta(hours=23, minutes=59)
    env.write_inbox([ev])
    env.write_state([{"event_id": eid(ev), "status": "waiting", "attempts": 1,
                      "first_wait_at": now_iso(first),
                      "next_wake_at": now_iso(now - timedelta(seconds=10)),
                      "updated_at": now_iso(first),
                      "last_error": "pre-check unverifiable"}])
    out = env.run()
    check("waiting 23h59m -> re-woken",
          out["decision"] == "wake" and len(out["payload"]["events"]) == 1, out)
    rec = env.read_state()[eid(ev)]
    check("waiting 23h59m -> first_wait_at unchanged",
          rec.get("first_wait_at") == now_iso(first), rec)
    check("waiting re-wake does not consume attempts",
          rec.get("attempts") == 1, rec)

    # 24時間01分では諦めとして dead へ。
    first_expired = now - timedelta(hours=24, minutes=1)
    env.write_state([{"event_id": eid(ev), "status": "waiting", "attempts": 1,
                      "first_wait_at": now_iso(first_expired),
                      "next_wake_at": now_iso(now - timedelta(seconds=10)),
                      "updated_at": now_iso(first_expired),
                      "last_error": "pre-check unverifiable"}])
    out2 = env.run()
    check("waiting 24h01m -> dead-lettered, not re-woken as event",
          out2["decision"] == "wake"
          and len(out2["payload"]["events"]) == 0
          and len(out2["payload"]["dead_letter"]) == 1, out2)
    rec2 = env.read_state()[eid(ev)]
    check("waiting 24h01m -> status dead", rec2["status"] == "dead", rec2)
    check("dead reason mentions 24h window",
          "24h window expired" in (rec2.get("last_error") or ""), rec2)
    check("dead-letter line appended exactly once",
          len(env.read_dead()) == 1, env.read_dead())
    out3 = env.run()
    check("dead waiting event never re-woken",
          out3["decision"] == "silent", out3)
    check("no duplicate dead-letter line", len(env.read_dead()) == 1)


def test_waiting_rewake_annotates_resend():
    # DLQ再送の最終形（2026-11-14、Yanayana の reconcile_ambiguous.py 参考）:
    # waiting で起こし直されるとき、ペイロードのイベントに resend が付き、
    # first_wait_at（元の失敗時刻）と JST 表示と、コード側で固定した
    # 接頭辞 prefix（「遅れての再送（元の失敗時刻：…）」）が入る。新規イベントには付かない。
    env = Env()
    ev = make_event()
    ev_new = make_event(ts="1794476893.695469")
    now = datetime.now(timezone.utc)
    first = now - timedelta(hours=1)
    env.write_inbox([ev, ev_new])
    env.write_state([{"event_id": eid(ev), "status": "waiting", "attempts": 1,
                      "first_wait_at": now_iso(first),
                      "next_wake_at": now_iso(now - timedelta(seconds=10)),
                      "updated_at": now_iso(first),
                      "last_error": "pre-check unverifiable"}])
    out = env.run()
    check("waiting re-wake + new event -> wake with 2 events",
          out["decision"] == "wake" and len(out["payload"]["events"]) == 2, out)
    by_ts = {e["ts"]: e for e in out["payload"]["events"]}
    resend = by_ts[ev["ts"]].get("resend")
    check("waiting event carries resend annotation",
          isinstance(resend, dict), by_ts)
    check("resend.first_wait_at is the original failure time",
          resend and resend.get("first_wait_at") == now_iso(first), resend)
    from zoneinfo import ZoneInfo
    expected_jst = first.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M JST")
    check("resend.first_wait_at_jst is JST for the prefix",
          resend and resend.get("first_wait_at_jst") == expected_jst, resend)
    check("resend.prefix is the agreed wording",
          resend and resend.get("prefix") == f"遅れての再送（元の失敗時刻：{expected_jst}）", resend)
    check("new event has no resend annotation",
          "resend" not in by_ts[ev_new["ts"]], by_ts[ev_new["ts"]])
    rec = env.read_state()[eid(ev)]
    check("resend annotation does not consume attempts",
          rec.get("attempts") == 1, rec)


def test_start_grace_does_not_burn_attempts():
    # 起動猶予（2026-11-14）：最初の wake から3時間の間は、ワーカーがまだ
    # 起動していなくても試行回数を消費しない。実測の根拠：dead-letter 165件
    # 全件がイベント発生から 30〜37分で dead 化し、そのうち 116件は遅れて
    # 起動したワーカーが後に done にしていた。
    env = Env()
    ev = make_event()
    now = datetime.now(timezone.utc)
    stale = now - timedelta(seconds=TIMEOUT_SECS + 60)

    # 30分前に最初の wake：猶予内なので何もしない（silent、試行そのまま）。
    env.write_inbox([ev])
    env.write_state([{"event_id": eid(ev), "status": "processing",
                      "attempts": 1, "updated_at": now_iso(stale),
                      "first_wake_at": now_iso(now - timedelta(minutes=30)),
                      "last_error": ""}])
    out = env.run()
    rec = env.read_state()[eid(ev)]
    check("grace 30m -> silent, attempts untouched",
          out["decision"] == "silent" and rec["attempts"] == 1
          and rec["status"] == "processing", rec)
    check("grace 30m -> no dead-letter line",
          len(env.read_dead()) == 0, env.read_dead())

    # 2時間59分前でもまだ猶予内。
    env.write_state([{"event_id": eid(ev), "status": "processing",
                      "attempts": 1, "updated_at": now_iso(stale),
                      "first_wake_at": now_iso(now - timedelta(hours=2, minutes=59)),
                      "last_error": ""}])
    out2 = env.run()
    rec2 = env.read_state()[eid(ev)]
    check("grace 2h59m -> silent, attempts untouched",
          out2["decision"] == "silent" and rec2["attempts"] == 1, rec2)

    # attempts=3 でも猶予内なら dead にしない。
    env.write_state([{"event_id": eid(ev), "status": "processing",
                      "attempts": 3, "updated_at": now_iso(stale),
                      "first_wake_at": now_iso(now - timedelta(minutes=30)),
                      "last_error": ""}])
    out3 = env.run()
    rec3 = env.read_state()[eid(ev)]
    check("grace with attempts=3 -> still silent, not dead",
          out3["decision"] == "silent" and rec3["status"] == "processing"
          and len(env.read_dead()) == 0, rec3)

    # 猶予を過ぎたら（3時間01分前）通常のリトライに戻る：attempts 1 -> 2。
    env.write_state([{"event_id": eid(ev), "status": "processing",
                      "attempts": 1, "updated_at": now_iso(stale),
                      "first_wake_at": now_iso(now - timedelta(hours=3, minutes=1)),
                      "last_error": ""}])
    out4 = env.run()
    rec4 = env.read_state()[eid(ev)]
    check("past grace 3h01m -> retry, attempts 1 -> 2",
          out4["decision"] == "wake" and rec4["attempts"] == 2, rec4)

    # first_wake_at が無い古い記録は updated_at を代用する（移行期）。
    env.write_state([{"event_id": eid(ev), "status": "processing",
                      "attempts": 1, "updated_at": now_iso(stale),
                      "last_error": ""}])
    out5 = env.run()
    rec5 = env.read_state()[eid(ev)]
    check("legacy record without first_wake_at -> grace from updated_at",
          out5["decision"] == "silent" and rec5["attempts"] == 1, rec5)

    # failed には猶予は無い（ワーカーは起動して失敗を報告した）。
    env.write_state([{"event_id": eid(ev), "status": "failed",
                      "attempts": 2, "updated_at": now_iso(stale),
                      "first_wake_at": now_iso(now - timedelta(minutes=30)),
                      "last_error": "boom"}])
    out6 = env.run()
    rec6 = env.read_state()[eid(ev)]
    check("failed inside grace -> retry anyway, attempts 2 -> 3",
          out6["decision"] == "wake" and rec6["attempts"] == 3, rec6)


def test_parallel_claims_single_flight():
    import concurrent.futures
    env = Env()
    ev = make_event()
    env.write_inbox([ev])
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        outs = list(ex.map(lambda _: env.run(use_flock=True), range(10)))
    wakes = [o for o in outs if o["decision"] == "wake"]
    check("10 parallel flock-guarded polls -> exactly one wake",
          len(wakes) == 1, f"wakes={len(wakes)}")
    rec = env.read_state()[eid(ev)]
    check("parallel claims -> attempts stays 1", rec["attempts"] == 1, rec)


def test_fastpath_reclaims_processing_within_grace():
    # 高速スイープの取り戻し（2026-11-14）：SLACK_POLL_FASTPATH=1 のとき、
    # 起動猶予の内側でも processing の経過が FASTPATH_RECLAIM_SECS
    # （デフォルト120秒）を超えたイベントをスイープ側が引き取る。
    # attempts と first_wake_at は触らない（会計はフック側）。
    now = datetime.now(timezone.utc)
    seed = {"status": "processing", "attempts": 1,
            "updated_at": now_iso(now - timedelta(seconds=300)),
            "first_wake_at": now_iso(now - timedelta(minutes=30)),
            "last_error": ""}

    env = Env()
    env.env["SLACK_POLL_FASTPATH"] = "1"
    ev = make_event()
    env.write_inbox([ev])
    env.write_state([dict(seed, event_id=eid(ev))])
    out = env.run()
    check("fastpath reclaim -> wake", out["decision"] == "wake", out)
    due = out["payload"]["events"]
    check("fastpath reclaim -> event carries annotation",
          len(due) == 1 and isinstance(due[0].get("fastpath_reclaim"), dict),
          due)
    age = (due[0].get("fastpath_reclaim") or {}).get("processing_age_secs", 0)
    check("fastpath reclaim -> age is the seeded ~300s", 290 <= age <= 320, age)
    rec = env.read_state()[eid(ev)]
    check("fastpath reclaim -> attempts & first_wake untouched",
          rec["attempts"] == 1
          and rec["first_wake_at"] == seed["first_wake_at"]
          and rec["status"] == "processing", rec)
    check("fastpath reclaim -> updated_at advanced",
          rec["updated_at"] != seed["updated_at"], rec)
    out2 = env.run()
    check("fastpath reclaim -> immediate re-poll is silent",
          out2["decision"] == "silent", out2)

    # しきい値未満（60秒）は取り戻さない。
    env2 = Env()
    env2.env["SLACK_POLL_FASTPATH"] = "1"
    env2.write_inbox([ev])
    env2.write_state([dict(seed, event_id=eid(ev),
                           updated_at=now_iso(now - timedelta(seconds=60)))])
    out3 = env2.run()
    check("fastpath below threshold -> silent",
          out3["decision"] == "silent", out3)

    # 環境変数なし（フック側）では同じ種でも従来通り猶予で silent。
    env3 = Env()
    env3.write_inbox([ev])
    env3.write_state([dict(seed, event_id=eid(ev))])
    out4 = env3.run()
    check("no fastpath env -> grace still applies (silent)",
          out4["decision"] == "silent", out4)


def test_burst_cap_defers_extra_events():
    # バースト対策（2026-11-21）：1回の wake は上限件数まで。残りは状態を
    # 付けずに残し、次のポーリングで改めて新着として起こす。
    env = Env()
    env.env["SLACK_POLL_MAX_PER_WAKE"] = "4"
    evs = [make_event(ts=f"1794476892.00000{i}") for i in range(6)]
    env.write_inbox(evs)
    out = env.run()
    check("burst cap -> wake", out["decision"] == "wake", out)
    due = out["payload"]["events"]
    check("burst cap -> only 4 dispatched", len(due) == 4, due)
    check("burst cap -> queue_remaining is 2",
          out["payload"].get("queue_remaining") == 2, out["payload"])
    st = env.read_state()
    check("burst cap -> state has only the 4 dispatched",
          len(st) == 4, st)
    out2 = env.run()
    due2 = out2["payload"]["events"]
    check("burst cap -> next poll dispatches the remaining 2",
          out2["decision"] == "wake" and len(due2) == 2
          and out2["payload"].get("queue_remaining") == 0, out2)
    check("burst cap -> all 6 in state after second poll",
          len(env.read_state()) == 6, env.read_state())


def test_burst_priority_dm_and_mention_first():
    # 優先順位：DM > ねいろ宛て（本文に Neiro / ねいろ）> その他（古い順）。
    env = Env()
    env.env["SLACK_POLL_MAX_PER_WAKE"] = "3"
    dm = make_event(ts="1794476892.000002",
                    channel=make_id("D", "TESTDM0001"))
    dm["text"] = "dm question"
    mention = make_event(ts="1794476892.000001")
    mention["text"] = "Neiroさん、質問です"
    plain_old = make_event(ts="1794476892.000000")
    plain_old["text"] = "設計の話"
    plain_new = make_event(ts="1794476892.000003")
    plain_new["text"] = "設計の話2"
    env.write_inbox([plain_new, plain_old, mention, dm])
    out = env.run()
    due = out["payload"]["events"]
    order = [e["ts"] for e in due]
    check("burst priority -> DM, mention, oldest plain",
          order == [dm["ts"], mention["ts"], plain_old["ts"]], order)
    check("burst priority -> newest plain left queued",
          out["payload"].get("queue_remaining") == 1, out["payload"])


def test_burst_duplicate_lines_dispatched_once():
    # 同一ポーリングで inbox に同じイベントが2行あっても、候補は1件だけ
    # （状態変更の遅延適用で二重に枠を消費しない。agy レビュー指摘）。
    env = Env()
    ev = make_event()
    env.write_inbox([ev, dict(ev)])
    out = env.run()
    due = out["payload"]["events"]
    check("duplicate lines -> dispatched once",
          out["decision"] == "wake" and len(due) == 1, out)
    check("duplicate lines -> one state record",
          len(env.read_state()) == 1, env.read_state())


def test_burst_priority_bot_mention():
    # Slack 形式のメンション <@ボットID> も ねいろ宛てとして優先する。
    # ids.json は SLACK_POLLER_IDS_FILE で差し替える（合成IDで検証）。
    env = Env()
    env.env["SLACK_POLL_MAX_PER_WAKE"] = "1"
    bot = make_id("U", "TESTBOT001")
    ids_path = os.path.join(env.d.name, "ids.json")
    with open(ids_path, "w", encoding="utf-8") as f:
        json.dump({"bot_neiro_test": bot}, f)
    env.env["SLACK_POLLER_IDS_FILE"] = ids_path
    plain = make_event(ts="1794476892.000000")
    plain["text"] = "設計の話"
    mention = make_event(ts="1794476892.000009")
    mention["text"] = f"<@{bot}> 質問です"
    env.write_inbox([plain, mention])
    out = env.run()
    due = out["payload"]["events"]
    check("bot mention -> dispatched before older plain post",
          len(due) == 1 and due[0]["ts"] == mention["ts"], due)


def test_burst_priority_aging_promotes_old_plain():
    # エージング（2026-11-21）：「その他」は待ち時間で段階的に昇格する。
    # 5分を超えたらねいろ宛て相当（優先度1）になり、同順位では古い順
    # なので、新しい ねいろ宛てより先に起こされる。
    env = Env()
    env.env["SLACK_POLL_MAX_PER_WAKE"] = "2"
    now = datetime.now(timezone.utc)
    plain_old = make_event(ts="1794476892.000000")
    plain_old["text"] = "設計の話"
    plain_old["received_at"] = now_iso(now - timedelta(minutes=6))
    mention = make_event(ts="1794476892.000001")
    mention["text"] = "Neiroさん、質問です"
    mention["received_at"] = now_iso(now)
    plain_new = make_event(ts="1794476892.000002")
    plain_new["text"] = "設計の話2"
    plain_new["received_at"] = now_iso(now)
    env.write_inbox([plain_new, mention, plain_old])
    out = env.run()
    due = out["payload"]["events"]
    order = [e["ts"] for e in due]
    check("aging -> 6min old plain promoted above newer mention",
          order == [plain_old["ts"], mention["ts"]], order)
    check("aging -> newest plain left queued",
          out["payload"].get("queue_remaining") == 1, out["payload"])


def test_burst_priority_aging_top_after_15min():
    # 15分を超えた「その他」は最上位（DM相当）になり、DMより ts が
    # 古ければ DM より先に起こされる。5分未満のものは昇格しない。
    env = Env()
    env.env["SLACK_POLL_MAX_PER_WAKE"] = "2"
    now = datetime.now(timezone.utc)
    plain_old = make_event(ts="1794476892.000000")
    plain_old["text"] = "設計の話"
    plain_old["received_at"] = now_iso(now - timedelta(minutes=16))
    dm = make_event(ts="1794476892.000001",
                    channel=make_id("D", "TESTDM0001"))
    dm["text"] = "dm question"
    dm["received_at"] = now_iso(now)
    plain_young = make_event(ts="1794476892.000002")
    plain_young["text"] = "設計の話2"
    plain_young["received_at"] = now_iso(now - timedelta(minutes=4))
    env.write_inbox([plain_young, dm, plain_old])
    out = env.run()
    due = out["payload"]["events"]
    order = [e["ts"] for e in due]
    check("aging -> 16min old plain promoted to top, before DM",
          order == [plain_old["ts"], dm["ts"]], order)
    check("aging -> 4min old plain not promoted, left queued",
          out["payload"].get("queue_remaining") == 1, out["payload"])


def test_burst_priority_aging_naive_received_at():
    # タイムゾーンなしの received_at でも落ちず、UTC として扱って昇格する
    # （agy レビュー指摘、2026-11-21）。
    env = Env()
    env.env["SLACK_POLL_MAX_PER_WAKE"] = "1"
    now = datetime.now(timezone.utc)
    plain_old = make_event(ts="1794476892.000000")
    plain_old["text"] = "設計の話"
    plain_old["received_at"] = (now - timedelta(minutes=6)).replace(
        tzinfo=None).isoformat()
    plain_new = make_event(ts="1794476892.000001")
    plain_new["text"] = "Neiroさん、質問です"
    plain_new["received_at"] = now_iso(now)
    env.write_inbox([plain_new, plain_old])
    out = env.run()
    due = out["payload"]["events"]
    check("aging naive received_at -> promoted, no crash",
          len(due) == 1 and due[0]["ts"] == plain_old["ts"], due)


def test_fastpath_reclaim_keeps_resend_note():
    # 中-2（2026-11-22）：waiting を経た再送が処理中に死んで fastpath
    # 取り戻しに回ったときも、resend 注記を付け直す。付けないと再送文の
    # 先頭から「遅れての再送（元の失敗時刻：…）」が落ちる。
    # waiting を経ていない取り戻しには resend は付かない。
    from zoneinfo import ZoneInfo
    now = datetime.now(timezone.utc)
    first = now - timedelta(hours=1)
    expected_jst = first.astimezone(ZoneInfo("Asia/Tokyo")).strftime(
        "%Y-%m-%d %H:%M JST")

    env = Env()
    env.env["SLACK_POLL_FASTPATH"] = "1"
    ev = make_event()
    env.write_inbox([ev])
    env.write_state([{"event_id": eid(ev), "status": "processing",
                      "attempts": 1,
                      "updated_at": now_iso(now - timedelta(seconds=300)),
                      "first_wake_at": now_iso(now - timedelta(minutes=30)),
                      "first_wait_at": now_iso(first),
                      "last_error": ""}])
    out = env.run()
    due = out["payload"]["events"]
    check("reclaim of a resend -> wake", out["decision"] == "wake", out)
    resend = due[0].get("resend") if due else None
    check("reclaim of a resend -> resend annotation kept",
          isinstance(resend, dict), due)
    check("reclaim resend.first_wait_at is the original failure time",
          resend and resend.get("first_wait_at") == now_iso(first), resend)
    check("reclaim resend.prefix is the agreed wording",
          resend and resend.get("prefix")
          == f"遅れての再送（元の失敗時刻：{expected_jst}）", resend)
    check("reclaim keeps fastpath_reclaim annotation too",
          isinstance(due[0].get("fastpath_reclaim"), dict), due)

    # waiting を経ていない取り戻しには resend は付かない。
    env2 = Env()
    env2.env["SLACK_POLL_FASTPATH"] = "1"
    env2.write_inbox([ev])
    env2.write_state([{"event_id": eid(ev), "status": "processing",
                       "attempts": 1,
                       "updated_at": now_iso(now - timedelta(seconds=300)),
                       "first_wake_at": now_iso(now - timedelta(minutes=30)),
                       "last_error": ""}])
    out2 = env2.run()
    due2 = out2["payload"]["events"]
    check("reclaim without waiting history -> no resend",
          len(due2) == 1 and "resend" not in due2[0], due2)


def test_stale_retry_keeps_resend_note():
    # 中-2（2026-11-22）：waiting を経た再送が stale リトライに回った
    # ときも resend 注記を付け直す（fastpath 取り戻しと共通の注記）。
    # waiting を経ていない stale リトライには resend は付かない。
    from zoneinfo import ZoneInfo
    now = datetime.now(timezone.utc)
    first = now - timedelta(hours=1)
    expected_jst = first.astimezone(ZoneInfo("Asia/Tokyo")).strftime(
        "%Y-%m-%d %H:%M JST")
    old = now - timedelta(seconds=TIMEOUT_SECS + 60)
    first_wake = now - timedelta(hours=4)

    env = Env()
    ev = make_event()
    env.write_inbox([ev])
    env.write_state([{"event_id": eid(ev), "status": "processing",
                      "attempts": 1, "updated_at": now_iso(old),
                      "first_wake_at": now_iso(first_wake),
                      "first_wait_at": now_iso(first),
                      "last_error": ""}])
    out = env.run()
    due = out["payload"]["events"]
    resend = due[0].get("resend") if due else None
    check("stale retry of a resend -> resend annotation kept",
          isinstance(resend, dict), due)
    check("stale retry resend.prefix is the agreed wording",
          resend and resend.get("prefix")
          == f"遅れての再送（元の失敗時刻：{expected_jst}）", resend)

    env2 = Env()
    env2.write_inbox([ev])
    env2.write_state([{"event_id": eid(ev), "status": "processing",
                       "attempts": 1, "updated_at": now_iso(old),
                       "first_wake_at": now_iso(first_wake),
                       "last_error": ""}])
    out2 = env2.run()
    due2 = out2["payload"]["events"]
    check("stale retry without waiting history -> no resend",
          len(due2) == 1 and "resend" not in due2[0], due2)


def _secs_until(iso_ts):
    return (datetime.fromisoformat(iso_ts)
            - datetime.now(timezone.utc)).total_seconds()


def test_waiting_staged_intervals():
    # waiting 段階化（2026-11-25、Yama方式）：再 wake 間隔は 2分→5分→
    # 15分→30分→60分と広がり、起こし直しの回数は wait_wakes に数える。
    # 窓（24時間）と first_wait_at（元の失敗時刻）は従来通り不変。
    env = Env()
    ev = make_event()
    now = datetime.now(timezone.utc)
    first = now - timedelta(hours=1)

    # 初めて waiting を見たとき：窓の起点と第1段（2分）の次回 wake を
    # 刻むだけで、その場では起こさない。
    env.write_inbox([ev])
    env.write_state([{"event_id": eid(ev), "status": "waiting", "attempts": 1,
                      "updated_at": now_iso(first),
                      "last_error": "pre-check unverifiable"}])
    out = env.run()
    check("waiting first sight -> silent (stage 1 scheduled)",
          out["decision"] == "silent", out)
    rec = env.read_state()[eid(ev)]
    check("waiting first sight -> first_wait_at is the original failure",
          rec.get("first_wait_at") == now_iso(first), rec)
    check("waiting first sight -> wait_wakes 0", rec.get("wait_wakes") == 0, rec)
    d1 = _secs_until(rec["next_wake_at"])
    check("waiting first sight -> next wake in ~120s", 100 <= d1 <= 140, d1)

    # 第1段を過ぎた：起こし直され、wait_wakes=1、次回は第2段（5分）。
    rec["next_wake_at"] = now_iso(now - timedelta(seconds=5))
    env.write_state([rec])
    out2 = env.run()
    check("waiting stage 1 due -> re-woken",
          out2["decision"] == "wake" and len(out2["payload"]["events"]) == 1,
          out2)
    rec2 = env.read_state()[eid(ev)]
    check("waiting stage 1 -> wait_wakes 1", rec2.get("wait_wakes") == 1, rec2)
    check("waiting stage 1 -> attempts still untouched",
          rec2.get("attempts") == 1, rec2)
    d2 = _secs_until(rec2["next_wake_at"])
    check("waiting stage 1 -> next wake in ~300s", 270 <= d2 <= 330, d2)

    # 段が進むと間隔が広がる：wait_wakes=3 の起こし直しで次は最終段60分。
    rec2["status"] = "waiting"
    rec2["wait_wakes"] = 3
    rec2["next_wake_at"] = now_iso(now - timedelta(seconds=5))
    env.write_state([rec2])
    out3 = env.run()
    check("waiting stage 4 due -> re-woken", out3["decision"] == "wake", out3)
    rec3 = env.read_state()[eid(ev)]
    check("waiting stage 4 -> wait_wakes 4", rec3.get("wait_wakes") == 4, rec3)
    d3 = _secs_until(rec3["next_wake_at"])
    check("waiting stage 4 -> next wake in ~3600s (final stage)",
          3400 <= d3 <= 3700, d3)


def test_reclaim_cutoff_and_backoff():
    # 取り戻しの打ち切り（2026-11-25、Yama方式）：first_wake から3時間を
    # 過ぎたものと、回数上限（10回）に達したものは取り戻さない。間隔は
    # reclaim_count で段階的に広がる（バックオフ）。打ち切り側では
    # カウンタは進まない（消費しない）。
    now = datetime.now(timezone.utc)
    ev = make_event()
    base = {"status": "processing", "attempts": 1, "last_error": ""}

    # 窓切れ：first_wake が3時間1分前。processing 経過が十分でも
    # 取り戻さない。
    env = Env()
    env.env["SLACK_POLL_FASTPATH"] = "1"
    env.write_inbox([ev])
    env.write_state([dict(base, event_id=eid(ev),
                          updated_at=now_iso(now - timedelta(seconds=2000)),
                          first_wake_at=now_iso(now - timedelta(hours=3,
                                                                minutes=1)))])
    out = env.run()
    check("reclaim past the 3h window -> silent",
          out["decision"] == "silent", out)
    rec = env.read_state()[eid(ev)]
    check("reclaim past the window -> count not consumed",
          not rec.get("reclaim_count"), rec)

    # 回数上限：reclaim_count=10（上限）では取り戻さない。
    env2 = Env()
    env2.env["SLACK_POLL_FASTPATH"] = "1"
    env2.write_inbox([ev])
    env2.write_state([dict(base, event_id=eid(ev), reclaim_count=10,
                           updated_at=now_iso(now - timedelta(seconds=2000)),
                           first_wake_at=now_iso(now - timedelta(hours=1)))])
    out2 = env2.run()
    check("reclaim at the count cap -> silent",
          out2["decision"] == "silent", out2)

    # バックオフ：reclaim_count=1 のしきい値は300秒。経過200秒では
    # 取り戻さず、400秒では取り戻してカウンタが 2 になる。
    env3 = Env()
    env3.env["SLACK_POLL_FASTPATH"] = "1"
    env3.write_inbox([ev])
    seed3 = dict(base, event_id=eid(ev), reclaim_count=1,
                 first_wake_at=now_iso(now - timedelta(hours=1)))
    env3.write_state([dict(seed3,
                           updated_at=now_iso(now - timedelta(seconds=200)))])
    out3 = env3.run()
    check("reclaim backoff stage 1 at 200s -> silent",
          out3["decision"] == "silent", out3)
    env3.write_state([dict(seed3,
                           updated_at=now_iso(now - timedelta(seconds=400)))])
    out4 = env3.run()
    check("reclaim backoff stage 1 at 400s -> wake",
          out4["decision"] == "wake", out4)
    rec4 = env3.read_state()[eid(ev)]
    check("reclaim increments reclaim_count to 2",
          rec4.get("reclaim_count") == 2, rec4)
    due4 = out4["payload"]["events"]
    check("reclaim annotation carries the new count",
          (due4[0].get("fastpath_reclaim") or {}).get("reclaim_count") == 2,
          due4)


def test_wake_truncates_long_event_text():
    # 読み取り上限（2026-11-22）：3,000字超の投稿は wake ペイロードで
    # 切られ、text_truncated に元の文字数が残る。短い投稿はそのまま。
    env = Env()
    long_ev = make_event(ts="1794476892.695469")
    long_ev["text"] = "あ" * 3500
    short_ev = make_event(ts="1794476893.695469")
    short_ev["text"] = "短い投稿"
    env.write_inbox([long_ev, short_ev])
    out = env.run()
    due = out["payload"]["events"]
    check("long event -> wake with both events",
          out["decision"] == "wake" and len(due) == 2, out)
    cut = due[0]
    check("long event text cut to 3000 chars",
          len(cut.get("text") or "") == 3000, len(cut.get("text") or ""))
    check("long event text_truncated carries original length",
          cut.get("text_truncated") == {"original_chars": 3500,
                                        "kept_chars": 3000},
          cut.get("text_truncated"))
    kept = due[1]
    check("short event text untouched, no annotation",
          kept.get("text") == "短い投稿"
          and "text_truncated" not in kept, kept)


def test_wake_total_text_budget_trims_oldest():
    # 合計上限（2026-11-22）：1回あたりの合計を超えたら古いイベントの
    # 本文から落とし、新しい方を長く残す。イベント自体は落とさない。
    env = Env()
    env.env["SLACK_POLL_MAX_EVENT_TEXT_CHARS"] = "10000"
    env.env["SLACK_POLL_MAX_WAKE_TEXT_CHARS"] = "1200"
    old_ev = make_event(ts="1794476892.695469")
    old_ev["text"] = "あ" * 1000
    new_ev = make_event(ts="1794476893.695469")
    new_ev["text"] = "い" * 1000
    env.write_inbox([old_ev, new_ev])
    out = env.run()
    due = out["payload"]["events"]
    check("budget overrun -> both events still dispatched",
          len(due) == 2, due)
    check("oldest event trimmed to the floor first",
          len(due[0].get("text") or "") == 500
          and due[0].get("text_truncated") == {"original_chars": 1000,
                                               "kept_chars": 500},
          due[0].get("text_truncated"))
    check("newest event trimmed only as far as needed",
          len(due[1].get("text") or "") == 700
          and due[1].get("text_truncated") == {"original_chars": 1000,
                                               "kept_chars": 700},
          due[1].get("text_truncated"))
    check("wake total fits the budget",
          sum(len(e.get("text") or "") for e in due) <= 1200, due)


def main():
    global FAIL
    tests = [test_new_event_wakes_once,
             test_stale_processing_rewakes_once,
             test_stale_at_max_attempts_dead,
             test_failed_retries_then_dead,
             test_done_never_rewoken,
             test_malformed_lines_skipped,
             test_skipped_lines_counted_snapshot,
             test_waiting_boundary_24h,
             test_waiting_rewake_annotates_resend,
             test_start_grace_does_not_burn_attempts,
             test_fastpath_reclaims_processing_within_grace,
             test_burst_cap_defers_extra_events,
             test_burst_priority_dm_and_mention_first,
             test_burst_duplicate_lines_dispatched_once,
             test_burst_priority_bot_mention,
             test_burst_priority_aging_promotes_old_plain,
             test_burst_priority_aging_top_after_15min,
             test_burst_priority_aging_naive_received_at,
             test_fastpath_reclaim_keeps_resend_note,
             test_stale_retry_keeps_resend_note,
             test_waiting_staged_intervals,
             test_reclaim_cutoff_and_backoff,
             test_wake_truncates_long_event_text,
             test_wake_total_text_budget_trims_oldest,
             test_parallel_claims_single_flight]
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
