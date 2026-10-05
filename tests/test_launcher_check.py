"""Tests for the launcher check itself.

Each case builds a throwaway repository with five launcher copies, plants one
way a copy can fall behind, and requires the check to name that copy. A clean
set must pass, or a check that fails everything would pass these too.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'scripts', 'launcher-check.py')

BODY = b": << 'CMDBLOCK'\n@echo off\nCMDBLOCK\nexec python3 \"$@\"\n"
PLACES = ['plugins/alpha/scripts', 'plugins/beta/scripts',
          'plugins/gamma/hooks']


class LauncherCheckTests(unittest.TestCase):

    def repo(self, bodies=None, pinned=PLACES, modes=None):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        git = ['git', '-C', tmp, '-c', 'core.autocrlf=false']
        subprocess.run(git + ['init', '-q'], check=True)
        for place in PLACES:
            os.makedirs(os.path.join(tmp, place))
            path = os.path.join(tmp, place, 'run-python-hook.cmd')
            with open(path, 'wb') as fh:
                fh.write((bodies or {}).get(place, BODY))
            if place in pinned:
                plugin = os.path.join(tmp, *place.split('/')[:2])
                with open(os.path.join(plugin, '.gitattributes'), 'w') as fh:
                    fh.write('*.cmd text eol=lf\n')
        subprocess.run(git + ['add', '-A'], check=True)
        for place in PLACES:
            mode = (modes or {}).get(place, '+x')
            subprocess.run(git + ['update-index', '--chmod=' + mode,
                                  place + '/run-python-hook.cmd'], check=True)
        return tmp

    def run_check(self, root):
        return subprocess.run([sys.executable, SCRIPT, root],
                              capture_output=True, text=True)

    def test_identical_pinned_executable_copies_pass(self):
        p = self.run_check(self.repo())
        self.assertEqual(0, p.returncode, p.stdout + p.stderr)
        self.assertIn('ok (3 copies)', p.stdout)

    def test_a_copy_that_differs_is_named(self):
        p = self.run_check(self.repo(
            bodies={'plugins/beta/scripts': BODY + b'# drifted\n'}))
        self.assertEqual(1, p.returncode, p.stdout)
        drift = [l for l in p.stdout.splitlines() if 'beta' in l]
        self.assertEqual(1, len(drift), p.stdout)
        self.assertNotIn('alpha', drift[0])

    def test_a_copy_not_executable_in_the_index_is_named(self):
        p = self.run_check(self.repo(modes={'plugins/gamma/hooks': '-x'}))
        self.assertEqual(1, p.returncode, p.stdout)
        self.assertIn('plugins/gamma/hooks/run-python-hook.cmd is 100644',
                      p.stdout)

    def test_a_copy_with_no_lf_pin_is_named(self):
        p = self.run_check(self.repo(pinned=PLACES[1:]))
        self.assertEqual(1, p.returncode, p.stdout)
        self.assertIn('plugins/alpha/scripts/run-python-hook.cmd has eol '
                      'unspecified', p.stdout)

    def test_a_carriage_return_is_named(self):
        crlf = BODY.replace(b'\n', b'\r\n')
        p = self.run_check(self.repo(bodies={p: crlf for p in PLACES}))
        self.assertEqual(1, p.returncode, p.stdout)
        self.assertEqual(3, p.stdout.count('holds a carriage return'),
                         p.stdout)

    def test_no_copies_fails_rather_than_passing(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        subprocess.run(['git', '-C', tmp, 'init', '-q'], check=True)
        p = self.run_check(tmp)
        self.assertEqual(1, p.returncode, p.stdout)
        self.assertIn('nothing was checked', p.stdout)

    def test_the_repository_passes(self):
        p = self.run_check(ROOT)
        self.assertEqual(0, p.returncode, p.stdout + p.stderr)
        self.assertIn('ok (5 copies)', p.stdout)


if __name__ == '__main__':
    unittest.main()
