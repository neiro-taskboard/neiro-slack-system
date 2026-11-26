#!/usr/bin/env python3
"""Run the whole hooks test suite: every test_*.py in this directory.

中-6 (Yama's Claude Code audit, 2026-11-21): run_tests.py used to contain
only 11 inline duplicates of the slack-poll tests, while the repo now has
23 test files in two styles. The README's "run run_tests.py before
commit/push" gate therefore passed while most of the suite never ran.

This runner fixes that without changing any test file:
  1. unittest suites: `python3 -m unittest discover` over test_*.py
     (12 modules, 166 tests as of 2026-11-26).
  2. standalone check-style scripts: the files that print
     "[PASS] ..." / "N passed, 0 failed" from their own main() instead of
     defining unittest TestCases (11 files, 345 checks as of 2026-11-26:
     poll, answered-check, reconcile-ambiguous, send-once fail-closed,
     send-locked, resend, permalink, thread-bounded, mark-guard,
     ledger-parroting, send-once timeout). They are detected, not hardcoded: a test file
     whose module loads zero unittest tests is run as a script.
  3. Exit code is 1 if anything fails, 0 only when everything passes.
     (511 checks in total as of 2026-11-26.)

Style follows the neighbours (plain Python, no pytest):
  python3 run_tests.py
"""
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = 300


def run(cmd, label):
    print(f"== {label}", flush=True)
    try:
        proc = subprocess.run(cmd, cwd=HERE, timeout=TIMEOUT,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True)
    except subprocess.TimeoutExpired as exc:
        # A hanging suite must fail this runner, not abort it: without
        # this the traceback would stop the remaining suites and print
        # no FAILED summary (Groq gpt-oss-120b review, 2026-11-26).
        print(f"TIMEOUT after {TIMEOUT}s")
        tail = "\n".join((exc.stdout or "").rstrip().splitlines()[-3:])
        if tail:
            print(tail)
        print("exit=124")
        return 124
    tail = "\n".join(proc.stdout.rstrip().splitlines()[-3:])
    if tail:
        print(tail)
    print(f"exit={proc.returncode}")
    return proc.returncode


def main():
    failures = []

    # 1. unittest suites (discover finds the TestCase-based files).
    rc = run([sys.executable, "-m", "unittest", "discover",
              "-s", HERE, "-p", "test_*.py"], "unittest discover")
    if rc != 0:
        failures.append("unittest discover")

    # 2. Standalone check-style scripts: files with zero unittest tests.
    # The import below only counts tests: check-style scripts must keep
    # their work behind `if __name__ == "__main__"` (all 11 do, as of
    # 2026-11-26), or the import itself would run their checks.
    loader = unittest.TestLoader()
    saved_path = list(sys.path)
    sys.path.insert(0, HERE)
    try:
        for name in sorted(os.listdir(HERE)):
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            module = name[:-3]
            try:
                suite = loader.loadTestsFromName(module)
                count = suite.countTestCases()
            except Exception as exc:  # import error: discover already failed
                print(f"== {name}: could not load ({exc})")
                failures.append(name)
                continue
            if count:
                continue
            rc = run([sys.executable, os.path.join(HERE, name)], name)
            if rc != 0:
                failures.append(name)
    finally:
        sys.path[:] = saved_path

    if failures:
        print(f"\nFAILED: {', '.join(failures)}")
        return 1
    print("\nALL SUITES PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
