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
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

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


def make_event(ts="1794476892.695469", channel="C0CGYGT9V7G",
               workspace="T0CGV6V7161"):
    return {"workspace_id": workspace, "channel": channel, "ts": ts,
            "user": "U0CH3MQH7PT", "text": "test"}


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
