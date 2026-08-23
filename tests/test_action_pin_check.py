"""Tests for the action-pin gate itself.

The gate asserts a property of the workflows, so it stays green whenever they
happen to be correct -- including once it has quietly stopped reading them at
all. These plant each hole in turn and require the gate to report it, because
a check that cannot go red passes forever, and the first moved tag it would
have caught then runs unnoticed on every job in the repository.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'scripts', 'action-pin-check.py')

CHECKOUT = 'actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0'
SETUP = ('actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065'
         ' # v5.6.0')

WORKFLOW = """name: tests

on:
  pull_request:

jobs:
  lib:
    runs-on: ubuntu-latest
    steps:
      - uses: %(checkout)s
      - uses: %(setup)s
        with:
          python-version: '3.9'
      - run: make lib-test
"""


class ActionPinGateTests(unittest.TestCase):
    def setUp(self):
        """A throwaway copy of the layout, so a test never edits the real tree."""
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, 'scripts'))
        os.makedirs(os.path.join(self.tmp, '.github', 'workflows'))
        shutil.copy(SCRIPT, os.path.join(self.tmp, 'scripts'))

    def write(self, checkout=CHECKOUT, setup=SETUP, name='tests.yml'):
        path = os.path.join(self.tmp, '.github', 'workflows', name)
        with open(path, 'w') as f:
            f.write(WORKFLOW % {'checkout': checkout, 'setup': setup})

    def run_gate(self):
        return subprocess.run(
            [sys.executable, os.path.join(self.tmp, 'scripts',
                                          'action-pin-check.py')],
            capture_output=True, text=True)

    def test_a_fully_pinned_workflow_passes(self):
        self.write()
        proc = self.run_gate()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)

    def test_a_mutable_tag_is_reported(self):
        """The case the whole gate exists for: a tag its owner can move."""
        self.write(checkout='actions/checkout@v4')
        proc = self.run_gate()
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn('actions/checkout@v4 is a mutable ref', proc.stdout)

    def test_a_branch_ref_is_reported(self):
        """Worse than a tag: it moves on every push to the action's repo."""
        self.write(checkout='actions/checkout@main')
        proc = self.run_gate()
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn('actions/checkout@main is a mutable ref', proc.stdout)

    def test_an_abbreviated_sha_is_reported(self):
        """A short SHA looks pinned and is ambiguous by construction."""
        self.write(checkout='actions/checkout@11d5960')
        proc = self.run_gate()
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn('actions/checkout@11d5960 is a mutable ref', proc.stdout)

    def test_a_ref_less_action_is_reported(self):
        self.write(checkout='actions/checkout')
        proc = self.run_gate()
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn('actions/checkout carries no ref at all', proc.stdout)

    def test_a_pin_without_a_version_comment_is_reported(self):
        """Assertion 2: the half that keeps a pin bumpable rather than frozen."""
        self.write(checkout=CHECKOUT.split(' #')[0])
        proc = self.run_gate()
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn('has no trailing `# <version>` comment', proc.stdout)

    def test_a_local_action_is_allowed(self):
        """Deliberately asserts green among neighbours asserting red: an
        in-tree action moves with the commit that runs it, so demanding a SHA
        of it would be the gate firing on something correct."""
        self.write(checkout='./.github/actions/setup')
        proc = self.run_gate()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)

    def test_every_workflow_file_is_read(self):
        """A gate that stops after the first file passes on a tree whose
        second workflow is unpinned. The `.yaml` spelling is covered here too."""
        self.write()
        self.write(checkout='actions/checkout@v4', name='release-note.yaml')
        proc = self.run_gate()
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn('release-note.yaml', proc.stdout)


if __name__ == '__main__':
    unittest.main()
