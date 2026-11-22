#!/usr/bin/env python3
"""Tests for hooks/scripts/slack-sync-check-run.sh.

No real Slack calls: the runner is copied into a temporary directory
with stub slack-ids.sh / slack-send-locked.sh, and the two sync checks
are stubbed with scripts the test controls. Run with:
python3 test_slack_sync_check_run.py
"""
import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import unittest

RUNNER_PATH = os.path.expanduser(
    "~/workspace/neiro-slack-system/hooks/scripts/slack-sync-check-run.sh")

GRACE_SECS = 900


def write_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, 0o755)


class SyncCheckRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.runner = os.path.join(self.dir, "slack-sync-check-run.sh")
        shutil.copy(RUNNER_PATH, self.runner)
        # Stub slack-ids.sh: the runner sources it for ALERT_CHANNEL.
        write_file(os.path.join(self.dir, "slack-ids.sh"),
                   "slack_id() { echo 'C_TESTCHAN01'; }\n")
        # Stub slack-send-locked.sh: log invocations, succeed unless
        # SEND_FAIL=1 in the environment.
        write_file(os.path.join(self.dir, "slack-send-locked.sh"),
                   "#!/usr/bin/env bash\n"
                   "echo \"send $*\" >> \"$SEND_LOG\"\n"
                   "if [ \"${SEND_FAIL:-0}\" = \"1\" ]; then exit 1; fi\n"
                   "exit 0\n")
        self.send_log = os.path.join(self.dir, "send.log")
        # Fake check scripts: each cats a control file and exits 1,
        # or exits 0 when the control file says "ok".
        self.hook_out = os.path.join(self.dir, "hook.out")
        self.listener_out = os.path.join(self.dir, "listener.out")
        self.hook_check = os.path.join(self.dir, "hook-check.sh")
        self.listener_check = os.path.join(self.dir, "listener-check.sh")
        for check, out in ((self.hook_check, self.hook_out),
                           (self.listener_check, self.listener_out)):
            write_file(check,
                       "#!/usr/bin/env bash\n"
                       "if [ \"$(cat '%s')\" = \"ok\" ]; then exit 0; fi\n"
                       "cat '%s'\n"
                       "exit 1\n" % (out, out))
        self.hook_state = os.path.join(self.dir, "hook.state")
        self.listener_state = os.path.join(self.dir, "listener.state")
        self.set_check_output("hook", "drift line 1\ndrift line 2\n")
        self.set_check_output("listener", "ok")

    def tearDown(self):
        self.tmp.cleanup()

    def set_check_output(self, which, content):
        write_file(self.hook_out if which == "hook"
                   else self.listener_out, content)

    def run_runner(self, extra_env=None):
        env = dict(os.environ,
                   SEND_LOG=self.send_log,
                   SYNC_CHECK_RUN_HOOK_CHECK=self.hook_check,
                   SYNC_CHECK_RUN_HOOK_STATE=self.hook_state,
                   SYNC_CHECK_RUN_LISTENER_CHECK=self.listener_check,
                   SYNC_CHECK_RUN_LISTENER_STATE=self.listener_state)
        if extra_env:
            env.update(extra_env)
        return subprocess.run(["bash", self.runner], env=env,
                              capture_output=True, text=True)

    def send_count(self):
        if not os.path.exists(self.send_log):
            return 0
        with open(self.send_log, encoding="utf-8") as f:
            return len([line for line in f if line.startswith("send ")])

    def read_state(self, path):
        state = {}
        with open(path, encoding="utf-8") as f:
            for line in f:
                key, _, val = line.strip().partition("=")
                state[key] = val
        return state

    def write_state(self, path, sig, first_seen, warned, drift_since=None):
        if drift_since is None:
            drift_since = first_seen
        write_file(path, "sig=%s\nfirst_seen=%s\nwarned=%s\ndrift_since=%s\n"
                   % (sig, first_seen, warned, drift_since))

    def sig_of(self, content):
        # The runner captures check output with $(...), which strips
        # trailing newlines, so the signature is over the stripped text.
        return hashlib.md5(
            content.rstrip("\n").encode("utf-8")).hexdigest()

    # 1. 両方正常なら無言で exit 0、状態ファイルも送出もなし
    def test_passing_checks_silent(self):
        self.set_check_output("hook", "ok")
        result = self.run_runner()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.send_count(), 0)
        self.assertFalse(os.path.exists(self.hook_state))
        self.assertFalse(os.path.exists(self.listener_state))

    # 2. 初回検出は黙って状態だけ記録する（drift_since もここから始まる）
    def test_first_detection_silent_records_state(self):
        before = int(time.time())
        result = self.run_runner()
        after = int(time.time())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.send_count(), 0)
        state = self.read_state(self.hook_state)
        self.assertEqual(state["sig"],
                         self.sig_of("drift line 1\ndrift line 2\n"))
        self.assertTrue(before <= int(state["first_seen"]) <= after)
        self.assertEqual(state["warned"], "0")
        self.assertTrue(before <= int(state["drift_since"]) <= after)
        self.assertFalse(os.path.exists(self.listener_state))

    # 3. 猶予内（15分未満）は同じずれでも黙ったまま（drift_since も保持）
    def test_grace_not_elapsed_stays_silent(self):
        self.run_runner()
        first_seen = self.read_state(self.hook_state)["first_seen"]
        drift_since = self.read_state(self.hook_state)["drift_since"]
        result = self.run_runner()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.send_count(), 0)
        state = self.read_state(self.hook_state)
        self.assertEqual(state["first_seen"], first_seen)
        self.assertEqual(state["drift_since"], drift_since)
        self.assertEqual(state["warned"], "0")

    # 4. 猶予を過ぎたら警告を1回送り、warned を立てる
    def test_grace_elapsed_warns_once(self):
        sig = self.sig_of("drift line 1\ndrift line 2\n")
        old = int(time.time()) - GRACE_SECS - 60
        self.write_state(self.hook_state, sig, old, 0)
        result = self.run_runner()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.send_count(), 1)
        state = self.read_state(self.hook_state)
        self.assertEqual(state["first_seen"], str(old))
        self.assertEqual(state["drift_since"], str(old))
        self.assertEqual(state["warned"], "1")
        with open(self.send_log, encoding="utf-8") as f:
            logged = f.read()
        self.assertIn("hook-sync-" + sig, logged)
        self.assertIn("フックの単一正本チェックで異常を検出しました", logged)

    # 5. 警告済みのエピソードでは再送しない
    def test_warned_episode_stays_silent(self):
        sig = self.sig_of("drift line 1\ndrift line 2\n")
        old = int(time.time()) - GRACE_SECS - 60
        self.write_state(self.hook_state, sig, old, 1)
        result = self.run_runner()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.send_count(), 0)

    # 6. 署名が変われば first_seen の時計をリセットして黙る。
    # warned と drift_since はエピソード単位で引き継ぐ（「続いている間は
    # 1回」を守るため、警告済みのエピソードでは再警告しない）。
    def test_signature_change_resets_clock_warned_stays(self):
        sig = self.sig_of("drift line 1\ndrift line 2\n")
        old = int(time.time()) - GRACE_SECS - 60
        drift = int(time.time()) - 2 * GRACE_SECS - 60
        self.write_state(self.hook_state, sig, old, 1, drift)
        self.set_check_output("hook", "different drift\n")
        before = int(time.time())
        result = self.run_runner()
        after = int(time.time())
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.send_count(), 0)
        state = self.read_state(self.hook_state)
        self.assertEqual(state["sig"], self.sig_of("different drift\n"))
        self.assertTrue(before <= int(state["first_seen"]) <= after)
        self.assertEqual(state["warned"], "1")
        self.assertEqual(state["drift_since"], str(drift))

    # 6b. 未警告のエピソードで署名が変われば、猶予内で黙って記録する
    # （drift_since は引き継ぐ）
    def test_signature_change_unwarned_keeps_drift_since(self):
        sig = self.sig_of("drift line 1\ndrift line 2\n")
        now = int(time.time())
        self.write_state(self.hook_state, sig, now, 0, now)
        self.set_check_output("hook", "different drift\n")
        result = self.run_runner()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.send_count(), 0)
        state = self.read_state(self.hook_state)
        self.assertEqual(state["sig"], self.sig_of("different drift\n"))
        self.assertEqual(state["warned"], "0")
        self.assertEqual(state["drift_since"], str(now))

    # 7. ずれが解消されれば状態ごとクリア
    def test_resolved_drift_clears_state(self):
        sig = self.sig_of("drift line 1\ndrift line 2\n")
        self.write_state(self.hook_state, sig, int(time.time()), 1)
        self.set_check_output("hook", "ok")
        result = self.run_runner()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.send_count(), 0)
        self.assertFalse(os.path.exists(self.hook_state))

    # 8. 送信に失敗したときは warned を立てず、次回は猶予を待たず再送する
    def test_send_failure_retries_without_waiting(self):
        sig = self.sig_of("drift line 1\ndrift line 2\n")
        old = int(time.time()) - GRACE_SECS - 60
        self.write_state(self.hook_state, sig, old, 0)
        result = self.run_runner({"SEND_FAIL": "1"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.send_count(), 1)
        self.assertEqual(self.read_state(self.hook_state)["warned"], "0")
        result = self.run_runner({"SEND_FAIL": "1"})
        self.assertEqual(self.send_count(), 2)

    # 9. 旧形式の状態ファイル（署名だけ1行）は警告済みとして読み替える
    def test_old_format_state_treated_as_warned(self):
        sig = self.sig_of("drift line 1\ndrift line 2\n")
        write_file(self.hook_state, sig + "\n")
        result = self.run_runner()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.send_count(), 0)

    # 10. 旧形式でも署名が違えば新しいエピソードとして黙って記録する
    def test_old_format_different_sig_is_new_episode(self):
        write_file(self.hook_state, "0" * 32 + "\n")
        result = self.run_runner()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.send_count(), 0)
        state = self.read_state(self.hook_state)
        self.assertEqual(state["sig"],
                         self.sig_of("drift line 1\ndrift line 2\n"))
        self.assertEqual(state["warned"], "0")

    # 11. 壊れた first_seen は初回検出として時計をリセットする
    def test_corrupt_first_seen_resets_clock(self):
        sig = self.sig_of("drift line 1\ndrift line 2\n")
        write_file(self.hook_state,
                   "sig=%s\nfirst_seen=not-a-number\nwarned=0\n" % sig)
        result = self.run_runner()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.send_count(), 0)
        state = self.read_state(self.hook_state)
        self.assertEqual(state["warned"], "0")

    # 12. リスナー側も同じ猶予ルールで警告する
    def test_listener_side_grace(self):
        self.set_check_output("hook", "ok")
        self.set_check_output("listener", "listener drift\n")
        result = self.run_runner()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.send_count(), 0)
        sig = self.sig_of("listener drift\n")
        old = int(time.time()) - GRACE_SECS - 60
        self.write_state(self.listener_state, sig, old, 0)
        result = self.run_runner()
        self.assertEqual(self.send_count(), 1)
        with open(self.send_log, encoding="utf-8") as f:
            logged = f.read()
        self.assertIn("listener-sync-" + sig, logged)
        self.assertIn("リスナーの同期チェックで異常を検出しました", logged)

    # 13. 猶予秒数は環境変数で上書きできる
    def test_grace_secs_env_override(self):
        self.run_runner({"SYNC_CHECK_RUN_GRACE_SECS": "0"})
        self.assertEqual(self.send_count(), 0)
        result = self.run_runner({"SYNC_CHECK_RUN_GRACE_SECS": "0"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.send_count(), 1)

    # 14. ずれ全体が上限（30分）を越えて署名が変われば、その場で警告する
    def test_cap_exceeded_new_signature_warns_immediately(self):
        sig = self.sig_of("drift line 1\ndrift line 2\n")
        now = int(time.time())
        drift = now - 2 * GRACE_SECS - 60
        self.write_state(self.hook_state, sig, now, 0, drift)
        self.set_check_output("hook", "different drift\n")
        result = self.run_runner()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.send_count(), 1)
        state = self.read_state(self.hook_state)
        self.assertEqual(state["sig"], self.sig_of("different drift\n"))
        self.assertEqual(state["warned"], "1")
        self.assertEqual(state["drift_since"], str(drift))

    # 15. 上限を越えた慢性的なずれでも、警告済みのエピソードでは
    # 署名が変わっても再警告しない（「続いている間は1回」）
    def test_cap_exceeded_warned_episode_stays_silent(self):
        sig = self.sig_of("drift line 1\ndrift line 2\n")
        now = int(time.time())
        drift = now - 2 * GRACE_SECS - 60
        self.write_state(self.hook_state, sig, now, 1, drift)
        self.set_check_output("hook", "different drift\n")
        result = self.run_runner()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.send_count(), 0)
        state = self.read_state(self.hook_state)
        self.assertEqual(state["warned"], "1")
        self.assertEqual(state["drift_since"], str(drift))

    # 16. 署名が入れ替わり続ける慢性的なずれでも、警告は1回だけ
    def test_chronic_drift_warns_only_once(self):
        now = int(time.time())
        drift = now - 2 * GRACE_SECS - 60
        sig = self.sig_of("drift line 1\ndrift line 2\n")
        self.write_state(self.hook_state, sig, now, 0, drift)
        self.set_check_output("hook", "drift A\n")
        self.run_runner()
        self.assertEqual(self.send_count(), 1)
        # さらに署名が変わっても、同じエピソードでは警告しない
        self.set_check_output("hook", "drift B\n")
        self.run_runner()
        self.assertEqual(self.send_count(), 1)
        self.set_check_output("hook", "drift C\n")
        self.run_runner()
        self.assertEqual(self.send_count(), 1)
        state = self.read_state(self.hook_state)
        self.assertEqual(state["drift_since"], str(drift))
        self.assertEqual(state["warned"], "1")

    # 17. drift_since のない旧形式（3行）は first_seen を引き継ぐ
    def test_old_three_line_state_migrates_drift_since(self):
        sig = self.sig_of("drift line 1\ndrift line 2\n")
        old = int(time.time()) - GRACE_SECS - 60
        write_file(self.hook_state,
                   "sig=%s\nfirst_seen=%s\nwarned=0\n" % (sig, old))
        result = self.run_runner()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.send_count(), 1)
        state = self.read_state(self.hook_state)
        self.assertEqual(state["drift_since"], str(old))

    # 18. 壊れた drift_since は first_seen として扱う（即座の上限超過にしない）
    def test_corrupt_drift_since_falls_back_to_first_seen(self):
        sig = self.sig_of("drift line 1\ndrift line 2\n")
        now = int(time.time())
        write_file(self.hook_state,
                   "sig=%s\nfirst_seen=%s\nwarned=0\ndrift_since=xxx\n"
                   % (sig, now))
        result = self.run_runner()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.send_count(), 0)
        state = self.read_state(self.hook_state)
        self.assertEqual(state["drift_since"], str(now))

    # 19. 未来の drift_since は now に丸める（即座の上限超過にしない）
    def test_future_drift_since_clamped_to_now(self):
        sig = self.sig_of("drift line 1\ndrift line 2\n")
        now = int(time.time())
        future = now + 3600
        self.write_state(self.hook_state, sig, now, 0, future)
        result = self.run_runner()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.send_count(), 0)
        state = self.read_state(self.hook_state)
        self.assertTrue(now <= int(state["drift_since"]) <= now + 5)

    # 20. 新規エピソードは猶予0でも初回は警告しない
    def test_new_episode_never_warns_immediately(self):
        result = self.run_runner({"SYNC_CHECK_RUN_GRACE_SECS": "0"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.send_count(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
