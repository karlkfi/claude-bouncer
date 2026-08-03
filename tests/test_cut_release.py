#!/usr/bin/env python3
"""Tests for scripts/cut-release.sh.

Run with: python3 -m unittest discover tests
     or:  python3 tests/test_cut_release.py

The release script can't be driven end to end from a test: even --dry-run runs
preflight (gh auth, a fetch of origin/main, and this very suite). What is
testable in isolation is the EXIT trap, whose status becomes the script's exit
status — a handler ending on a false test reports a successful release as a
failure. These tests lift the real handler out of the script and run it under
bash, so the assertion tracks the shipped source rather than a copy of it.
"""
import os
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "cut-release.sh"


def exit_trap_source():
    """The cleanup definition and its trap, verbatim from the script."""
    src = SCRIPT.read_text(encoding="utf-8")
    start = src.index("cleanup()")
    marker = "trap cleanup EXIT"
    return src[start:src.index(marker, start) + len(marker)]


def run_trap(notes_tmp):
    """Run the handler with NOTES_TMP set as the script would set it."""
    body = "set -euo pipefail\nNOTES_TMP=%s\n%s\n" % (
        shlex.quote(notes_tmp), exit_trap_source())
    return subprocess.run(["bash", "-c", body], capture_output=True, text=True)


class ExitTrapTests(unittest.TestCase):
    def test_no_tempfile_exits_zero(self):
        # --notes-file leaves NOTES_TMP empty; the release still succeeded.
        proc = run_trap("")
        self.assertEqual(proc.returncode, 0,
                         "EXIT trap leaked a failure status: %s" % proc.stderr)

    def test_tempfile_is_removed(self):
        fd, path = tempfile.mkstemp(prefix="cut-release-notes.")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.unlink(path))
        proc = run_trap(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(os.path.exists(path), "generated notes were not removed")


class SyntaxTests(unittest.TestCase):
    def test_script_parses(self):
        proc = subprocess.run(["bash", "-n", str(SCRIPT)],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
