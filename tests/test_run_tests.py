#!/usr/bin/env python3
"""Tests for scripts/run-tests.py, the sharding test runner.

Run with: python3 scripts/run-tests.py

The end-to-end cases point the runner at a synthetic tests directory via
``--tests`` rather than at this repo's own suite: a fixture can then contain a
deliberate failure, skip, or import error, and a case costs a fraction of a
second instead of the whole suite.
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from importlib import util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNNER = REPO / "scripts" / "run-tests.py"

# Filename has a dash, so import by path.
_spec = util.spec_from_file_location("run_tests", RUNNER)
runner = util.module_from_spec(_spec)
_spec.loader.exec_module(runner)

SAMPLE = """
import unittest


class SampleTests(unittest.TestCase):
    def test_pass_one(self):
        pass

    def test_pass_two(self):
        pass

    def test_skipped(self):
        self.skipTest("on purpose")

    @unittest.expectedFailure
    def test_expected_failure(self):
        self.fail("known")
"""

FAILING = """
import unittest


class BrokenTests(unittest.TestCase):
    def test_fails(self):
        self.fail("boom")

    def test_errors(self):
        raise RuntimeError("kaboom")
"""


class BatchTests(unittest.TestCase):
    """Every id runs exactly once, whatever the shape of the split."""

    def test_batches_partition_the_ids(self):
        ids = ["t%03d" % i for i in range(200)]
        for jobs in (1, 2, 4, 18):
            flat = [i for b in runner.batch(ids, jobs) for i in b]
            self.assertCountEqual(flat, ids, "jobs=%d lost or duplicated" % jobs)

    def test_batches_are_never_empty(self):
        for count in (1, 3, 17, 200):
            ids = ["t%d" % i for i in range(count)]
            batches = runner.batch(ids, 4)
            self.assertTrue(all(batches), "empty batch for %d ids" % count)

    def test_batches_stride_across_discovery_order(self):
        # Discovery groups a class together and the end-to-end classes cost two
        # orders of magnitude more per test, so a contiguous split would hand
        # one worker the whole tail.
        ids = ["t%03d" % i for i in range(200)]
        first = runner.batch(ids, 4)[0]
        self.assertGreater(len(set(first)), 1)
        self.assertNotEqual(first, ids[:len(first)])


class MergeTests(unittest.TestCase):
    def test_merge_accumulates_every_field(self):
        summary = {"run": 0, "failures": [], "errors": [], "skipped": [],
                   "expected_failures": 0, "unexpected_successes": []}
        runner.merge(summary, {"run": 3, "failures": [("a", "tb")],
                               "errors": [], "skipped": [("b", "why")],
                               "expected_failures": 1,
                               "unexpected_successes": []})
        runner.merge(summary, {"run": 2, "failures": [], "errors": [("c", "tb")],
                               "skipped": [], "expected_failures": 2,
                               "unexpected_successes": ["d"]})
        self.assertEqual(summary["run"], 5)
        self.assertEqual(summary["expected_failures"], 3)
        self.assertEqual(summary["failures"], [("a", "tb")])
        self.assertEqual(summary["errors"], [("c", "tb")])
        self.assertEqual(summary["skipped"], [("b", "why")])
        self.assertEqual(summary["unexpected_successes"], ["d"])


class CheckCeilingTests(unittest.TestCase):
    """The Q45 ratchet: over is a failure, under says so loudly."""

    def _check(self, skipped, ceiling):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            problem = runner.check_ceiling(skipped, ceiling)
        return problem, out.getvalue()

    def test_over_the_ceiling_returns_a_regression(self):
        problem, out = self._check([("a", "why"), ("b", "why")], 1)
        self.assertIn("REGRESSION", problem)
        self.assertIn("1 more test(s)", problem)
        self.assertIn("skips: skipped=2 ceiling=1", out)

    def test_at_the_ceiling_is_quiet(self):
        problem, out = self._check([("a", "why")], 1)
        self.assertIsNone(problem)
        self.assertNotIn("IMPROVED", out)

    def test_every_skip_is_named_with_its_reason(self):
        _, out = self._check([("a", "no drive letters"), ("b", "Windows-only")], 2)
        self.assertIn("skips: skipped a -- no drive letters", out)
        self.assertIn("skips: skipped b -- Windows-only", out)

    def test_under_the_ceiling_asks_for_a_tighter_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary = os.path.join(tmp, "summary.md")
            os.environ["GITHUB_STEP_SUMMARY"] = summary
            try:
                problem, out = self._check([("a", "why")], 4)
            finally:
                os.environ.pop("GITHUB_STEP_SUMMARY", None)
            self.assertIsNone(problem)
            self.assertIn("IMPROVED", out)
            with open(summary) as fh:
                self.assertIn("Tighten `--max-skips`", fh.read())


def write_suite(tmp, **modules):
    for name, body in modules.items():
        with open(os.path.join(tmp, name + ".py"), "w") as fh:
            fh.write(textwrap.dedent(body))
    return tmp


class RunnerEndToEndTests(unittest.TestCase):
    """The verdict the runner prints and the status it exits with."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.suite_dir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def _run(self, *args, jobs="2"):
        env = dict(os.environ)
        env.pop("GITHUB_STEP_SUMMARY", None)
        return subprocess.run(
            [sys.executable, str(RUNNER), "--tests", self.suite_dir,
             "--jobs", jobs, *args],
            capture_output=True, text=True, env=env, timeout=120,
        )

    def test_passing_suite_is_ok(self):
        write_suite(self.suite_dir, test_sample=SAMPLE)
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Ran 4 tests", result.stdout)
        self.assertIn("OK (skipped=1, expected failures=1)", result.stdout)

    def test_sharding_does_not_change_the_counts(self):
        write_suite(self.suite_dir, test_sample=SAMPLE)
        serial = self._run(jobs="1").stdout
        sharded = self._run(jobs="4").stdout
        self.assertIn("Ran 4 tests", serial)
        self.assertIn("Ran 4 tests", sharded)
        self.assertIn("OK (skipped=1, expected failures=1)", serial)
        self.assertIn("OK (skipped=1, expected failures=1)", sharded)

    def test_failure_and_error_are_reported_and_fail_the_run(self):
        write_suite(self.suite_dir, test_broken=FAILING)
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAILED (failures=1, errors=1)", result.stdout)
        self.assertIn("FAIL: test_broken.BrokenTests.test_fails", result.stdout)
        self.assertIn("boom", result.stdout)
        self.assertIn("ERROR: test_broken.BrokenTests.test_errors", result.stdout)
        self.assertIn("kaboom", result.stdout)

    def test_import_error_is_reported_not_sharded(self):
        # Discovery hands an unimportable module back as a synthetic test whose
        # name a worker can't load again, so the run stays in this process.
        write_suite(self.suite_dir, test_bad="import definitely_not_a_module\n")
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("definitely_not_a_module",
                      result.stdout + result.stderr)

    def test_ceiling_breach_fails_a_passing_suite(self):
        write_suite(self.suite_dir, test_sample=SAMPLE)
        result = self._run("--max-skips", "0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("skips: skipped=1 ceiling=0", result.stdout)
        self.assertIn("REGRESSION", result.stderr)

    def test_ceiling_met_exactly_passes(self):
        write_suite(self.suite_dir, test_sample=SAMPLE)
        result = self._run("--max-skips", "1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("IMPROVED", result.stdout)

    def test_room_under_the_ceiling_asks_for_a_tighter_one(self):
        write_suite(self.suite_dir, test_sample=SAMPLE)
        result = self._run("--max-skips", "5")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("IMPROVED", result.stdout)
        self.assertIn("to `1`", result.stdout)

    def test_a_failing_suite_never_reaches_the_ceiling(self):
        # A ceiling verdict on a red run would read as the reason it failed.
        write_suite(self.suite_dir, test_broken=FAILING)
        result = self._run("--max-skips", "0")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("skips:", result.stdout)


if __name__ == "__main__":
    unittest.main()
