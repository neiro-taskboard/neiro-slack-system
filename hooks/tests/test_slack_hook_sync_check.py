#!/usr/bin/env python3
"""Tests for hooks/scripts/slack-hook-sync-check.sh.

No real Slack calls: only temporary directories and the check script
itself are exercised. Run with: python3 test_slack_hook_sync_check.py
"""
import os
import subprocess
import tempfile
import unittest

CHECK_PATH = os.path.expanduser(
    "~/workspace/neiro-slack-system/hooks/scripts/slack-hook-sync-check.sh")


def run_check(live_dir, repo_dir):
    env = dict(os.environ, HOOKS_DIR=live_dir,
               REPO_HOOKS_DIR=repo_dir)
    return subprocess.run(["bash", CHECK_PATH], env=env,
                          capture_output=True, text=True)


class HookSyncCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.live = os.path.join(self.tmp.name, "live")
        self.repo = os.path.join(self.tmp.name, "repo")
        for d in ("scripts", "definitions", "tests"):
            os.makedirs(os.path.join(self.repo, d))
            os.makedirs(os.path.join(self.live, d))

    def tearDown(self):
        self.tmp.cleanup()

    def write_repo(self, rel, content):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def link_live(self, rel):
        src = os.path.join(self.repo, rel)
        dst = os.path.join(self.live, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.symlink(src, dst)

    def write_live_real(self, rel, content):
        path = os.path.join(self.live, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    # 1. リンクが正しければ exit 0 で無言
    def test_matching_symlinks_pass_silently(self):
        self.write_repo("scripts/slack-poll.py", "print('ok')\n")
        self.write_repo("definitions/slack-new-messages.json", "{}\n")
        self.write_repo("tests/test_x.py", "print('ok')\n")
        for rel in ("scripts/slack-poll.py",
                    "definitions/slack-new-messages.json",
                    "tests/test_x.py"):
            self.link_live(rel)
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    # 2. 内容が同一の通常ファイルは許容する（2026-11-24、フック実行側の
    # 実パス制約を取り込み。Yamaさん・Yanayanaさん側と同じ構成）
    def test_regular_file_identical_content_passes(self):
        self.write_repo("scripts/slack-poll.py", "print('ok')\n")
        self.write_live_real("scripts/slack-poll.py", "print('ok')\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    # 2b. 内容が違う通常ファイルは CONTENT-DRIFT で exit 1
    def test_regular_file_drift_detected(self):
        self.write_repo("scripts/slack-poll.py", "print('ok')\n")
        self.write_live_real("scripts/slack-poll.py", "print('drift')\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("CONTENT-DRIFT", r.stdout)

    # 3. 稼働中のファイルが無ければ MISSING で exit 1
    def test_missing_live_file(self):
        self.write_repo("scripts/slack-poll.py", "print('ok')\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("MISSING", r.stdout)
        self.assertIn("slack-poll.py", r.stdout)

    # 4. リンク切れは BROKEN-LINK で exit 1
    def test_broken_link(self):
        self.write_repo("scripts/slack-poll.py", "print('ok')\n")
        os.symlink(os.path.join(self.repo, "scripts", "gone.py"),
                   os.path.join(self.live, "scripts", "slack-poll.py"))
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("BROKEN-LINK", r.stdout)

    # 5. 違う先を指すリンクは WRONG-TARGET で exit 1
    def test_wrong_target(self):
        self.write_repo("scripts/slack-poll.py", "print('ok')\n")
        other = os.path.join(self.tmp.name, "other.py")
        with open(other, "w", encoding="utf-8") as f:
            f.write("print('other')\n")
        os.symlink(other,
                   os.path.join(self.live, "scripts", "slack-poll.py"))
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("WRONG-TARGET", r.stdout)

    # 6. 構文が壊れていれば SYNTAX-ERROR で exit 1
    def test_syntax_error_detected(self):
        self.write_repo("scripts/slack-poll.py", "def broken(:\n")
        self.link_live("scripts/slack-poll.py")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("SYNTAX-ERROR", r.stdout)

    # 7. JSON が壊れていれば INVALID-JSON で exit 1
    def test_invalid_json_detected(self):
        self.write_repo("definitions/slack-new-messages.json", "{bad\n")
        self.link_live("definitions/slack-new-messages.json")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("INVALID-JSON", r.stdout)

    # 8. リポジトリにだけあるテストは MISSING で exit 1
    def test_repo_only_test_is_missing(self):
        self.write_repo("tests/test_x.py", "print('ok')\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("MISSING", r.stdout)
        self.assertIn("test_x.py", r.stdout)

    # 9. 稼働中にだけあるテストは UNPUBLISHED で exit 1
    def test_live_only_test_is_unpublished(self):
        self.write_live_real("tests/test_y.py", "print('ok')\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("UNPUBLISHED", r.stdout)
        self.assertIn("test_y.py", r.stdout)

    # 9b. シンボリックリンクで置かれた未公開テストも UNPUBLISHED
    # （2026-11-21、agyレビュー指摘：-type f だけだとすり抜ける）
    def test_live_only_symlinked_test_is_unpublished(self):
        other = os.path.join(self.tmp.name, "other.py")
        with open(other, "w", encoding="utf-8") as f:
            f.write("print('other')\n")
        os.symlink(other, os.path.join(self.live, "tests", "test_z.py"))
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("UNPUBLISHED", r.stdout)
        self.assertIn("test_z.py", r.stdout)

    # 10. 稼働中の tests/ の .bak は対象外
    def test_live_test_bak_ignored(self):
        self.write_live_real("tests/test_x.py.bak-20261112-stats",
                             "print('old')\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 0)

    # 11. サブディレクトリのテストも対象（再帰）
    def test_subdir_test_covered(self):
        self.write_repo("tests/sub/test_x.py", "print('ok')\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("MISSING", r.stdout)

    # 11b. サブディレクトリのシンボリックリンク正常一致は通る
    def test_subdir_symlink_match_passes(self):
        self.write_repo("tests/sub/test_x.py", "print('ok')\n")
        self.link_live("tests/sub/test_x.py")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    # 11c. サブディレクトリ配下の未公開テストも UNPUBLISHED
    def test_subdir_live_only_test_is_unpublished(self):
        self.write_live_real("tests/sub/test_y.py", "print('ok')\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("UNPUBLISHED", r.stdout)
        self.assertIn("test_y.py", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
