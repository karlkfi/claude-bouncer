"""Tests for the gate-parity check itself.

It asserts agreement between two files, so it stays green whenever they
happen to agree -- including once it has stopped reading one of them. Each
case plants one way a gate can go unrun in CI and requires the check to name
it, beside a clean pair it must pass.
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, 'scripts', 'gate-parity-check.py')

spec = importlib.util.spec_from_file_location('gate_parity_check', SCRIPT)
gpc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gpc)

MAKEFILE = """PYTHON ?= python3
PLUGINS := alpha-guard

check: lint-a lint-b suites

# A comment between targets.
lint-a:
\t$(PYTHON) scripts/a.py --strict

lint-b:
\t@$(PYTHON) scripts/b.py \\
\t  --long-flag

suites:
\t@for p in $(PLUGINS); do echo $$p; done
"""

WORKFLOW = """name: tests

on:
  pull_request:

jobs:
  root:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      # a comment
      - run: python3 scripts/a.py --strict
      - name: b
        run: |
          make lint-b

  alpha-guard:
    if: needs.changes.outputs.alpha_guard == 'true'
    runs-on: ubuntu-latest
    steps:
      - run: python3 -m unittest discover tests
        working-directory: plugins/alpha-guard
"""


class GateParityTests(unittest.TestCase):

    def problems(self, makefile=MAKEFILE, workflow=WORKFLOW):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        mk, wf = os.path.join(tmp, 'Makefile'), os.path.join(tmp, 'tests.yml')
        with open(mk, 'w') as fh:
            fh.write(makefile)
        with open(wf, 'w') as fh:
            fh.write(workflow)
        variables, targets = gpc.read_makefile(mk)
        return gpc.check_parity(variables, targets, gpc.read_workflow(wf),
                                carried={'suites': gpc._plugin_jobs})

    def test_a_matching_pair_passes(self):
        self.assertEqual([], self.problems())

    def test_a_gate_ci_never_runs_fails(self):
        bad = self.problems(makefile=MAKEFILE.replace(
            'check: lint-a', 'check: lint-c lint-a') + 'lint-c:\n\ttrue\n')
        self.assertEqual(1, len(bad), bad)
        self.assertIn('lint-c', bad[0])

    def test_a_gate_named_but_undefined_fails(self):
        bad = self.problems(makefile=MAKEFILE.replace(
            'check: lint-a', 'check: lint-z lint-a'))
        self.assertEqual(['lint-z: named by `check:` but has no recipe'], bad)

    def test_a_flag_dropped_from_ci_fails(self):
        """The recipe is matched whole, so CI running the script with a
        different flag set is not the same gate."""
        bad = self.problems(workflow=WORKFLOW.replace(
            'python3 scripts/a.py --strict', 'python3 scripts/a.py'))
        self.assertEqual(1, len(bad), bad)
        self.assertIn('lint-a', bad[0])

    def test_a_step_in_a_path_filtered_job_does_not_count(self):
        guarded = WORKFLOW.replace(
            '  root:\n    runs-on',
            "  root:\n    if: needs.changes.outputs.x == 'true'\n    runs-on")
        bad = self.problems(workflow=guarded)
        self.assertEqual(2, len(bad), bad)

    def test_a_step_outside_the_root_does_not_count(self):
        moved = WORKFLOW.replace(
            '      - run: python3 scripts/a.py --strict\n',
            '      - run: python3 scripts/a.py --strict\n'
            '        working-directory: plugins/alpha-guard\n')
        bad = self.problems(workflow=moved)
        self.assertEqual(1, len(bad), bad)
        self.assertIn('lint-a', bad[0])

    def test_a_multi_line_recipe_is_reached_only_through_make(self):
        bad = self.problems(workflow=WORKFLOW.replace('make lint-b',
                                                      'echo lint-b'))
        self.assertEqual(1, len(bad), bad)
        self.assertIn('lint-b', bad[0])

    def test_a_plugin_with_no_job_fails(self):
        bad = self.problems(makefile=MAKEFILE.replace(
            'PLUGINS := alpha-guard', 'PLUGINS := alpha-guard beta-guard'))
        self.assertEqual(1, len(bad), bad)
        self.assertIn('beta-guard', bad[0])

    STEP_A = '      - run: python3 scripts/a.py --strict\n'

    def lint_a_rewritten(self, replacement, workflow=WORKFLOW):
        assert self.STEP_A in workflow
        return self.problems(workflow=workflow.replace(self.STEP_A,
                                                       replacement))

    def test_a_step_that_cannot_fail_the_job_does_not_count(self):
        """Each rewrite leaves lint-a in the workflow while CI either skips it
        or reads its failure as green."""
        for rewrite in (
                self.STEP_A + '        if: false\n',
                '      - if: needs.changes.outputs.x == \'true\'\n'
                '        run: python3 scripts/a.py --strict\n',
                self.STEP_A + '        continue-on-error: true\n',
                '      - run: python3 scripts/a.py --strict || true\n',
                '      - run: echo python3 scripts/a.py --strict\n',
                '      - run: cd plugins/alpha-guard && '
                'python3 scripts/a.py --strict\n',
                '      - run: python3 scripts/a.py.orig --strict\n',
                '      - run: |\n          # python3 scripts/a.py --strict\n'
                '          true\n',
                '      - run: make -n lint-a\n',
                '      - run: make -C plugins/alpha-guard lint-a\n',
                '      - run: make lint-b && echo lint-a\n',
                '      - run: python3 scripts/a.py --strict | tail -5\n'):
            bad = self.lint_a_rewritten(rewrite)
            self.assertEqual(['lint-a'], [b.split(':')[0] for b in bad],
                             rewrite)

    def test_a_block_or_continued_command_still_counts(self):
        for rewrite in (
                '      - run: |-\n          python3 scripts/a.py --strict\n',
                '      - run: |\n          python3 scripts/a.py \\\n'
                '            --strict\n',
                '      - run: make lint-a lint-b\n'):
            self.assertEqual([], self.lint_a_rewritten(rewrite), rewrite)

    def test_a_job_that_cannot_fail_the_run_does_not_count(self):
        soft = WORKFLOW.replace('  root:\n    runs-on',
                                '  root:\n    continue-on-error: true\n'
                                '    runs-on')
        self.assertEqual(2, len(self.problems(workflow=soft)))

    def test_a_job_needing_a_path_filtered_job_does_not_count(self):
        moved = WORKFLOW.replace(self.STEP_A, '') + (
            '\n  late:\n    needs: [alpha-guard]\n    runs-on: ubuntu-latest\n'
            '    steps:\n      - run: python3 scripts/a.py --strict\n')
        bad = self.problems(workflow=moved)
        self.assertEqual(['lint-a'], [b.split(':')[0] for b in bad])

    def test_a_block_list_needs_is_read(self):
        moved = WORKFLOW.replace(self.STEP_A, '') + (
            '\n  late:\n    needs:\n      - root\n      - alpha-guard\n'
            '    runs-on: ubuntu-latest\n'
            '    steps:\n      - run: python3 scripts/a.py --strict\n')
        bad = self.problems(workflow=moved)
        self.assertEqual(['lint-a'], [b.split(':')[0] for b in bad])

    def test_a_workflow_default_working_directory_is_read(self):
        moved = WORKFLOW.replace('\njobs:\n', '\ndefaults:\n  run:\n'
                                 '    working-directory: plugins/alpha-guard\n'
                                 '\njobs:\n', 1)
        self.assertEqual(2, len(self.problems(workflow=moved)))

    def test_the_repository_passes(self):
        p = subprocess.run([sys.executable, SCRIPT], capture_output=True,
                           text=True)
        self.assertEqual(0, p.returncode, p.stdout + p.stderr)
        self.assertIn('ok (', p.stdout)


if __name__ == '__main__':
    unittest.main()
