#!/usr/bin/env python3
"""Tests for scripts/friction-report.py. Run by test/run.sh.

The reason fixtures are read out of hooks/branch-guard.py rather than copied, so
a reason added or reworded there fails here until a category covers it. The
transcripts are synthetic, and no real one is read.
"""
import ast
import io
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
SCRIPT = os.path.join(PLUGIN, 'scripts', 'friction-report.py')
HOOK = os.path.join(PLUGIN, 'hooks', 'branch-guard.py')


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fr = load('friction_report', SCRIPT)
guard = load('branch_guard', HOOK)
with open(HOOK, encoding='utf-8') as _f:
    HOOK_SOURCE = _f.read()

VERDICTS = {'ask', 'ask-shared'} | set(guard.DENY_ROUTES)
# What an interpolated name stands for, where the words around it decide the
# category. Any other is a branch, a path or a subcommand.
VALUES = {'what': '`git branch -M`', 'verb': 'Copying'}
# A reason handed on by name is collected where it was written.
HANDED_ON = {'reason'}


def render(node):
    """Every text a reason expression can produce, or None if unknown."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        return [''.join(
            p.value if isinstance(p, ast.Constant)
            else VALUES.get(getattr(p.value, 'id', None), 'x')
            for p in node.values)]
    if isinstance(node, (ast.BoolOp, ast.IfExp)):
        parts = (node.values if isinstance(node, ast.BoolOp)
                 else [node.body, node.orelse])
        texts = [render(p) for p in parts]
        return None if None in texts else sum(texts, [])
    if isinstance(node, ast.Name):
        if node.id in HANDED_ON:
            return []
        value = getattr(guard, node.id, None)
        return [value] if isinstance(value, str) else None
    if isinstance(node, ast.Call):
        if getattr(node.func, 'id', None) == 'push_overlap_reason':
            return [guard.push_overlap_reason('origin/main', ['a', 'b'])]
        template = getattr(getattr(node.func, 'value', None), 'value', '')
        if str(template).startswith('`gh {} {}` '):
            return []                # classify_gh's, driven in gh_reasons()
    return None


def hook_reasons(source):
    """({text: line}, [line]) -- the causes the hook's classifiers can return,
    and the lines holding a reason this could not read."""
    found, unread = {}, []

    def take(node):
        texts = render(node)
        if texts is None:
            unread.append(node.lineno)
        for text in texts or ():
            found[text] = node.lineno

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Tuple) and len(node.elts) == 2 \
                and getattr(node.elts[0], 'value', None) in VERDICTS \
                and getattr(node.elts[1], 'value', None) not in VERDICTS:
            take(node.elts[1])
        elif isinstance(node, ast.Call):
            name = getattr(node.func, 'id', None)
            if name in ('confirm', 'refuse'):
                take(node.args[0])
            elif name == '_feature' and len(node.args) > 1:
                take(node.args[1])
        elif isinstance(node, ast.FunctionDef) \
                and node.name == 'protected_targets':
            for pair in ast.walk(node):
                if isinstance(pair, ast.Tuple) and len(pair.elts) == 2:
                    take(pair.elts[1])
    return found, unread


def gh_reasons():
    for sub, subsub in guard.DESTRUCTIVE_GH:
        yield guard.classify_gh(sub, [subsub, '--repo', 'o/n'])
    for sub, subsub in guard.GH_PIN_REQUIRED:
        yield guard.classify_gh(sub, [subsub])


def matches(reason):
    return [c for c, rx in fr.CATEGORY_PATTERNS.items() if rx.match(reason)]


class CategoryTests(unittest.TestCase):
    def setUp(self):
        self.found, self.unread = hook_reasons(HOOK_SOURCE)

    def test_every_hook_reason_has_exactly_one_category(self):
        self.assertEqual(self.unread, [], 'reasons at these hook lines are in '
                         'a form this test cannot read')
        for reason, line in self.found.items():
            with self.subTest(line=line, reason=reason):
                self.assertEqual(len(matches(reason)), 1, matches(reason))

    def test_every_gh_reason_has_exactly_one_category(self):
        seen = set()
        for verdict, reason in gh_reasons():
            with self.subTest(reason=reason):
                self.assertIn(verdict, VERDICTS)
                self.assertEqual(len(matches(reason)), 1, matches(reason))
                seen.update(matches(reason))
        self.assertEqual(seen, {'remote-delete', 'unpinned-gh'})

    def test_every_category_is_reached_by_the_hook(self):
        reached = {c for r in self.found for c in matches(r)}
        reached |= {c for _, r in gh_reasons() for c in matches(r)}
        self.assertEqual(reached, set(fr.CATEGORY_PATTERNS))

    def test_every_category_has_its_own_fix(self):
        self.assertEqual(list(fr.CATEGORY_HINT), list(fr.CATEGORY_PATTERNS))
        self.assertEqual(len(set(fr.CATEGORY_HINT.values())),
                         len(fr.CATEGORY_HINT))

    def test_the_collector_sees_a_reason_added_to_the_hook(self):
        for line in ("return ('ask', 'Brand new cause')",
                     "return ('deny-rebase', f'Brand new {cause}')",
                     "return _feature(branch, 'Brand new cause', probe)",
                     "confirm('Brand new cause', mode)"):
            with self.subTest(line=line):
                found, _ = hook_reasons(
                    HOOK_SOURCE + '\ndef added():\n    %s\n' % line)
                new = set(found) - set(self.found)
                self.assertEqual(len(new), 1)
                self.assertEqual(matches(new.pop()), [])

    def test_the_collector_reports_a_reason_it_cannot_read(self):
        _, unread = hook_reasons(
            HOOK_SOURCE + "\ndef added():\n    return ('ask', build(x))\n")
        self.assertEqual(len(unread), 1)

    def test_wording_the_hook_no_longer_emits(self):
        for reason, want in (
                ('Deleting/renaming a git branch — confirm', 'unpublished-tip'),
                ('Removing/moving a git worktree — confirm', 'worktree-admin'),
                ('Stash operation on a protected branch', 'protected-work'),
                ('`gh pr merge --delete-branch` deletes the branch',
                 'remote-delete'),
                ("`git reset --hard` discards changes, and a `git -C`/"
                 "`--git-dir` option points at another repository",
                 'unnamed-target')):
            self.assertEqual(fr.category_of(reason), want)

    def test_a_name_in_a_reason_cannot_move_it(self):
        reason = ("branch-guard: Push targets 'Stash operation on a protected "
                  "branch', not the worktree branch 'x' — confirm")
        self.assertEqual(fr.category_of(reason), 'cross-push')

    def test_an_unknown_reason_is_other(self):
        self.assertEqual(fr.category_of('branch-guard: something new'), 'other')


class EmitTests(unittest.TestCase):
    """The category and the label survive what the hook wraps round a cause."""

    def emitted(self, fn, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            fn(*args)
        out = json.loads(buf.getvalue())['hookSpecificOutput']
        return out['permissionDecision'], out['permissionDecisionReason']

    def test_confirm_and_refuse(self):
        cause = '`git restore` discards working-tree changes'
        for fn, args, want in (
                (guard.confirm, (cause, 'default'), 'ask'),
                (guard.confirm, (cause, 'dontAsk'), 'deny'),
                (guard.refuse, (cause, 'do this'), 'deny')):
            decision, reason = self.emitted(fn, *args)
            self.assertEqual(decision, want)
            self.assertEqual(fr.category_of(reason), 'uncommitted-work')
            self.assertEqual(fr.NO_PROMPT in reason, 'dontAsk' in args)

    def test_every_allow_is_recognized(self):
        texts = []
        for node in ast.walk(ast.parse(HOOK_SOURCE)):
            if isinstance(node, ast.Call) \
                    and getattr(node.func, 'id', None) == 'emit' \
                    and getattr(node.args[0], 'value', None) == 'allow':
                texts += [t.replace('— x is set', '— %s is set'
                                    % guard.OVERRIDE_VAR)
                          for t in render(node.args[1])]
        self.assertEqual(len(texts), 3)
        for text in texts:
            with self.subTest(text=text):
                self.assertEqual(fr.own_label('allow', text), 'branch-guard')
        self.assertIsNone(fr.own_label('ask', texts[0]))
        self.assertIsNone(fr.own_label('allow', 'Guarded commands target '
                                       'workspace/pipe only'))
        for text in texts:
            self.assertIsNone(fr.own_label('allow', text + ' And more.'))


class WiringTests(unittest.TestCase):
    def read(self, *parts):
        with open(os.path.join(PLUGIN, *parts), encoding='utf-8') as f:
            return f.read()

    def test_every_tool_the_hook_runs_on_is_counted(self):
        entries = json.loads(self.read('hooks', 'hooks.json'))
        tools = {t for e in entries['hooks']['PreToolUse']
                 for t in e['matcher'].split('|')}
        self.assertEqual(tools, set(fr.GUARDED_TOOLS))

    def test_the_command_runs_the_script(self):
        self.assertIn('"${CLAUDE_PLUGIN_ROOT}/scripts/friction-report.py"',
                      self.read('commands', 'friction-report.md'))

    def test_the_readme_lists_the_categories(self):
        section = self.read('README.md').split('## Friction report')[1]
        listed = re.findall(r'^\| `([a-z-]+)` \|', section.split('\n## ')[0],
                            re.M)
        self.assertEqual(listed, list(fr.CATEGORY_PATTERNS))


class OverrideTests(unittest.TestCase):
    def test_only_a_prefix_the_hook_reads_counts(self):
        for command, want in (
                ('BRANCH_GUARD_OVERRIDE=r git restore f', True),
                ('cd x && BRANCH_GUARD_OVERRIDE=r git restore f', True),
                ('env BRANCH_GUARD_OVERRIDE=r git restore f', True),
                ('BRANCH_GUARD_OVERRIDE= git restore f', False),
                ('grep BRANCH_GUARD_OVERRIDE=r f', False),
                ('git commit -m "BRANCH_GUARD_OVERRIDE=r git"', False),
                ("cat > f <<'E'\nBRANCH_GUARD_OVERRIDE=r git restore f\nE",
                 False),
                ('BRANCH_GUARD_OVERRIDE=r git "', False),
                ('git status', False)):
            with self.subTest(command=command):
                self.assertEqual(fr.has_override(command), want)


TS = '2026-07-01T12:00:00.000Z'
OLD = '2020-01-01T00:00:00Z'
WIRED = '"${CLAUDE_PLUGIN_ROOT}/hooks/run-python-hook.cmd" branch-guard.py'
ASK = "branch-guard: Dropping stashed changes — confirm before proceeding."
DENY = ("branch-guard: 'origin/main' has moved since this branch left it, and "
        "its new commits edit the same lines — run that rebase")
DENY_MODE = ("branch-guard: `git restore` discards working-tree changes — "
             "denied because permission mode 'dontAsk' has no way to prompt "
             "for confirmation. Retrying won't help")
EDIT_ASK = "branch-guard: Targets protected branch 'main' — confirm"
ALLOW = "Safe git/gh operation on branch 'x' — auto-approved."
LIFTED = ("`git restore` discards working-tree changes — BRANCH_GUARD_OVERRIDE "
          "is set (r), so branch-guard allowed it.")
PREFIXED = 'BRANCH_GUARD_OVERRIDE=r git restore f'
HOOK_ERROR = 'PreToolUse:%s hook error: '


def record(ts=TS, **fields):
    out = {'cwd': '/home/u/proj', 'sessionId': 's1', **fields}
    if ts:
        out['timestamp'] = ts
    return out


def call(cid, command='git push', tool='Bash', **kw):
    return record(message={'content': [
        {'type': 'tool_use', 'id': cid, 'name': tool,
         'input': {'command': command}}]}, **kw)


def denied(cid, text, **kw):
    return record(message={'content': [
        {'type': 'tool_result', 'tool_use_id': cid, 'is_error': True,
         'content': text}]}, **kw)


def decided(cid, decision, reason, tool='Bash', hook=WIRED, **kw):
    return record(attachment={
        'type': 'hook_success', 'hookName': 'PreToolUse:' + tool,
        'command': hook, 'toolUseID': cid,
        'stdout': json.dumps({'hookSpecificOutput': {
            'permissionDecision': decision,
            'permissionDecisionReason': reason}})}, **kw)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_report(self, records, *args, code=0):
        with open(os.path.join(self.tmp, 's1.jsonl'), 'w',
                  encoding='utf-8') as f:
            f.writelines(json.dumps(r) + '\n' for r in records)
        p = subprocess.run(
            [sys.executable, SCRIPT, '--transcripts', self.tmp,
             '--plugins-dir', os.path.join(self.tmp, 'none'),
             '--since', 'all'] + list(args),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=dict(os.environ, PYTHONIOENCODING='utf-8'))
        self.assertEqual(p.returncode, code, p.stderr.decode('utf-8'))
        return p.stdout.decode('utf-8')

    def report(self, records, *args, code=0):
        return json.loads(self.run_report(records, '--json', *args, code=code))

    def test_both_denial_forms_count(self):
        r = self.report([
            call('t1'), denied('t1', DENY),
            call('t2'), denied('t2', HOOK_ERROR % 'Bash' + DENY)])
        self.assertEqual(r['decisions'], {'deny': 2})
        self.assertEqual(r['categories']['deny'], {'stale-base': 2})

    def test_a_repeat_and_an_untimestamped_copy_do_not(self):
        pair = [call('t1'), denied('t1', DENY)]
        r = self.report(pair * 2 + [denied('t1', DENY, ts=None)])
        self.assertEqual((r['decisions'], r['calls']), ({'deny': 1}, 1))

    def test_since_drops_the_old_and_the_undated(self):
        r = self.report([
            call('t1'), denied('t1', DENY),
            call('t2', ts=None), denied('t2', DENY, ts=None),
            call('t3', ts=OLD), denied('t3', DENY, ts=OLD)],
            '--since', '2026-01-01')
        self.assertEqual((r['decisions'], r['calls']), ({'deny': 1}, 1))

    def test_every_guarded_tool_is_counted(self):
        records = []
        for i, tool in enumerate(fr.GUARDED_TOOLS):
            records += [call('a%d' % i, tool=tool),
                        decided('a%d' % i, 'ask', EDIT_ASK, tool=tool),
                        call('d%d' % i, tool=tool),
                        denied('d%d' % i, HOOK_ERROR % tool + EDIT_ASK)]
        r = self.report(records + [call('r', tool='Read')])
        self.assertEqual(r['tools'], dict.fromkeys(fr.GUARDED_TOOLS, 2))
        self.assertEqual(r['calls'], 2 * len(fr.GUARDED_TOOLS))

    def test_a_numbered_hook_command(self):
        numbered = {'hook': 'command hook 7'}
        r = self.report([
            call('t1'), decided('t1', 'ask', ASK, **numbered),
            call('t2'), decided('t2', 'allow', ALLOW, **numbered),
            call('t3'), decided('t3', 'allow', 'Guarded commands target '
                                'workspace/pipe only', **numbered),
            call('t4'), decided('t4', 'ask', 'prod-guard: x', **numbered)])
        self.assertEqual(r['decisions'], {'ask': 1, 'allow': 1})
        self.assertEqual(r['categories']['ask'], {'stash-drop': 1})

    def test_overrides(self):
        r = self.report([
            call('t1', PREFIXED), decided('t1', 'allow', LIFTED),
            call('t2', PREFIXED), denied('t2', DENY_MODE),
            call('t3', 'BRANCH_GUARD_OVERRIDE=r git status'),
            call('t4', 'grep BRANCH_GUARD_OVERRIDE=r f'),
            call('t5'), denied('t5', DENY)])
        self.assertEqual(r['overrides'],
                         {'prefixed': 3, 'lifted': 1, 'refused': 1})
        self.assertEqual(r['no_prompt_denies'], 1)

    def test_the_rate_is_of_calls_and_sessions_in_the_window(self):
        records = [call('t%d' % i) for i in range(9)] + [
            call('x', sessionId='s2'), decided('t0', 'ask', ASK),
            call('old', ts=OLD, sessionId='s3')]
        text = self.run_report(records, '--since', '2026-01-01')
        self.assertIn('friction (ask+deny): 1 -- 10.00% of the 10 calls', text)
        self.assertIn('sessions stopped at least once: 1 of 2', text)

    def test_text_names_the_category_and_its_fix(self):
        text = self.run_report([call('t1'), denied('t1', DENY),
                                call('t2'), decided('t2', 'ask', ASK)])
        for cat, ask, deny in (('stale-base', 0, 1), ('stash-drop', 1, 0)):
            self.assertRegex(text, r'\n +%d +%d  %s +%s' % (
                ask, deny, cat, re.escape(fr.CATEGORY_HINT[cat])))
        self.assertIn('branch-guard 2', text)

    def test_all_plugins_omits_this_guards_own_statistics(self):
        records = [call('t1', PREFIXED), decided('t1', 'allow', LIFTED),
                   call('t2'), denied('t2', 'prod-guard: no'),
                   call('t3'), denied('t3', DENY)]
        r = self.report(records, '--plugin', 'all')
        self.assertEqual(r['plugins'], {'branch-guard': 2, 'prod-guard': 1})
        self.assertEqual(r['categories']['deny'], {'other': 1, 'stale-base': 1})
        for key in ('overrides', 'calls', 'sessions', 'sessions_stopped'):
            self.assertNotIn(key, r)

    def test_a_hook_that_never_ran_is_reported(self):
        failed = decided('t1', 'ask', ASK)
        failed['attachment'].update(type='hook_cancelled', timedOut=True,
                                    timeoutMs=60000)
        text = self.run_report([call('t1'), failed])
        self.assertIn('outcomes: error 1', text)
        self.assertIn('timed out after 60000ms', text)

    def test_empty_results_say_which_filter_emptied_them(self):
        records = [call('t1'), decided('t1', 'ask', ASK)]
        for args, code, want in (
                (('--plugin', 'nope-guard'), 2, 'branch-guard (1)'),
                (('--repo', 'nope'), 2, "--repo 'nope' matched no cwd"),
                (('--since', '2026-08-01'), 0, 'most recent is 2026-07-01')):
            with self.subTest(args=args):
                self.assertIn(want, self.run_report(records, *args, code=code))
        self.assertIn('No guard decisions', self.run_report([call('t1')]))

    def test_a_bad_since_is_a_usage_error(self):
        self.run_report([call('t1')], '--since', '7w', code=1)


if __name__ == '__main__':
    unittest.main()
