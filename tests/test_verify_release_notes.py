"""Tests for the release-notes check.

The bug this script came back from was silent: it derived a tag from a notes
filename, which was right in the repository it was written for and names a tag
that will never exist here. Nothing about `v1.9.0` looks wrong until gh is
asked for it, so the derivation is what these tests pin -- through `--dry-run`,
which does it and stops before the network.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'scripts', 'verify-release-notes.sh')


def dry_run(*args):
    return subprocess.run(['bash', SCRIPT, '--dry-run'] + list(args),
                          capture_output=True, text=True, cwd=ROOT)


def rows(proc):
    """(notes path, tag here, tag there, repo there) per line."""
    return [line.split('\t') for line in proc.stdout.splitlines() if line]


def split_row(path):
    plugin = path.split('/')[1]
    version = path.rsplit('/', 1)[1][:-len('.md')]
    return plugin, version


def notes_on_disk():
    found = []
    for plugin in sorted(os.listdir(os.path.join(ROOT, 'plugins'))):
        releases = os.path.join(ROOT, 'plugins', plugin, 'docs', 'releases')
        if not os.path.isdir(releases):
            continue
        for name in sorted(os.listdir(releases)):
            if name.startswith('v') and name.endswith('.md'):
                found.append((plugin, name[:-len('.md')]))
    return found


class DerivationTests(unittest.TestCase):
    def test_every_notes_file_is_checked(self):
        proc = dry_run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(notes_on_disk(),
                         [split_row(r[0]) for r in rows(proc)])

    def test_the_tag_here_carries_the_plugin(self):
        # The whole bug: a bare vX.Y.Z names no plugin and is never created here.
        for path, here, there, repo in rows(dry_run()):
            plugin, version = split_row(path)
            self.assertEqual('%s/%s' % (plugin, version), here)
            self.assertEqual(version, there)
            self.assertEqual('karlkfi/claude-%s' % plugin, repo)

    def test_a_named_plugin_narrows_the_run(self):
        proc = dry_run('branch-guard')
        self.assertEqual(0, proc.returncode, proc.stderr)
        listed = rows(proc)
        self.assertTrue(listed, 'branch-guard has notes files to check')
        for path, _, _, repo in listed:
            self.assertTrue(path.startswith('plugins/branch-guard/'), path)
            self.assertEqual('karlkfi/claude-branch-guard', repo)

    def test_an_unknown_plugin_is_refused(self):
        proc = dry_run('no-such-guard')
        self.assertEqual(2, proc.returncode, proc.stdout)
        self.assertIn('no such plugin', proc.stderr)


class EmptyRunTests(unittest.TestCase):
    """A check that compared nothing must not report success."""

    def test_no_notes_anywhere_exits_nonzero(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, 'scripts'))
        os.makedirs(os.path.join(tmp, 'plugins', 'alpha-guard'))
        shutil.copy(SCRIPT, os.path.join(tmp, 'scripts', 'verify-release-notes.sh'))
        proc = subprocess.run(
            ['bash', os.path.join(tmp, 'scripts', 'verify-release-notes.sh'), '--dry-run'],
            capture_output=True, text=True)
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn('no release notes found', proc.stderr)


class SyntaxTests(unittest.TestCase):
    def test_script_parses(self):
        proc = subprocess.run(['bash', '-n', SCRIPT], capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stderr)


if __name__ == '__main__':
    unittest.main()
