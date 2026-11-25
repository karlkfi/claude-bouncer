"""Tests for the shared transcript reader behind a friction report.

Each test writes a synthetic transcript and reads it back. What a reason means
is each guard's own question and is tested beside its report.
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'lib'))
import bouncer_friction as bf  # noqa: E402

TS = '2026-07-01T12:00:00.000Z'
REASON = "branch-guard: `git restore` discards working-tree changes"
WRAPPED = 'PreToolUse:Bash hook error: ' + REASON
HOOK = '"${CLAUDE_PLUGIN_ROOT}/hooks/run-python-hook.cmd" branch-guard.py'


def record(ts=TS, **fields):
    rec = {'cwd': '/home/u/proj', 'sessionId': 's1', **fields}
    if ts:
        rec['timestamp'] = ts
    return rec


def call(cid, command='git status', tool='Bash', **fields):
    key = 'command' if tool == 'Bash' else 'file_path'
    return record(message={'content': [
        {'type': 'tool_use', 'name': tool, 'id': cid,
         'input': {key: command}}]}, **fields)


def result(cid, text, is_error=True, **fields):
    return record(message={'content': [
        {'type': 'tool_result', 'tool_use_id': cid, 'is_error': is_error,
         'content': text}]}, **fields)


def attachment(cid, decision=None, reason='', hook=HOOK, tool='Bash', **att):
    stdout = '' if decision is None else json.dumps({'hookSpecificOutput': {
        'permissionDecision': decision, 'permissionDecisionReason': reason}})
    return record(attachment=dict({
        'type': 'hook_success', 'hookName': 'PreToolUse:' + tool,
        'command': hook, 'toolUseID': cid, 'stdout': stdout}, **att))


class ReaderCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def read(self, *transcripts, **kw):
        """(calls, decisions) over transcripts, each a list of records."""
        paths = []
        for i, recs in enumerate(transcripts):
            paths.append(os.path.join(self.tmp, '%d.jsonl' % i))
            with open(paths[-1], 'w', encoding='utf-8') as f:
                f.writelines(r if isinstance(r, str) else json.dumps(r) + '\n'
                             for r in recs)
        events = list(bf.read_events(paths, **kw))
        return ([e for e in events if isinstance(e, bf.Call)],
                [e for e in events if isinstance(e, bf.Decision)])


class DenyTests(ReaderCase):
    def test_bare_and_wrapped_denials_read_alike(self):
        for text in (REASON, 'Error: ' + REASON, WRAPPED,
                     [{'type': 'text', 'text': WRAPPED}]):
            with self.subTest(text=text):
                _, got = self.read([call('t1'), result('t1', text)])
                self.assertEqual(
                    [(d.plugin, d.decision, d.reason, d.tool, d.command)
                     for d in got],
                    [('branch-guard', 'deny', REASON, 'Bash', 'git status')])

    def test_wrapped_denial_of_another_tool(self):
        text = 'PreToolUse:NotebookEdit hook error: ' + REASON
        _, got = self.read([call('t1', tool='NotebookEdit'), result('t1', text)])
        self.assertEqual([(d.tool, d.command) for d in got],
                         [('NotebookEdit', '')])

    def test_a_plugin_outside_the_guard_shape(self):
        _, got = self.read([call('t1'), result('t1', 'pr-sentinel: no')])
        self.assertEqual([d.plugin for d in got], ['pr-sentinel'])

    def test_not_a_denial(self):
        for text, is_error in ((REASON, False), ('Exit code 1\n' + REASON, True),
                               ('error: boom', True), ('', True), (None, True),
                               ('PreToolUse:Bash hook error: nope', True)):
            with self.subTest(text=text):
                _, got = self.read([call('t1'), result('t1', text, is_error)])
                self.assertEqual(got, [])

    def test_a_result_no_call_joins_to(self):
        _, got = self.read([call('t1'), result('t2', REASON)])
        self.assertEqual(got, [])

    def test_a_declined_ask_is_one_ask(self):
        _, got = self.read([call('t1'), attachment('t1', 'ask', REASON),
                            result('t1', WRAPPED)])
        self.assertEqual([d.decision for d in got], ['ask'])

    def test_two_guards_denying_one_call(self):
        _, got = self.read([call('t1'), result('t1', REASON),
                            result('t1', 'prod-guard: x')])
        self.assertEqual([d.plugin for d in got], ['branch-guard', 'prod-guard'])


class RepeatTests(ReaderCase):
    EVENTS = [call('t1', uuid='u1'), result('t1', WRAPPED, uuid='u2'),
              call('t2', uuid='u3'),
              dict(attachment('t2', 'ask', REASON), uuid='u4')]

    def counts(self, *transcripts):
        calls, got = self.read(*transcripts)
        return len(calls), sorted(d.decision for d in got)

    def test_control(self):
        self.assertEqual(self.counts(self.EVENTS), (2, ['ask', 'deny']))

    def test_repeated_in_one_transcript(self):
        self.assertEqual(self.counts(self.EVENTS * 2), (2, ['ask', 'deny']))

    def test_copied_into_another_transcript(self):
        self.assertEqual(self.counts(self.EVENTS, self.EVENTS),
                         (2, ['ask', 'deny']))

    def test_a_repeat_under_a_new_uuid(self):
        again = [dict(r, uuid=r['uuid'] + 'x') for r in self.EVENTS]
        self.assertEqual(self.counts(self.EVENTS + again), (2, ['ask', 'deny']))

    def test_an_api_request_blob_adds_nothing(self):
        blob = [dict(result('t1', WRAPPED, ts=None), type='api-request-blob'),
                dict(call('t9', ts=None), type='api-request-blob')]
        self.assertEqual(self.counts(self.EVENTS + blob), (2, ['ask', 'deny']))

    def test_untimestamped_records_are_dropped(self):
        for ts in (None, '', 'yesterday', 7):
            with self.subTest(ts=ts):
                recs = [dict(r, timestamp=ts) for r in self.EVENTS]
                self.assertEqual(self.counts(recs), (0, []))

    def test_a_second_guard_on_the_call_still_counts(self):
        _, got = self.read([
            call('t1'), attachment('t1', 'ask', REASON),
            attachment('t1', 'allow', 'x', hook='/x/bash-prod-guard.py')])
        self.assertEqual([d.plugin for d in got], ['branch-guard', 'prod-guard'])


class AttachmentTests(ReaderCase):
    def test_tool_and_session_come_through(self):
        _, got = self.read([call('t1', '/f', tool='Edit'),
                            attachment('t1', 'ask', REASON, tool='Edit')])
        self.assertEqual([(d.tool, d.command, d.session, d.cwd) for d in got],
                         [('Edit', '', 's1', '/home/u/proj')])

    def test_a_record_with_no_session_falls_back_to_the_file(self):
        rec = call('t1')
        del rec['sessionId']
        calls, _ = self.read([rec])
        self.assertTrue(calls[0].session.endswith('0.jsonl'))

    def test_a_numbered_hook_command_is_named_by_its_reason(self):
        _, got = self.read([call('t1'), attachment(
            't1', 'ask', REASON, hook='command hook 7')])
        self.assertEqual([(d.plugin, d.decision) for d in got],
                         [('branch-guard', 'ask')])

    def test_a_numbered_allow_needs_own_label(self):
        recs = [call('t1'), attachment('t1', 'allow', 'Safe',
                                       hook='command hook 7'),
                call('t2'), attachment('t2', 'allow', 'other',
                                       hook='command hook 7'),
                call('t3'), attachment('t3', hook='command hook 7')]

        def own(decision, reason):
            return ('branch-guard' if (decision, reason) == ('allow', 'Safe')
                    else None)
        self.assertEqual(self.read(recs)[1], [])
        self.assertEqual([d.call_id for d in self.read(recs, own_label=own)[1]],
                         ['t1'])

    def test_own_label_does_not_reassign_a_named_guard(self):
        _, got = self.read(
            [call('t1'), attachment('t1', 'allow', 'x', hook='/x/prod-guard.py')],
            own_label=lambda *a: 'branch-guard')
        self.assertEqual([d.plugin for d in got], ['prod-guard'])

    def test_silence_is_a_defer_and_a_failure_is_not(self):
        _, got = self.read([
            call('t1'), attachment('t1'),
            call('t2'), attachment('t2', type='hook_cancelled', timedOut=True,
                                   timeoutMs=60000),
            call('t3'), attachment('t3', type='hook_non_blocking_error',
                                   exitCode=126, stderr='no\nyes')])
        self.assertEqual([(d.decision, d.reason) for d in got], [
            ('defer', ''),
            ('error', 'hook_cancelled: timed out after 60000ms'),
            ('error', 'hook_non_blocking_error: exit 126: no')])

    def test_a_long_failure_keeps_both_ends(self):
        note = bf.error_note({'type': 'e', 'stderr': 'a' * 200 + ' denied'})
        self.assertLess(len(note), 130)
        self.assertTrue(note.startswith('e: aaa') and note.endswith(' denied'))

    def test_a_record_that_is_not_a_verdict(self):
        for stdout in ('[]', '"x"', '{', '{"hookSpecificOutput": null}', '{}'):
            with self.subTest(stdout=stdout):
                _, got = self.read([call('t1'), attachment('t1', stdout=stdout)])
                self.assertEqual([d.decision for d in got], ['defer'])
        # These carry a hookName and no command.
        _, got = self.read([call('t1'), record(attachment={
            'type': 'hook_system_message', 'hookName': 'PreToolUse:Bash',
            'content': 'x', 'toolUseID': 't1'})])
        self.assertEqual(got, [])

    def test_other_hook_events_are_left_alone(self):
        rec = attachment('t1', 'ask', REASON)
        rec['attachment']['hookName'] = 'PostToolUse:Bash'
        self.assertEqual(self.read([call('t1'), rec])[1], [])


class RobustnessTests(ReaderCase):
    def test_malformed_lines_are_skipped(self):
        calls, _ = self.read([
            '\n', '{"tool_use"\n', '["tool_use"]\n',
            record(message='tool_use'), record(message={'content': 'tool_use'}),
            record(message={'content': ['tool_use', {'type': 'tool_use'}]}),
            record(message={'content': [{'type': 'tool_use', 'id': 't0',
                                         'name': 'Bash', 'input': None}]}),
            call('t1')])
        self.assertEqual([c.call_id for c in calls], ['t0', 't1'])

    def test_an_unreadable_path_is_skipped(self):
        missing = os.path.join(self.tmp, 'gone.jsonl')
        self.assertEqual(list(bf.read_events([missing])), [])


class NameTests(unittest.TestCase):
    def test_guard_name(self):
        for command, want in (
                (HOOK, 'branch-guard'),
                ('python3 "/x/scripts/bash-prod-guard.py"', 'prod-guard'),
                ('/x/pr-sentinel-guard.py', 'pr-sentinel'),
                ('"${R}/hooks/run-spill-guard.cmd" hook', 'spill-guard'),
                ('"${R}/hooks/run-python-hook.cmd"', None),
                ('/repo/.claude/hooks/lint.sh', None),
                ('command hook 7', None), ('', None), (None, None)):
            with self.subTest(command=command):
                self.assertEqual(bf.guard_name(command), want)

    def test_both_streams_use_one_label(self):
        for command, reason in (
                (HOOK, REASON), ('/x/pr-sentinel-guard.py', 'pr-sentinel: x'),
                ('"run-spill-guard.cmd"', 'spill-guard: x')):
            self.assertEqual(bf.guard_name(command), bf.named_guard(reason))


class WindowTests(unittest.TestCase):
    def test_parse_since(self):
        now = dt.datetime.now(dt.timezone.utc)
        for spec, delta in (('2d', dt.timedelta(days=2)),
                            ('3h', dt.timedelta(hours=3)),
                            ('4m', dt.timedelta(minutes=4))):
            self.assertLess(abs(now - delta - bf.parse_since(spec)),
                            dt.timedelta(seconds=5))
        self.assertEqual(bf.parse_since('2026-07-01'),
                         dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc))
        self.assertIsNone(bf.parse_since('all'))
        self.assertIsNone(bf.parse_since(''))
        for spec in ('7w', '2026-13-01', 'x'):
            with self.assertRaises(ValueError):
                bf.parse_since(spec)

    def test_in_window(self):
        event = bf.Call('t', 'Bash', '', '/home/u/proj',
                        bf.parse_ts({'timestamp': TS}), 's')
        day = dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
        self.assertTrue(bf.in_window(event, None, ''))
        self.assertTrue(bf.in_window(event, day, 'proj'))
        self.assertFalse(bf.in_window(event, day + dt.timedelta(days=1), ''))
        self.assertFalse(bf.in_window(event, day, 'other'))

    def test_a_timestamp_without_a_zone_compares(self):
        self.assertIsNotNone(
            bf.parse_ts({'timestamp': '2026-07-01T12:00:00'}).tzinfo)


class StaleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def stale(self, installed, available, known=True):
        clone = os.path.join(self.tmp, 'clone' if known else 'marketplaces/mkt')
        for path, data in (
                ('installed_plugins.json', {'plugins': {
                    'branch-guard@mkt': [{'version': v} for v in installed]}}),
                ('known_marketplaces.json',
                 {'mkt': {'installLocation': clone}} if known else {}),
                (os.path.join(clone, '.claude-plugin', 'marketplace.json'),
                 {'plugins': [{'name': 'branch-guard', 'version': available}]})):
            path = os.path.join(self.tmp, path)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                json.dump(data, f)
        return bf.stale_install(self.tmp, 'branch-guard')

    def test_lag_is_reported(self):
        for known in (True, False):
            self.assertEqual(
                self.stale(['1.9.0'], '1.10.0', known),
                {'plugin': 'branch-guard', 'installed': '1.9.0',
                 'available': '1.10.0', 'marketplace': 'mkt'})

    def test_newest_record_wins(self):
        self.assertIsNone(self.stale(['1.9.0', '1.10.0'], '1.10.0'))

    def test_no_opinion(self):
        self.assertIsNone(self.stale(['1.10.0'], '1.9.0'))
        self.assertIsNone(self.stale(['1.10.0'], None))
        self.assertIsNone(self.stale(['x'], '1.9.0'))
        self.assertIsNone(bf.stale_install('/nonexistent', 'branch-guard'))


if __name__ == '__main__':
    unittest.main()
