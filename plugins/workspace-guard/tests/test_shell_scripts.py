"""Tests for the shell helpers in ``scripts/``.

The Python scripts each have a suite. The one shell script left here drives a
live Claude session and a window manager, so there is nothing to assert about
its behavior — ``bash -n`` and the executable bit are the whole gate, and both
catch the failures that would otherwise surface only when someone reaches for
it.

Every test shells out to ``bash``. That is Git Bash on Windows, where these
scripts are ordinary GNU-tool scripts and run the same; the skip guard exists
for a host with no bash at all, not as a platform exemption.

The backlog helpers this file also used to cover — ``lint-backlog.sh``,
``next-task.sh``, ``backlog-metrics.sh`` — went with the per-plugin
``docs/STATUS.md``. The backlog is now one store at the repository root and
``scripts/queue.py`` checks it; ``tests/test_backlog.py`` there is the
successor to those cases.
"""

import os
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO, "scripts")
BASH = shutil.which("bash")

SHELL_SCRIPTS = [
    "capture-prompt-screenshot.sh",
]


@unittest.skipUnless(BASH, "no bash on PATH")
class ShellSyntaxTests(unittest.TestCase):
    def test_every_shell_script_parses(self):
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name):
                p = subprocess.run([BASH, "-n", os.path.join(SCRIPTS, name)],
                                   capture_output=True, text=True, timeout=60)
                self.assertEqual(p.returncode, 0, p.stderr)

    def test_every_shell_script_is_executable(self):
        for name in SHELL_SCRIPTS:
            with self.subTest(script=name):
                self.assertTrue(os.access(os.path.join(SCRIPTS, name), os.X_OK),
                                f"{name} is not executable")


if __name__ == "__main__":
    unittest.main()
