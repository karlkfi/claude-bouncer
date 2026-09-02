"""Tests for the vendoring gate itself.

`VendoringTests` in test_bouncer_parse.py asserts the copies are currently in
sync, which is a statement about the tree. This asserts something different and
easier to get wrong: that the gate REPORTS drift when there is some. A check
that cannot fail passes forever, and the first thing it would have caught ships.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'scripts', 'sync-lib.py')


def _declared_modules():
    """The module list the script itself declares. Read from the script so the
    fixture cannot drift from it."""
    spec = importlib.util.spec_from_file_location('sync_lib', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.MODULES


MODULES = _declared_modules()


class SyncLibGateTests(unittest.TestCase):
    def setUp(self):
        """A throwaway copy of the layout, so a test never edits the real tree."""
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, 'scripts'))
        os.makedirs(os.path.join(self.tmp, 'lib'))
        shutil.copy(SCRIPT, os.path.join(self.tmp, 'scripts', 'sync-lib.py'))
        # Every module the script declares, not a hardcoded one: a fixture that
        # names its own list stops exercising the gate the moment a module is
        # added, and the gate is the thing under test.
        for module in MODULES:
            shutil.copy(os.path.join(ROOT, 'lib', module),
                        os.path.join(self.tmp, 'lib', module))
        for name in ('alpha-guard', 'beta-guard'):
            os.makedirs(os.path.join(self.tmp, 'plugins', name))

    def run_sync(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(self.tmp, 'scripts', 'sync-lib.py')] + list(args),
            capture_output=True, text=True)

    def test_check_fails_before_the_first_sync(self):
        r = self.run_sync('--check')
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertIn('alpha-guard', r.stderr)

    def test_sync_then_check_passes(self):
        self.assertEqual(0, self.run_sync().returncode)
        r = self.run_sync('--check')
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)

    def test_check_fails_on_a_drifted_copy(self):
        self.run_sync()
        target = os.path.join(self.tmp, 'plugins', 'beta-guard', 'lib', 'bouncer_parse.py')
        with open(target, 'a') as f:
            f.write('\n# drift\n')
        r = self.run_sync('--check')
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertIn('beta-guard', r.stderr)
        self.assertNotIn('alpha-guard', r.stderr)

    def test_sync_repairs_drift(self):
        self.run_sync()
        target = os.path.join(self.tmp, 'plugins', 'beta-guard', 'lib', 'bouncer_parse.py')
        with open(target, 'a') as f:
            f.write('\n# drift\n')
        self.assertEqual(0, self.run_sync().returncode)
        self.assertEqual(0, self.run_sync('--check').returncode)

    def test_vendored_copy_is_marked_as_generated(self):
        self.run_sync()
        target = os.path.join(self.tmp, 'plugins', 'alpha-guard', 'lib', 'bouncer_parse.py')
        with open(target) as f:
            head = f.read(400)
        self.assertIn('VENDORED COPY', head)
        self.assertIn('scripts/sync-lib.py', head)


if __name__ == '__main__':
    unittest.main()
