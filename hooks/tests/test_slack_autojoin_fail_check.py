#!/usr/bin/env python3
"""Tests for hooks/scripts/slack-autojoin-fail-check.sh.

No real Slack calls and no real systemd: systemctl is stubbed with a
temporary script. Run with: python3 test_slack_autojoin_fail_check.py
"""
import os
import stat
import subprocess
import tempfile
import unittest

CHECK_PATH = os.path.expanduser(
    "~/workspace/neiro-slack-system/hooks/scripts/slack-autojoin-fail-check.sh")

STUB_SYSTEMCTL = """#!/bin/bash
# Fake systemctl for tests. Behaviour via env vars:
#   STUB_FAILED=1 -> is-failed exits 0 (failed); else exits 1.
#   STUB_RESULT / STUB_STATUS -> values printed by `show`.
if [ "$1" = "is-failed" ]; then
  if [ "${STUB_FAILED:-0}" = "1" ]; then exit 0; else exit 1; fi
fi
if [ "$1" = "show" ]; then
  for arg in "$@"; do
    case "$arg" in
      Result) echo "${STUB_RESULT:-exit-code}" ;;
      ExecMainStatus) echo "${STUB_STATUS:-1}" ;;
    esac
  done
  exit 0
fi
echo "unexpected systemctl call: $*" >&2
exit 2
"""


def run_check(stub_failed="0", stub_result="exit-code", stub_status="1"):
    tmp = tempfile.mkdtemp()
    stub = os.path.join(tmp, "systemctl")
    with open(stub, "w", encoding="utf-8") as f:
        f.write(STUB_SYSTEMCTL)
    os.chmod(stub, os.stat(stub).st_mode | stat.S_IEXEC)
    env = dict(os.environ, SYSTEMCTL=stub,
               STUB_FAILED=stub_failed,
               STUB_RESULT=stub_result, STUB_STATUS=stub_status)
    r = subprocess.run(["bash", CHECK_PATH], env=env,
                       capture_output=True, text=True)
    return r


class AutojoinFailCheckTest(unittest.TestCase):
    # 1. 失敗していなければ exit 0 で無言
    def test_healthy_passes_silently(self):
        r = run_check(stub_failed="0")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "")

    # 2. 失敗していれば FAILED で exit 1、Result と終了コードを含む
    def test_failed_reports_result_and_status(self):
        r = run_check(stub_failed="1", stub_result="exit-code",
                      stub_status="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAILED", r.stdout)
        self.assertIn("slack-auto-join.service", r.stdout)
        self.assertIn("Result=exit-code", r.stdout)
        self.assertIn("終了コード=1", r.stdout)

    # 3. Result が取れなくても unknown で報告する（落ちない）
    def test_failed_with_unknown_result(self):
        r = run_check(stub_failed="1", stub_result="unknown",
                      stub_status="?")
        self.assertEqual(r.returncode, 1)
        self.assertIn("FAILED", r.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
