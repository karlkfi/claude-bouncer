#!/usr/bin/env python3
"""Run the test suite across processes, optionally capping how many may skip.

The end-to-end layer invokes the hook as a subprocess once per assertion, and
that is the whole cost of the suite: Q70 measured 865 spawns at 35ms each,
against a 29s serial run. Most of a spawn is interpreter startup and loading the
4,655-line script, so there is nothing to optimise inside it -- only cores to
spread it over. The suite is safe to shard at test-method granularity: no
``setUpClass``, no ``os.chdir``, and every fixture builds its own
``TemporaryDirectory``.

``python3 -m unittest discover tests`` still runs the same tests serially, and
is the thing to fall back to when this runner is what's under suspicion.

A skip is invisible in a plain run: the trailer reads ``OK``, and a test that
quietly stops running on a platform looks exactly like one that passes there.
Windows still skips a few on purpose -- genuine platform splits, such as POSIX
permission bits -- so the count can't be gated at zero. ``--max-skips`` keeps
the known skips green and turns a new one red. The ceiling is a ratchet, not a
budget: when the run comes in under it, say so loudly, because a ceiling nobody
tightens stops ratcheting.

Usage:
    python3 scripts/run-tests.py
    python3 scripts/run-tests.py --jobs 4
    python3 scripts/run-tests.py --max-skips 5
"""
import argparse
import concurrent.futures
import math
import os
import pathlib
import sys
import time
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
TESTS = REPO / "tests"
CEILING_FILE = ".github/workflows/tests.yml"  # at the monorepo root

# Discovery hands back the import failure as a synthetic test whose name can't
# be loaded again by a worker.
FAILED_IMPORT = "unittest.loader._FailedTest"


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _load_path(tests_dir):
    """Make the test modules importable by the bare names discovery reports."""
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)


def run_batch(tests_dir, ids):
    """Run the named tests in this process; return outcomes as plain data.

    TestResult holds TestCase instances, which don't survive the trip back to
    the parent, so the ids are read off here.
    """
    _load_path(tests_dir)
    result = unittest.TestResult()
    result.buffer = True
    unittest.TestLoader().loadTestsFromNames(ids).run(result)
    return {
        "run": result.testsRun,
        "failures": [(t.id(), tb) for t, tb in result.failures],
        "errors": [(t.id(), tb) for t, tb in result.errors],
        "skipped": [(t.id(), reason) for t, reason in result.skipped],
        "expected_failures": len(result.expectedFailures),
        "unexpected_successes": [t.id() for t in result.unexpectedSuccesses],
    }


def batch(ids, jobs):
    """Split ids into batches small enough for the pool to balance itself.

    Discovery order groups each class together, and the end-to-end classes cost
    two orders of magnitude more per test than the unit ones, so a contiguous
    split hands one worker the whole tail. Striding mixes them.
    """
    size = max(1, min(16, len(ids) // (jobs * 8)))
    count = math.ceil(len(ids) / size)
    return [ids[i::count] for i in range(count)]


def merge(summary, part):
    summary["run"] += part["run"]
    summary["expected_failures"] += part["expected_failures"]
    for key in ("failures", "errors", "skipped", "unexpected_successes"):
        summary[key].extend(part[key])


def progress(part):
    return ("E" * len(part["errors"]) + "F" * len(part["failures"])
            + "s" * len(part["skipped"])
            + "." * (part["run"] - len(part["errors"]) - len(part["failures"])
                     - len(part["skipped"])))


def report(summary, elapsed, jobs):
    for label, entries in (("ERROR", summary["errors"]),
                           ("FAIL", summary["failures"])):
        for test_id, text in entries:
            print("\n" + "=" * 70)
            print("%s: %s" % (label, test_id))
            print("-" * 70)
            print(text)

    for test_id in summary["unexpected_successes"]:
        print("UNEXPECTED SUCCESS: %s" % test_id)

    print("-" * 70)
    print("Ran %d tests in %.3fs across %d processes\n"
          % (summary["run"], elapsed, jobs))

    counts = [("failures", len(summary["failures"])),
              ("errors", len(summary["errors"])),
              ("skipped", len(summary["skipped"])),
              ("expected failures", summary["expected_failures"]),
              ("unexpected successes", len(summary["unexpected_successes"]))]
    detail = ", ".join("%s=%d" % (n, c) for n, c in counts if c)
    ok = not (summary["failures"] or summary["errors"]
              or summary["unexpected_successes"])
    print(("OK" if ok else "FAILED") + (" (%s)" % detail if detail else ""))
    return ok


def check_ceiling(skipped, ceiling):
    """The Q45 ratchet. Returns an error message, or None when the run is fine."""
    for test_id, reason in sorted(skipped):
        print("skips: skipped %s -- %s" % (test_id, reason))
    print("skips: skipped=%d ceiling=%d" % (len(skipped), ceiling))

    if len(skipped) > ceiling:
        return ("skips: REGRESSION -- %d more test(s) skip than the ceiling "
                "allows. Make them run, or raise --max-skips in %s and say why."
                % (len(skipped) - ceiling, CEILING_FILE))

    if len(skipped) < ceiling:
        hint = ("Tighten `--max-skips` in `%s` to `%d`.\n"
                % (CEILING_FILE, len(skipped)))
        print("skips: IMPROVED -- " + hint, end="")
        summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_file:
            with open(summary_file, "a") as fh:
                fh.write(hint)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1,
                        help="worker processes (default: one per core)")
    parser.add_argument("--max-skips", type=int, default=None,
                        help="fail when more tests skip than this")
    parser.add_argument("--tests", default=str(TESTS),
                        help="directory to discover (default: tests/)")
    args = parser.parse_args()

    tests_dir = str(pathlib.Path(args.tests).resolve())
    _load_path(tests_dir)
    suite = unittest.TestLoader().discover(tests_dir)
    ids = [t.id() for t in _flatten(suite)]

    if any(i.startswith(FAILED_IMPORT) for i in ids):
        # Nothing to shard -- report the import error the way unittest does.
        return 0 if unittest.TextTestRunner().run(suite).wasSuccessful() else 1

    jobs = max(1, min(args.jobs, len(ids)))
    summary = {"run": 0, "failures": [], "errors": [], "skipped": [],
               "expected_failures": 0, "unexpected_successes": []}
    start = time.perf_counter()
    with concurrent.futures.ProcessPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(run_batch, tests_dir, b)
                   for b in batch(ids, jobs)]
        for future in concurrent.futures.as_completed(futures):
            part = future.result()
            merge(summary, part)
            sys.stderr.write(progress(part))
            sys.stderr.flush()
    sys.stderr.write("\n")

    ok = report(summary, time.perf_counter() - start, jobs)
    if not ok:
        return 1
    if args.max_skips is not None:
        problem = check_ceiling(summary["skipped"], args.max_skips)
        if problem:
            sys.stdout.flush()   # or the verdict lands above its own evidence
            print(problem, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
