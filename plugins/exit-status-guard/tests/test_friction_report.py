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
import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

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

# The hook reads these ahead of the payload's cwd, so one exported in the shell
# running the suite would hand every hook call a registry no test wrote (Q162).
# The registry names are the guard's REGISTRY_ENV_VARS.
ISOLATED_ENV = ('CLAUDE_PROJECT_DIR', 'EXIT_STATUS_GUARD_REGISTRY',
                'PIPE_GUARD_REGISTRY')


def setUpModule():
    patcher = mock.patch.dict(os.environ)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    for name in ISOLATED_ENV:
        os.environ.pop(name, None)


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


PREFIX = 'PreToolUse:Bash hook error: '
TS = '2026-07-01T12:00:00Z'


def deny_records(tuid, reason, command='make check | tail -5', ts=TS):
    """The two records a denied Bash call leaves: the tool_use and the error
    tool_result carrying the reason. There is no attachment (see DENY_TEXT)."""
    return tuple(
        {'timestamp': ts, 'cwd': '/home/u/proj', 'message': {'content': [block]}}
        for block in (
            {'type': 'tool_use', 'name': 'Bash', 'id': tuid,
             'input': {'command': command}},
            {'type': 'tool_result', 'tool_use_id': tuid, 'is_error': True,
             'content': reason}))


def attachment(tuid, stdout, command='command hook 7'):
    return {'timestamp': TS, 'cwd': '/home/u/proj', 'attachment': {
        'type': 'hook_success', 'hookName': 'PreToolUse:Bash',
        'toolUseID': tuid, 'command': command, 'stdout': stdout}}


def replay(rec):
    """`rec` as an `api-request-blob` restates it: the message, and neither the
    `uuid` nor the `timestamp` (nor the cwd) of the record it came from."""
    return {'type': 'api-request-blob', 'hash': 'h', 'sessionId': 's',
            'message': rec['message']}


def scan(records, plugin=None, cutoff=None):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 's.jsonl')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.writelines(json.dumps(r) + '\n' for r in records)
        return fr.scan([path], plugin or fr.THIS_GUARD, cutoff, '')[0]


class TestDenyRecovery(unittest.TestCase):
    """Every verdict here is a deny, and a deny leaves no attachment, so the
    tool result is the only place this guard's decisions are recorded."""

    @classmethod
    def setUpClass(cls):
        cls.reason = hook_reason('make check | tail -5')

    def test_every_recorded_form_is_recovered(self):
        for form in ('%s', PREFIX + '%s', 'Error: %s'):
            with self.subTest(form):
                got = scan(deny_records('toolu_1', form % self.reason))
                self.assertEqual([('deny', self.reason)],
                                 [(d['decision'], d['reason']) for d in got])
                report = fr.build_report(got, raw=True)
                self.assertEqual({'make check': 1}, report['gates'])
                self.assertEqual({'piped': 1}, report['categories'])
                self.assertEqual({'make check | tail -5': 1}, report['commands'])

    def test_a_denial_recorded_before_the_opener_is_recovered(self):
        got = scan(deny_records(
            'toolu_1', "`make check` is piped into a filter, so this call's "
                       "exit status is the filter's."))
        self.assertEqual(['deny'], [d['decision'] for d in got])
        self.assertEqual(fr.THIS_GUARD, got[0]['plugin'])

    def test_ordinary_failures_are_not_denials(self):
        for text in ('Exit code 1', "error: pathspec 'x' did not match",
                     PREFIX + "error: pathspec 'x' did not match",
                     '`make check` failed', 'quoting ' + self.reason):
            with self.subTest(text):
                self.assertEqual([], scan(deny_records('toolu_1', text), 'all'))
        use, result = deny_records('toolu_1', self.reason)
        result['message']['content'][0]['is_error'] = False
        self.assertEqual([], scan([use, result], 'all'))

    def test_a_sibling_is_named_by_its_own_opener(self):
        records = deny_records('toolu_1', PREFIX + 'prod-guard: blocked')
        self.assertEqual([], scan(records))
        self.assertEqual(['prod-guard'],
                         [d['plugin'] for d in scan(records, 'all')])

    def test_a_denial_on_a_non_bash_call_is_ignored(self):
        self.assertEqual([], scan(deny_records('toolu_1', self.reason)[1:]))

    def test_an_unnamed_hook_is_named_by_its_reason(self):
        """Some attachments record `command hook 7` where the script path was.
        A declined ask comes back as an error worded like a deny, so missing
        its attachment would count it as one."""
        ask = 'prod-guard: confirm'
        use, result = deny_records('toolu_1', PREFIX + ask)
        att = attachment('toolu_1', json.dumps({'hookSpecificOutput': {
            'permissionDecision': 'ask', 'permissionDecisionReason': ask}}))
        self.assertEqual([('prod-guard', 'ask')],
                         [(d['plugin'], d['decision'])
                          for d in scan([use, att, result], 'all')])

    def test_an_unattributable_hook_is_left_alone(self):
        for stdout in (
                '{"hookSpecificOutput": {"permissionDecisionReason": null}}',
                '{"hookSpecificOutput": {"permissionDecision": "allow",'
                ' "permissionDecisionReason": "error: not a guard"}}',
                '{"systemMessage": "hi"}', '["not", "a", "verdict"]', 'plain'):
            with self.subTest(stdout):
                self.assertEqual([], scan([attachment('toolu_1', stdout)], 'all'))


class TestRepeats(unittest.TestCase):
    """A transcript restates its own records; each denial counts once."""

    REASON = PREFIX + 'exit-status-guard: `make check` is piped'

    def test_a_replayed_denial_counts_once(self):
        use, result = deny_records('toolu_1', self.REASON)
        for order in ((use, result, replay(use), replay(result)),
                      (replay(use), replay(result), use, result)):
            with self.subTest(order[0].get('type')):
                self.assertEqual(['/home/u/proj'],
                                 [d['cwd'] for d in scan(order)])

    def test_a_replay_does_not_pass_the_window(self):
        use, result = deny_records('toolu_1', self.REASON,
                                   ts='2020-01-01T00:00:00Z')
        cutoff = dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc)
        self.assertEqual([], scan([use, result, replay(use), replay(result)],
                                  cutoff=cutoff))

    def test_a_record_written_twice_counts_once(self):
        records = [
            *deny_records('toolu_1', self.REASON),
            attachment('toolu_2', '', 'run-python-hook.cmd '
                                      'bash-exit-status-guard.py')]
        for n, rec in enumerate(records):
            rec['uuid'] = 'u-%d' % n
        self.assertEqual(['defer', 'deny'],
                         sorted(d['decision'] for d in scan(records * 2)))


if __name__ == '__main__':
    unittest.main()
