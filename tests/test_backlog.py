"""Tests for the backlog store and the gate over it.

Two different claims. `StoreTests` asserts things about the tree: every item
names its plugin, and every link an item carries resolves — both of which the
consolidation could have broken silently, since `queue.py` knows nothing about
either. `GateTests` asserts the harder one, that `make backlog-lint` REPORTS a
defect when there is one; a linter never shown failing is not evidence that it
checks anything.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUE = os.path.join(ROOT, 'scripts', 'queue.py')
STORE = os.path.join(ROOT, 'docs', 'queue')

# The flags `make backlog-lint` runs. Kept here rather than shelling out to
# make, so a Windows runner can still exercise the gate.
STRICT = ['--strict', 'blocked-opener',
          '--strict', 'deferred-trigger',
          '--strict', 'empty-store']

PLUGINS = ['workspace-guard', 'branch-guard', 'prod-guard',
           'exit-status-guard', 'foreground-guard']
OWNERS = set(PLUGINS) | {'repo'}

ITEM = re.compile(r'^Q\d+\.md$')
LINK = re.compile(r'\]\(([^)]+)\)')
TARGET = re.compile(r'^target: (.+)$', re.M)


def lint(store, extra=()):
    return subprocess.run(
        [sys.executable, QUEUE, '--store', store, 'lint'] + STRICT + list(extra),
        capture_output=True, text=True)


def items(store):
    return sorted(n for n in os.listdir(store) if ITEM.match(n))


class StoreTests(unittest.TestCase):
    def test_the_gate_passes_on_the_real_store(self):
        p = lint(STORE)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_every_item_names_its_owner(self):
        """The one label this repo adds. Without it a single store holding five
        plugins' work is not filterable by the person who owns one of them."""
        for name in items(STORE):
            with open(os.path.join(STORE, name), encoding='utf-8') as fh:
                labels = re.findall(r'^ +- (.+)$', fh.read(), re.M)
            self.assertTrue(OWNERS & set(labels),
                            '%s carries no plugin label: %s' % (name, labels))

    def test_every_link_resolves(self):
        """Consolidation rebased every relative link onto docs/queue/. A broken
        one looks fine in the source and only fails when someone clicks."""
        for name in items(STORE):
            path = os.path.join(STORE, name)
            with open(path, encoding='utf-8') as fh:
                text = fh.read()
            dests = [m for m in LINK.findall(text)] + TARGET.findall(text)
            for dest in dests:
                dest = dest.strip()
                if dest.startswith(('http://', 'https://', '#')):
                    continue
                self.assertTrue(
                    os.path.exists(os.path.join(STORE, dest)),
                    '%s links %s, which does not resolve' % (name, dest))


class GateTests(unittest.TestCase):
    """One clean store per case, with a single defect introduced."""

    CLEAN = ('---\n'
             'id: Q1\n'
             'rank: a0\n'
             'labels:\n'
             '    - repo\n'
             'status: %s\n'
             'size: S\n'
             '---\n'
             '\n'
             '# A title\n'
             '\n'
             '%s\n')

    def store(self, status='ready', notes='An ordinary note.'):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, 'Q1.md'), 'w', encoding='utf-8') as fh:
            fh.write(self.CLEAN % (status, notes))
        return tmp

    def test_a_clean_store_passes(self):
        p = lint(self.store())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_a_deferred_item_with_no_trigger_fails(self):
        p = lint(self.store(status='deferred'))
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_a_deferred_item_with_a_trigger_passes(self):
        p = lint(self.store(status='deferred',
                            notes='**Demand:** somebody asks for it.'))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_a_blocked_item_that_does_not_say_what_it_waits_on_fails(self):
        p = lint(self.store(status='blocked'))
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_an_empty_store_fails(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        p = lint(tmp)
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_a_hand_typed_rank_fails(self):
        tmp = self.store()
        path = os.path.join(tmp, 'Q1.md')
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(text.replace('rank: a0', 'rank: 1.5'))
        p = lint(tmp)
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == '__main__':
    unittest.main()
