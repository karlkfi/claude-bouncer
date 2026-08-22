"""Assert the friction report still reads the reasons the guard now emits.

The two scripts meet at one string. The report parses the deny reason for the
gate that was hit and the rule that fired, so a change to how the guard opens
that reason breaks the report -- and breaks it silently: `GATE_RE` simply stops
matching, the run still succeeds, and every gate drops out of a table that goes
on printing. Nothing is raised and no count goes to zero, because the same run
reports categories and commands from other patterns.

So the reason here is taken from the guard rather than written out by hand: a
literal would pin the string this file was authored against, which is the one
thing that cannot regress.
"""
import importlib.util
import json
import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, 'scripts', 'bash-exit-status-guard.py')


def load_report():
    """Import the report script, whose filename is not a valid module name."""
    path = os.path.join(REPO, 'scripts', 'friction-report.py')
    spec = importlib.util.spec_from_file_location('friction_report', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fr = load_report()


def hook_reason(command, background=False):
    """The reason the hook denies `command` with, as the transcript records it."""
    proc = subprocess.run(
        [sys.executable, SCRIPT], timeout=30, capture_output=True, text=True,
        input=json.dumps({'tool_name': 'Bash', 'cwd': REPO,
                          'tool_input': {'command': command,
                                         'run_in_background': background}}))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip(), 'want a deny, got silence: %r' % command
    hso = json.loads(proc.stdout)['hookSpecificOutput']
    return hso['permissionDecisionReason']


class TestGateExtraction(unittest.TestCase):
    """`GATE_RE` against reasons from both sides of the prefix."""

    def test_the_gate_resolves_from_a_reason_the_guard_produced(self):
        m = fr.GATE_RE.match(hook_reason('make check | tail -5'))
        self.assertIsNotNone(m, 'the report can no longer name the gate')
        self.assertEqual('make check', m.group(1))

    def test_the_gate_resolves_for_every_rule(self):
        """One rule builds its reason without `with_log_path`, so all four are
        exercised rather than the piped one standing in for the set."""
        for name, cmd, bg in (
                ('piped', 'make check | tail -5', False),
                ('background', 'make check > c.log 2>&1; echo "EXIT=$?"', True),
                ('sequenced', 'make check; git push', False)):
            with self.subTest(name):
                m = fr.GATE_RE.match(hook_reason(cmd, bg))
                self.assertIsNotNone(m, 'no gate in the %s reason' % name)
                self.assertEqual('make check', m.group(1))

    def test_a_reason_recorded_before_the_prefix_still_resolves(self):
        """Transcripts are historical: 1.x and 2.0 denials open with the gate."""
        m = fr.GATE_RE.match('`make check` is piped into a filter, so this')
        self.assertIsNotNone(m, 'the report lost its own history')
        self.assertEqual('make check', m.group(1))

    def test_prose_naming_a_gate_is_not_read_as_one(self):
        """The anchor is what keeps this from matching mid-sentence, and the
        prefix is the only thing allowed in front of it."""
        for reason in ('denied because `make check` was piped',
                       'exit-status-guard says `make check` was piped'):
            with self.subTest(reason):
                self.assertIsNone(fr.GATE_RE.match(reason))


class TestCategories(unittest.TestCase):
    """The rule patterns search rather than match, so the prefix passes them --
    asserted rather than assumed, since the whole class is anchoring."""

    def test_every_rule_categorizes_from_a_reason_the_guard_produced(self):
        for cat, cmd, bg in (
                ('piped', 'make check | tail -5', False),
                ('pipestatus', 'make check | tail -5; exit ${PIPESTATUS[0]}', False),
                ('background', 'make check > c.log 2>&1; echo "EXIT=$?"', True),
                ('sequenced', 'make check; git push', False)):
            with self.subTest(cat):
                self.assertEqual(cat, fr.categorize(hook_reason(cmd, bg)))


if __name__ == '__main__':
    unittest.main()
