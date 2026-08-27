"""Tests for the install-reference gate itself.

The gate asserts a property of the READMEs, so it stays green whenever they
happen to be correct -- including once it has quietly stopped reading them.
These plant each of the shapes that survived the consolidation pass and
require the gate to report it, and plant the references the repository keeps
on purpose and require it to stay quiet, because a check that fires on the
`pipe-guard` migration block or on the pr-sentinel roster gets overridden and
then means nothing.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'scripts', 'install-ref-check.py')

MANIFEST = {
    'name': 'claude-bouncer',
    'owner': {'name': 'karlkfi'},
    'plugins': [{'name': 'workspace-guard'}, {'name': 'prod-guard'}],
}

# The root README carries the migration section the gate holds an exclusion
# for; prod-guard's carries the matching-rules example. Both are real.
ROOT_README = """# claude-bouncer

The five plugins were published from `karlkfi/claude-workspace-guard` and its four
siblings. Those repositories are being retired.
"""

PROD_README = """# prod-guard

A vetted allowlist entry can clear a built-in prod heuristic -- the escape
hatch for false positives like a repo
slug that merely contains `prod`, e.g. `karlkfi/claude-prod-guard`)
"""

GOOD = """# workspace-guard

[![release](https://img.shields.io/github/v/release/karlkfi/claude-bouncer?filter=workspace-guard%2F*)](https://github.com/karlkfi/claude-bouncer/releases?q=workspace-guard)

```
/plugin marketplace add karlkfi/claude-bouncer
/plugin install workspace-guard@claude-bouncer
```

2. Add `karlkfi/claude-bouncer` as a marketplace.

```json
{
  "extraKnownMarketplaces": {
    "claude-bouncer": {
      "source": { "source": "git", "url": "https://github.com/karlkfi/claude-bouncer.git" }
    }
  },
  "enabledPlugins": {
    "workspace-guard@claude-bouncer": true
  }
}
```

Compare it against the
[latest release](https://github.com/karlkfi/claude-bouncer/releases?q=workspace-guard).
"""


class InstallRefGateTests(unittest.TestCase):
    def setUp(self):
        """A throwaway copy of the layout, so a test never edits the real tree."""
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, 'scripts'))
        os.makedirs(os.path.join(self.tmp, '.claude-plugin'))
        os.makedirs(os.path.join(self.tmp, 'plugins', 'workspace-guard'))
        os.makedirs(os.path.join(self.tmp, 'plugins', 'prod-guard'))
        shutil.copy(SCRIPT, os.path.join(self.tmp, 'scripts'))
        with open(os.path.join(self.tmp, '.claude-plugin',
                               'marketplace.json'), 'w') as f:
            json.dump(MANIFEST, f)
        self.write_root(ROOT_README)
        self.write('prod-guard', PROD_README)
        self.write('workspace-guard', GOOD)

    def write_root(self, text):
        with open(os.path.join(self.tmp, 'README.md'), 'w') as f:
            f.write(text)

    def write(self, plugin, text):
        with open(os.path.join(self.tmp, 'plugins', plugin, 'README.md'),
                  'w') as f:
            f.write(text)

    def run_gate(self):
        return subprocess.run(
            [sys.executable,
             os.path.join(self.tmp, 'scripts', 'install-ref-check.py')],
            capture_output=True, text=True)

    def plant(self, old, new):
        """One substitution into the known-good README."""
        self.assertIn(old, GOOD)
        self.write('workspace-guard', GOOD.replace(old, new))
        return self.run_gate()

    def test_a_correct_readme_passes(self):
        proc = self.run_gate()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)

    def test_the_exclusions_are_reported_as_held_back(self):
        """A waiver nobody can count is a waiver reached for to hide drift."""
        proc = self.run_gate()
        self.assertIn('2 exclusion(s) held back', proc.stdout)

    # --- the four shapes the consolidation pass left behind ------------------

    def test_a_retired_repo_in_a_badge_is_reported(self):
        proc = self.plant(
            'release/karlkfi/claude-bouncer?filter=workspace-guard%2F*',
            'release/karlkfi/claude-workspace-guard')
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn('karlkfi/claude-workspace-guard is a retired repository',
                      proc.stderr)

    def test_a_retired_repo_in_a_desktop_step_is_reported(self):
        proc = self.plant('2. Add `karlkfi/claude-bouncer`',
                          '2. Add `karlkfi/claude-workspace-guard`')
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn('is a retired repository', proc.stderr)

    def test_a_retired_repo_in_a_latest_release_link_is_reported(self):
        proc = self.plant(
            '[latest release](https://github.com/karlkfi/claude-bouncer/releases?q=workspace-guard)',
            '[latest release](https://github.com/karlkfi/claude-workspace-guard/releases)')
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn('is a retired repository', proc.stderr)

    def test_a_marketplace_key_naming_the_plugin_is_reported(self):
        """The half a link checker could never see: every link here resolves."""
        proc = self.plant('"extraKnownMarketplaces": {\n    "claude-bouncer"',
                          '"extraKnownMarketplaces": {\n    "workspace-guard"')
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn('names marketplace workspace-guard, expected '
                      'claude-bouncer', proc.stderr)

    # --- the command forms ---------------------------------------------------

    def test_an_add_of_the_wrong_repo_is_reported(self):
        """A slug that is not retired, so only the add rule can catch it --
        otherwise this passes on the retired-repo rule and proves nothing."""
        proc = self.plant('marketplace add karlkfi/claude-bouncer',
                          'marketplace add someone/a-fork')
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn('marketplace add someone/a-fork, '
                      'expected karlkfi/claude-bouncer', proc.stderr)

    def test_an_install_from_the_old_marketplace_is_reported(self):
        proc = self.plant('/plugin install workspace-guard@claude-bouncer',
                          '/plugin install workspace-guard@workspace-guard')
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn('names marketplace workspace-guard', proc.stderr)

    def test_an_enabled_plugin_entry_is_read(self):
        proc = self.plant('"workspace-guard@claude-bouncer": true',
                          '"workspace-guard@prod-guard": true')
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn('names marketplace prod-guard', proc.stderr)

    def test_an_update_of_the_old_marketplace_is_reported(self):
        self.write('workspace-guard',
                   GOOD + '\n`/plugin marketplace update workspace-guard`\n')
        proc = self.run_gate()
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn('names marketplace workspace-guard', proc.stderr)

    # --- what the gate must not fire on --------------------------------------

    def test_an_uninstall_of_the_old_pair_passes(self):
        """exit-status-guard's 2.0.0 block, which is correct as written."""
        self.write('workspace-guard', GOOD + """
```
/plugin uninstall pipe-guard@pipe-guard
/plugin marketplace remove pipe-guard
```
""")
        proc = self.run_gate()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)

    def test_a_sibling_repo_link_passes(self):
        """pr-sentinel and pipe-guard are not plugins here, so not retired."""
        self.write('workspace-guard', GOOD + """
- [**pr-sentinel**](https://github.com/karlkfi/claude-pr-sentinel)
- ([#19](https://github.com/karlkfi/claude-pipe-guard/issues/19))
""")
        proc = self.run_gate()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)

    def test_a_stale_exclusion_is_an_error(self):
        """Reconciled in both directions: a waiver cannot outlive its line."""
        self.write('prod-guard', '# prod-guard\n\nNothing to exempt here.\n')
        proc = self.run_gate()
        self.assertEqual(1, proc.returncode, proc.stdout)
        self.assertIn('exclusion matches nothing', proc.stderr)


if __name__ == '__main__':
    unittest.main()
