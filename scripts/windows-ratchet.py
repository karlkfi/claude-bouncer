#!/usr/bin/env python3
"""Run the suite and fail only when Windows results get worse than recorded.

The suite does not pass on Windows yet: many fixtures assume POSIX path
semantics (Q39). That leaves no good plain-CI option. A gating job is red on
every pull request until Q39 lands. A ``continue-on-error`` job is red too, it
just doesn't block. Either way the red stops meaning anything.

So gate on the delta instead of the absolute. A recorded baseline of failures
and errors passes when matched or beaten and fails when exceeded, which still
catches the thing worth catching -- a new Windows regression, or a crash like
the ``os.getuid()`` AttributeError reappearing -- while Q39 is worked.

Errors are tracked separately from failures on purpose. A failure is an
assertion that did not hold; an error is the guard blowing up before it could
decide, which is the shape of every Windows bug found so far.
"""
import json
import os
import pathlib
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
BASELINE = REPO / "tests" / "windows-baseline.json"

# unittest's trailer, e.g. "FAILED (failures=78, errors=0, skipped=67)".
COUNT_RE = re.compile(r"\b(failures|errors)=(\d+)")
RAN_RE = re.compile(r"^Ran (\d+) tests? in ", re.M)


def run_suite():
    """Return (ran, failures, errors) from a discover run, or exit non-zero."""
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "tests"],
        capture_output=True, text=True, cwd=REPO)
    output = proc.stderr + proc.stdout
    print(output)

    ran = RAN_RE.search(output)
    if not ran:
        # No trailer means the suite never got as far as running, so a count
        # comparison would be comparing against nothing.
        sys.exit("windows-ratchet: suite did not run to completion")

    counts = {"failures": 0, "errors": 0}
    counts.update({k: int(v) for k, v in COUNT_RE.findall(output)})
    return int(ran.group(1)), counts["failures"], counts["errors"]


def main():
    ran, failures, errors = run_suite()
    baseline = json.loads(BASELINE.read_text())
    max_f, max_e = baseline["failures"], baseline["errors"]

    print("windows-ratchet: ran=%d failures=%d errors=%d" % (ran, failures, errors))
    print("windows-ratchet: baseline failures<=%d errors<=%d (%s)"
          % (max_f, max_e, baseline.get("measured_on", "unknown")))

    if failures > max_f or errors > max_e:
        sys.exit("windows-ratchet: REGRESSION -- results got worse than the "
                 "baseline. Fix it, or justify the change and update %s."
                 % BASELINE.relative_to(REPO))

    if failures < max_f or errors < max_e:
        # Passing quietly here would let the baseline drift permanently loose,
        # which is how a ratchet stops ratcheting.
        print("windows-ratchet: IMPROVED -- tighten the baseline to "
              'failures=%d errors=%d' % (failures, errors))
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as fh:
                fh.write("Tighten `tests/windows-baseline.json` to "
                         "`failures=%d errors=%d`.\n" % (failures, errors))


if __name__ == "__main__":
    main()
