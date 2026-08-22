"""Tests for the release script.

Two halves, and the second is the one that is easy to skip. The pure functions
-- version arithmetic, the verdict -- are asserted directly. The file-writing
half runs the script as a subprocess against a throwaway tree, because what
matters there is that it REFUSES: a bump that writes two of three locations, or
a tag on a commit that never merged, is exactly what the script exists to make
impossible, and a check that has only ever succeeded has not shown it can.
"""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'scripts', 'release.py')

spec = importlib.util.spec_from_file_location('release', SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

MARKETPLACE = {
    'plugins': [
        {'name': 'alpha-guard', 'description': 'a', 'version': '1.2.3',
         'source': './plugins/alpha-guard'},
        {'name': 'beta-guard', 'description': 'b', 'version': '0.4.0',
         'source': './plugins/beta-guard'},
    ]
}

README = """# fake

| Plugin | Version | What it stops |
| --- | --- | --- |
| [alpha-guard](plugins/alpha-guard) | 1.2.3 | things |
| [beta-guard](plugins/beta-guard) | 0.4.0 | other things |
"""


class VersionArithmeticTests(unittest.TestCase):
    def test_levels(self):
        self.assertEqual('2.0.0', release.next_version('1.9.3', 'major'))
        self.assertEqual('1.10.0', release.next_version('1.9.3', 'minor'))
        self.assertEqual('1.9.4', release.next_version('1.9.3', 'patch'))
        self.assertEqual('2.5.2', release.next_version('2.5.1', '2.5.2'))

    def refuses(self, spec_):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit):
                release.next_version('1.9.3', spec_)
        return err.getvalue()

    def test_a_non_increase_is_refused(self):
        self.assertIn('not greater', self.refuses('1.9.3'))
        self.assertIn('not greater', self.refuses('1.0.0'))

    def test_an_unknown_level_is_refused(self):
        self.assertIn('major, minor, patch', self.refuses('moderate'))


class VerdictTests(unittest.TestCase):
    """The verdict is a starting point, so what it must not do is over-claim."""

    def test_no_delta_is_none(self):
        self.assertEqual('none', release.verdict([], []))

    def test_internal_churn_holds(self):
        commits = [('a', 'ci: run the suites from one workflow'),
                   ('b', 'docs: fix a typo'),
                   ('c', 'test(prod-guard): pin the override prefix')]
        self.assertEqual('hold', release.verdict(commits, []))

    def test_a_feat_releases(self):
        commits = [('a', 'docs: fix a typo'),
                   ('b', 'feat(lib): extract the shell parser')]
        self.assertEqual('release', release.verdict(commits, []))

    def test_an_answered_note_outranks_the_commit_types(self):
        """A `chore:` carrying a note is still a release -- the author said so."""
        commits = [('a', 'chore(guard): retune a message')]
        notes = [('7', 'Unanchored pkill patterns now deny.')]
        self.assertEqual('release', release.verdict(commits, notes))

    def test_none_and_the_unanswered_markers_do_not_count_as_notes(self):
        commits = [('a', 'chore(guard): retune a message')]
        for note in ('None', '!! UNANSWERED', '!! NO SECTION', '!! UNREADABLE'):
            self.assertEqual('hold', release.verdict(commits, [('7', note)]), note)


class TreeTests(unittest.TestCase):
    """The file-writing half, against a throwaway copy of the layout."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, 'scripts'))
        os.makedirs(os.path.join(self.tmp, '.claude-plugin'))
        shutil.copy(SCRIPT, os.path.join(self.tmp, 'scripts', 'release.py'))
        shutil.copy(os.path.join(ROOT, 'scripts', 'release-note.py'),
                    os.path.join(self.tmp, 'scripts', 'release-note.py'))
        # release.py reads the three version locations through this one; the
        # fixture is hermetic and will not reach the real tree's copy.
        shutil.copy(os.path.join(ROOT, 'scripts', 'version-check.py'),
                    os.path.join(self.tmp, 'scripts', 'version-check.py'))
        self.write('.claude-plugin/marketplace.json', json.dumps(MARKETPLACE, indent=2))
        self.write('README.md', README)
        for entry in MARKETPLACE['plugins']:
            self.write('plugins/%s/.claude-plugin/plugin.json' % entry['name'],
                       json.dumps({'name': entry['name'], 'version': entry['version']}, indent=2))
        subprocess.run(['git', 'init', '-q', self.tmp], check=True)

    def write(self, rel, text):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)

    def read(self, rel):
        with open(os.path.join(self.tmp, rel)) as f:
            return f.read()

    def run_release(self, *args):
        return subprocess.run(
            [sys.executable, os.path.join(self.tmp, 'scripts', 'release.py')] + list(args),
            capture_output=True, text=True, cwd=self.tmp)

    def test_bump_writes_all_three_locations(self):
        r = self.run_release('bump', 'alpha-guard', 'minor')
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        self.assertIn('"version": "1.3.0"', self.read('plugins/alpha-guard/.claude-plugin/plugin.json'))
        self.assertIn('"version": "1.3.0"', self.read('.claude-plugin/marketplace.json'))
        self.assertIn('| [alpha-guard](plugins/alpha-guard) | 1.3.0 |', self.read('README.md'))

    def test_bump_leaves_the_other_plugin_alone(self):
        """One `"version"` per file is the trap: marketplace.json holds five."""
        self.run_release('bump', 'alpha-guard', 'major')
        marketplace = json.loads(self.read('.claude-plugin/marketplace.json'))
        self.assertEqual('2.0.0', marketplace['plugins'][0]['version'])
        self.assertEqual('0.4.0', marketplace['plugins'][1]['version'])
        self.assertIn('| [beta-guard](plugins/beta-guard) | 0.4.0 |', self.read('README.md'))

    def test_bump_refuses_when_the_locations_disagree(self):
        self.write('README.md', README.replace('| 1.2.3 |', '| 1.2.2 |'))
        r = self.run_release('bump', 'alpha-guard', 'patch')
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertIn('disagree', r.stderr)
        # and it wrote nothing on the way to refusing
        self.assertIn('"version": "1.2.3"', self.read('plugins/alpha-guard/.claude-plugin/plugin.json'))

    def test_bump_refuses_a_readme_row_it_cannot_find(self):
        """A README rewrite that drops the table must not pass silently."""
        self.write('README.md', '# fake\n\nno table here\n')
        r = self.run_release('bump', 'alpha-guard', 'patch')
        self.assertEqual(1, r.returncode, r.stdout)

    def test_an_unknown_plugin_is_refused(self):
        r = self.run_release('bump', 'gamma-guard', 'patch')
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertIn('alpha-guard, beta-guard', r.stderr)

    def test_tag_refuses_without_a_notes_file(self):
        r = self.run_release('tag', 'alpha-guard')
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertIn('no notes at', r.stderr)

    def test_tag_refuses_when_head_is_not_origin_main(self):
        self.write('plugins/alpha-guard/docs/releases/v1.2.3.md', 'body\n')
        r = self.run_release('tag', 'alpha-guard')
        self.assertEqual(1, r.returncode, r.stdout)
        self.assertNotIn('created', r.stdout)

    def commit_all(self, subject):
        subprocess.run(['git', '-C', self.tmp, 'add', '-A'], check=True)
        subprocess.run(['git', '-C', self.tmp, '-c', 'user.email=t@t', '-c', 'user.name=t',
                        'commit', '-qm', subject], check=True)

    def test_status_reports_every_plugin(self):
        self.commit_all('feat(alpha-guard): a thing')
        r = self.run_release('status', '--json')
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        report = json.loads(r.stdout)
        self.assertEqual(['alpha-guard', 'beta-guard'], [i['plugin'] for i in report])
        self.assertEqual('1.2.3', report[0]['version'])
        self.assertTrue(report[0]['first_release_here'])

    def test_status_names_all_three_locations(self):
        """`locations` is what the DISAGREE line and the report show, and since
        `version` is no longer derived from it, nothing else notices it quietly
        dropping a source."""
        self.commit_all('feat(alpha-guard): a thing')
        r = self.run_release('status', '--json')
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        report = json.loads(r.stdout)
        self.assertEqual([['plugin.json', '1.2.3'],
                          ['marketplace.json', '1.2.3'],
                          ['README.md', '1.2.3']],
                         report[0]['locations'])


if __name__ == '__main__':
    unittest.main()
