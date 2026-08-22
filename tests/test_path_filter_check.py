"""Tests for the path-filter gate itself.

The gate asserts a property of the workflow, so it stays green whenever the
workflow happens to be correct -- including once it has quietly stopped reading
the filters at all. These plant each hole in turn and require the gate to
report it, because a check that cannot go red passes forever and the first
unguarded suite it would have caught skips every run after that.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'scripts', 'path-filter-check.py')

WORKFLOW = """name: tests

on:
  pull_request:

jobs:
  changes:
    runs-on: ubuntu-latest
    outputs:
      alpha_guard: ${{ steps.filter.outputs.alpha_guard }}
      beta_guard: ${{ steps.filter.outputs.beta_guard }}
    steps:
      - uses: dorny/paths-filter@v4
        id: filter
        with:
          filters: |
            shared: &shared
              - 'lib/**'
              - '.github/workflows/tests.yml'
%(filters)s

  alpha-guard:
    needs: [changes]
    if: needs.changes.outputs.%(alpha_guard)s == 'true'
    runs-on: ubuntu-latest
    steps:
      - run: python3 -m unittest discover tests
        working-directory: plugins/alpha-guard

  beta-guard:
    needs: [changes]
    if: needs.changes.outputs.beta_guard == 'true'
    runs-on: ubuntu-latest
    steps:
      - run: python3 -m unittest discover tests
        working-directory: plugins/beta-guard
"""

BOTH = """            alpha_guard:
              - *shared
              - 'plugins/alpha-guard/**'
            beta_guard:
              - *shared
              - 'plugins/beta-guard/**'"""


class PathFilterGateTests(unittest.TestCase):
    def setUp(self):
        """A throwaway copy of the layout, so a test never edits the real tree."""
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, 'scripts'))
        os.makedirs(os.path.join(self.tmp, '.github', 'workflows'))
        for name in ('alpha-guard', 'beta-guard'):
            os.makedirs(os.path.join(self.tmp, 'plugins', name))
        shutil.copy(SCRIPT, os.path.join(self.tmp, 'scripts'))

    def write(self, filters=BOTH, alpha_guard='alpha_guard'):
        path = os.path.join(self.tmp, '.github', 'workflows', 'tests.yml')
        with open(path, 'w') as f:
            f.write(WORKFLOW % {'filters': filters, 'alpha_guard': alpha_guard})

    def edit(self, old, new=''):
        path = os.path.join(self.tmp, '.github', 'workflows', 'tests.yml')
        with open(path) as f:
            text = f.read()
        self.assertIn(old, text)
        with open(path, 'w') as f:
            f.write(text.replace(old, new))

    def run_gate(self):
        return subprocess.run(
            [sys.executable, os.path.join(self.tmp, 'scripts',
                                          'path-filter-check.py')],
            capture_output=True, text=True)

    def test_a_consistent_workflow_passes(self):
        self.write()
        proc = self.run_gate()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)

    def test_a_plugin_with_no_filter_is_reported(self):
        """The sixth-guard case: a new plugin whose suite would never run."""
        self.write()
        self.edit("            beta_guard:\n              - *shared\n"
                  "              - 'plugins/beta-guard/**'\n")
        proc = self.run_gate()
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn("no filter named 'beta_guard'", proc.stdout)

    def test_a_filter_that_omits_its_own_directory_is_reported(self):
        self.write()
        self.edit("              - 'plugins/beta-guard/**'\n")
        proc = self.run_gate()
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn('does not name plugins/beta-guard/**', proc.stdout)

    def test_a_filter_that_drops_the_shared_anchor_is_reported(self):
        """Without it a lib/ edit skips the guard that vendors the parser."""
        self.write()
        self.edit("            beta_guard:\n              - *shared\n",
                  "            beta_guard:\n")
        proc = self.run_gate()
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn("does not pull in *shared", proc.stdout)

    def test_a_shared_anchor_that_drops_the_workflow_is_reported(self):
        self.write()
        self.edit("              - '.github/workflows/tests.yml'\n")
        proc = self.run_gate()
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn('the shared anchor does not list', proc.stdout)

    def test_an_unguarded_job_is_reported(self):
        """A job added to the workflow without an `if:` -- the drift that
        reopens the hole in a new shape."""
        self.write()
        self.edit("    if: needs.changes.outputs.beta_guard == 'true'\n")
        proc = self.run_gate()
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn("job 'beta-guard' works in beta-guard and carries no",
                      proc.stdout)

    def test_a_job_gated_on_the_wrong_plugin_is_reported(self):
        """The quiet one: it runs on the other plugin's changes and skips on
        its own, so the suite is present, green, and never exercised."""
        self.write(alpha_guard='beta_guard')
        proc = self.run_gate()
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn("job 'alpha-guard' works in alpha-guard but is gated on "
                      "beta_guard", proc.stdout)


if __name__ == '__main__':
    unittest.main()
