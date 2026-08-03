#!/usr/bin/env python3
"""Run the suite and fail when more tests skip than the ceiling allows.

A skip is invisible in a plain ``unittest discover`` run: the trailer reads
``OK``, and a test that quietly stops running on a platform looks exactly like
one that passes there. Windows already skips a few on purpose -- ``$HOME`` is
unset under cmd.exe (Q43) -- so the count can't be gated at zero. A ceiling
keeps the known skips green and turns a new one red.

The ceiling is a ratchet, not a budget: when the run comes in under it, say so
loudly, because a ceiling nobody tightens stops ratcheting.

Usage:
    python3 scripts/skip-ceiling.py --max-skips 12
"""
import argparse
import os
import pathlib
import sys
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
CEILING_FILE = ".github/workflows/tests.yml"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-skips", type=int, required=True,
                        help="how many skipped tests this platform may have")
    args = parser.parse_args()

    suite = unittest.TestLoader().discover(str(REPO / "tests"))
    result = unittest.TextTestRunner(verbosity=1).run(suite)

    for test, reason in result.skipped:
        print("skip-ceiling: skipped %s -- %s" % (test.id(), reason))
    skips = len(result.skipped)
    print("skip-ceiling: skipped=%d ceiling=%d" % (skips, args.max_skips))

    if not result.wasSuccessful():
        sys.exit("skip-ceiling: the suite did not pass")

    if skips > args.max_skips:
        sys.exit("skip-ceiling: REGRESSION -- %d more test(s) skip than the "
                 "ceiling allows. Make them run, or raise --max-skips in %s "
                 "and say why." % (skips - args.max_skips, CEILING_FILE))

    if skips < args.max_skips:
        hint = ("Tighten `--max-skips` in `%s` to `%d`.\n" % (CEILING_FILE, skips))
        print("skip-ceiling: IMPROVED -- " + hint, end="")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as fh:
                fh.write(hint)


if __name__ == "__main__":
    main()
