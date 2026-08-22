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
from importlib import util

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

        rows = '\n'.join(self.readme_row(n, v)
                         for n, v in sorted(readme.items()))
        with open(os.path.join(self.tmp, 'README.md'), 'w') as f:
            f.write(README % rows)

    def readme_row(self, name, version):
        return '| [%s](plugins/%s) | %s | what it stops |' % (name, name, version)

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


class IconColumnGateTests(VersionCheckGateTests):
    """Every case above, against the row shape the README actually ships.

    The guards table leads each row with the plugin's icon, so the version is
    no longer the second cell. Running the whole suite against that shape is
    what stops a row regex that only reads the bare form from passing here.
    """

    def readme_row(self, name, version):
        return ('| <img src="plugins/%s/docs/img/favicon-48.png" width="24" alt=""> '
                '| [%s](plugins/%s) | %s | what it stops |'
                % (name, name, name, version))


class RealTreeTests(unittest.TestCase):
    def test_the_repository_agrees(self):
        """The assertion `make check` is actually making about this tree."""
        r = subprocess.run([sys.executable, SCRIPT], cwd=ROOT,
                           capture_output=True, text=True)
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)


class ImportableSurfaceTests(unittest.TestCase):
    """`read_sources` and `versions` are consumed by scripts/release.py, so a
    change to their shape has to fail here rather than in the release path."""

    @classmethod
    def setUpClass(cls):
        spec = util.spec_from_file_location('version_check', SCRIPT)
        cls.mod = util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_read_sources_names_the_three_places_in_report_order(self):
        labels = [label for label, _ in self.mod.read_sources()]
        self.assertEqual(['plugin.json', 'marketplace.json', 'README.md'], labels)

    def test_versions_returns_one_value_per_place(self):
        found = self.mod.versions('workspace-guard')
        self.assertEqual({'plugin.json', 'marketplace.json', 'README.md'},
                         set(found))
        self.assertTrue(self.mod.agree(found), found)

    def test_an_unknown_plugin_is_missing_everywhere(self):
        found = self.mod.versions('no-such-guard')
        self.assertEqual([self.mod.MISSING] * 3, list(found.values()))
        self.assertFalse(self.mod.agree(found))

    def test_sources_can_be_read_once_and_reused(self):
        sources = self.mod.read_sources()
        self.assertEqual(self.mod.versions('prod-guard'),
                         self.mod.versions('prod-guard', sources))


if __name__ == '__main__':
    unittest.main()
