"""Tests for the version-agreement gate itself.

The gate asserts a property of the tree, so it stays green whenever the tree
happens to be consistent -- including once it has quietly stopped reading one
of the three files. These plant each disagreement in turn and require the gate
to report it, because a check that cannot go red passes forever and the first
bump it would have caught ships.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'scripts', 'version-check.py')

README = """# claude-bouncer

## The guards

| Plugin | Version | What it stops |
| --- | --- | --- |
%s

More prose.
"""

BOTH = {'alpha-guard': '1.0.0', 'beta-guard': '2.0.0'}


class VersionCheckGateTests(unittest.TestCase):
    def setUp(self):
        """A throwaway copy of the layout, so a test never edits the real tree."""
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, 'scripts'))
        shutil.copy(SCRIPT, os.path.join(self.tmp, 'scripts', 'version-check.py'))

    def write_tree(self, plugin=None, marketplace=None, readme=None):
        """Write the three sources; each defaults to alpha 1.0.0, beta 2.0.0."""
        plugin = BOTH if plugin is None else plugin
        marketplace = BOTH if marketplace is None else marketplace
        readme = BOTH if readme is None else readme

        # Every plugin gets a directory: the directory list is what the gate
        # iterates, so one missing its plugin.json must still be judged.
        for name in BOTH:
            os.makedirs(os.path.join(self.tmp, 'plugins', name), exist_ok=True)
        for name, version in plugin.items():
            d = os.path.join(self.tmp, 'plugins', name, '.claude-plugin')
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, 'plugin.json'), 'w') as f:
                json.dump({'name': name, 'version': version}, f)

        d = os.path.join(self.tmp, '.claude-plugin')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'marketplace.json'), 'w') as f:
            json.dump({'plugins': [{'name': n, 'version': v}
                                   for n, v in sorted(marketplace.items())]}, f)

        rows = '\n'.join('| [%s](plugins/%s) | %s | what it stops |' % (n, n, v)
                         for n, v in sorted(readme.items()))
        with open(os.path.join(self.tmp, 'README.md'), 'w') as f:
            f.write(README % rows)

    def run_check(self):
        return subprocess.run(
            [sys.executable, os.path.join(self.tmp, 'scripts', 'version-check.py')],
            capture_output=True, text=True)

    def test_agreement_passes(self):
        self.write_tree()
        r = self.run_check()
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assertNotIn('DIFF', r.stdout)

    def test_stale_readme_row_fails(self):
        """The one measured to get missed: both JSON files bumped, README not."""
        bumped = {'alpha-guard': '1.1.0', 'beta-guard': '2.0.0'}
        self.write_tree(plugin=bumped, marketplace=bumped)
        r = self.run_check()
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertIn('alpha-guard', r.stderr)
        self.assertNotIn('beta-guard', r.stderr)
        self.assertIn('README.md=1.0.0', r.stdout)

    def test_stale_marketplace_entry_fails(self):
        """The one that ships nothing: `claude plugin update` compares this."""
        bumped = {'alpha-guard': '1.0.0', 'beta-guard': '2.1.0'}
        self.write_tree(plugin=bumped, readme=bumped)
        r = self.run_check()
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertIn('beta-guard', r.stderr)
        self.assertIn('marketplace.json=2.0.0', r.stdout)

    def test_stale_plugin_json_fails(self):
        bumped = {'alpha-guard': '1.1.0', 'beta-guard': '2.0.0'}
        self.write_tree(marketplace=bumped, readme=bumped)
        r = self.run_check()
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertIn('alpha-guard', r.stderr)
        self.assertIn('plugin.json=1.0.0', r.stdout)

    def test_missing_marketplace_entry_fails(self):
        """A plugin nobody listed is absence, not agreement."""
        self.write_tree(marketplace={'alpha-guard': '1.0.0'})
        r = self.run_check()
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertIn('beta-guard', r.stderr)
        self.assertIn('marketplace.json=-', r.stdout)

    def test_missing_readme_row_fails(self):
        self.write_tree(readme={'alpha-guard': '1.0.0'})
        r = self.run_check()
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertIn('beta-guard', r.stderr)
        self.assertIn('README.md=-', r.stdout)

    def test_missing_plugin_json_fails(self):
        self.write_tree(plugin={'alpha-guard': '1.0.0'})
        r = self.run_check()
        self.assertEqual(1, r.returncode, r.stdout + r.stderr)
        self.assertIn('beta-guard', r.stderr)
        self.assertIn('plugin.json=-', r.stdout)


class RealTreeTests(unittest.TestCase):
    def test_the_repository_agrees(self):
        """The assertion `make check` is actually making about this tree."""
        r = subprocess.run([sys.executable, SCRIPT], cwd=ROOT,
                           capture_output=True, text=True)
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)


if __name__ == '__main__':
    unittest.main()
