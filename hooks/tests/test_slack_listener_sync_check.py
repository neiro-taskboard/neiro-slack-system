#!/usr/bin/env python3
"""Tests for hooks/scripts/slack-listener-sync-check.sh.

No real Slack calls: only temporary directories and the check script
itself are exercised. Run with: python3 test_slack_listener_sync_check.py
"""
import os
import subprocess
import tempfile
import unittest

CHECK_PATH = os.path.expanduser(
    "~/workspace/neiro-slack-system/hooks/scripts/slack-listener-sync-check.sh")


def run_check(live_dir, repo_dir):
    env = dict(os.environ, LIVE_DIR=live_dir,
               REPO_LISTENER_DIR=repo_dir)
    return subprocess.run(["bash", CHECK_PATH], env=env,
                          capture_output=True, text=True)


class ListenerSyncCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.live = os.path.join(self.tmp.name, "live")
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.live)
        os.makedirs(self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def write_both(self, name, content, live_content=None):
        with open(os.path.join(self.repo, name), "w",
                  encoding="utf-8") as f:
            f.write(content)
        with open(os.path.join(self.live, name), "w",
                  encoding="utf-8") as f:
            f.write(content if live_content is None else live_content)

    def write_repo_test(self, name, content):
        path = os.path.join(self.repo, "tests", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def write_live_test(self, name, content):
        path = os.path.join(self.live, "tests", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    # 1. 一致していれば exit 0 で無言
    def test_matching_files_pass_silently(self):
        self.write_both("auto-join.py", "print('ok')\n")
        self.write_both("watchdog.sh", "#!/bin/bash\necho ok\n")
        self.write_both("slack-auto-join.service", "[Unit]\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    # 2. 内容が違えば DRIFT で exit 1
    def test_drift_detected(self):
        self.write_both("auto-join.py", "print('new')\n",
                        live_content="print('old')\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("DRIFT", r.stdout)
        self.assertIn("auto-join.py", r.stdout)

    # 3. 稼働中のファイルが無ければ MISSING で exit 1
    def test_missing_live_file(self):
        with open(os.path.join(self.repo, "backfill.py"), "w",
                  encoding="utf-8") as f:
            f.write("print('x')\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("MISSING", r.stdout)
        self.assertIn("backfill.py", r.stdout)

    # 4. archive.py は USER_NAMES の伏せ字のため内容が違ってもよい
    def test_archive_py_content_difference_ignored(self):
        self.write_both(
            "archive.py",
            "USER_NAMES = {}\n",
            live_content="USER_NAMES = {\"U0TEST\": \"someone\"}\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    # 5. 構文が壊れていれば SYNTAX-ERROR で exit 1
    def test_syntax_error_detected(self):
        self.write_both("listener.py", "def broken(:\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("SYNTAX-ERROR", r.stdout)

    # 6. 稼働中だけのファイル（config.json や .bak）は対象外
    def test_live_only_files_ignored(self):
        self.write_both("auto-join.py", "print('ok')\n")
        with open(os.path.join(self.live, "config.json"), "w",
                  encoding="utf-8") as f:
            f.write("{}\n")
        with open(os.path.join(self.live, "auto-join.py.bak"), "w",
                  encoding="utf-8") as f:
            f.write("print('old')\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 0)

    # 7. リポジトリのテストの構文が壊れていれば SYNTAX-ERROR で exit 1
    def test_repo_test_syntax_error_detected(self):
        self.write_repo_test("test_x.py", "def broken(:\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("SYNTAX-ERROR", r.stdout)
        self.assertIn("test_x.py", r.stdout)

    # 8. リポジトリにだけあるテストは MISSING にならない（テストは稼働中へ
    #    配置しない運用のため）
    def test_repo_only_test_is_not_missing(self):
        self.write_repo_test("test_x.py", "print('ok')\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    # 8b. 稼働中とリポジトリのテストが一致すれば exit 0 で無言（正常系）
    # （2026-11-21、agyレビュー指摘）
    def test_matching_live_and_repo_tests_pass(self):
        self.write_repo_test("test_x.py", "print('ok')\n")
        self.write_live_test("test_x.py", "print('ok')\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    # 9. 稼働中にだけあるテストは UNPUBLISHED で exit 1
    def test_live_only_test_is_unpublished(self):
        self.write_live_test("test_y.py", "print('ok')\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("UNPUBLISHED", r.stdout)
        self.assertIn("test_y.py", r.stdout)

    # 10. 稼働中のテストがリポジトリと違えば DRIFT で exit 1
    def test_live_test_drift_detected(self):
        self.write_repo_test("test_x.py", "print('new')\n")
        self.write_live_test("test_x.py", "print('old')\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("DRIFT", r.stdout)
        self.assertIn("test_x.py", r.stdout)

    # 11. 稼働中の tests/ の .bak は対象外
    def test_live_test_bak_ignored(self):
        self.write_live_test("test_x.py.bak-20261112-stats",
                             "print('old')\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 0)

    # 12. サブディレクトリのテストも対象（再帰）
    def test_subdir_test_covered(self):
        self.write_repo_test("sub/test_x.py", "def broken(:\n")
        r = run_check(self.live, self.repo)
        self.assertEqual(r.returncode, 1)
        self.assertIn("SYNTAX-ERROR", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
