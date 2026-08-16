"""Table-driven tests for the pipe-guard decision logic.

Both directions are asserted because both fail silently. A rule that stops
matching lets the original bug back in: a failing gate piped into `tail` reports
success and reads exactly like a real green. A rule that matches too much denies
every `git show`, `grep`, and commit message that merely NAMES a gate -- and
this runs on every Bash call.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, 'scripts', 'bash-pipe-guard.py')
REGISTRY = os.path.join(REPO, 'pipe-guard.json')


def load_module():
    """Import the hook script, whose filename is not a valid module name."""
    spec = importlib.util.spec_from_file_location('bash_pipe_guard', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pg = load_module()


def shipped_registry():
    """The registry the hook reads at runtime, not a copy.

    A registry edit that broke a pattern would otherwise pass a suite asserting
    its own fixture.
    """
    with open(REGISTRY, encoding='utf-8') as fh:
        data = json.load(fh)
    reg = pg.Registry(data)
    assert not reg.errors, 'registry patterns do not compile: %s' % reg.errors
    assert reg.gates, 'registry lists no gates'
    return reg


# (name, command, run_in_background, expect_deny, reason_substring)
CASES = [
    # --- A gate whose status the pipe swallows -------------------------------
    ('plain pipe to tail', 'make check | tail -30', False, True,
     "exit status is the filter's"),
    ('the canonical false green',
     'make check 2>&1 | tail -30; echo "EXIT=$?"', False, True, ''),
    ('git pull piped', 'git pull --ff-only 2>&1 | tail -5; echo "EXIT=$?"',
     False, True, ''),
    ('git push piped', 'git push -u origin HEAD 2>&1 | tail -3', False, True, ''),
    ('make -C piped to grep',
     'make -C sub test-integration | grep -E "FAIL|ok"', False, True, ''),
    ('go test piped', 'go test ./... | tail -20', False, True, ''),
    ('pytest piped', 'pytest -q | tail -5', False, True, ''),
    ('npm test piped', 'npm test | tail -20', False, True, ''),
    ('cargo test piped', 'cargo test --all | head -40', False, True, ''),
    ('ruff piped', 'ruff check . | tail', False, True, ''),
    ('gradlew piped', './gradlew build | tail -5', False, True, ''),
    ('scripts gate piped', 'scripts/ci/check-tools.sh | head -20', False, True, ''),
    ('bash-wrapped scripts gate',
     'bash scripts/docs/lint-backlog.sh | grep -v "^ok"', False, True, ''),
    ('tee loses the status too', 'make check | tee tmp/check.log', False, True, ''),
    ('inside a command substitution', 'out=$(make check | tail -1)',
     False, True, ''),
    ('inside a backtick substitution', 'out=`make check | tail -1`',
     False, True, ''),
    ('subshell group piped', '(cd sub && go test ./...) | tail -5',
     False, True, ''),
    ('brace group piped', '{ cd sub && go test ./...; } | tail -5',
     False, True, ''),
    ('after an unrelated leading segment', 'mkdir -p tmp; make check | grep FAIL',
     False, True, ''),
    ('env-prefixed gate', 'GOFLAGS=-mod=mod go build ./... | tail -5',
     False, True, ''),
    ('second stage of a three-stage pipeline', 'cat x | make check | tail',
     False, True, ''),
    ('|& pipes stderr too', 'make check |& tail -5', False, True, ''),
    ('time-wrapped gate piped', 'time make check | tail -5', False, True, ''),
    ('sudo-wrapped gate piped', 'sudo make install | tail -5', False, True, ''),

    # --- PIPESTATUS does not exist in zsh ------------------------------------
    ('PIPESTATUS[0] after a gate',
     'make check 2>&1 | tail -5; echo "EXIT=${PIPESTATUS[0]}"', False, True,
     'does not exist in zsh'),
    ('bare $PIPESTATUS, no gate', 'ls -l | wc -l; echo $PIPESTATUS', False, True,
     'does not exist in zsh'),

    # --- The correct forms ---------------------------------------------------
    ('redirect then echo $?', 'make check > tmp/check.log 2>&1; echo "EXIT=$?"',
     False, False, ''),
    ('redirect then grep the FILE',
     'make check > tmp/check.log 2>&1; echo "EXIT=$?"; grep -E "FAILED" tmp/check.log',
     False, False, ''),
    ('pipefail propagates', 'set -o pipefail; make check | tail -30',
     False, False, ''),
    ('set -euo pipefail counts', 'set -euo pipefail; make check 2>&1 | tail -30',
     False, False, ''),
    ('zsh $pipestatus recovers it',
     'make check 2>&1 | tail -5; echo "EXIT=${pipestatus[1]}"', False, False, ''),
    ('no pipe at all', 'make check', False, False, ''),
    ('gate on the RIGHT keeps its status',
     'printf "%s" "$msg" | git commit -F -', False, False, ''),
    ('gate guarded by &&', 'make check && git push', False, False, ''),

    # --- Commands that merely NAME a gate ------------------------------------
    ('git show of a file containing it',
     'git show origin/main:CLAUDE.md | grep -n "make check"', False, False, ''),
    ('commit message quoting the bug',
     'git commit -m "fix(ci): make check | tail was reporting EXIT=0"',
     False, False, ''),
    # A heredoc body is data, never a command, so a piped gate quoted in one is
    # text however the delimiter is written. No rule does this; the parser does.
    ('commit message in a quoted heredoc body',
     "git commit -F - <<'EOF'\nfix(ci): stop doing make check | tail -30\nEOF",
     False, False, ''),
    ('commit message in an unquoted heredoc body',
     'git commit -F - <<EOF\nci: make check | tail lied\nEOF', False, False, ''),
    # A quoted delimiter makes the body literal, so $PIPESTATUS there is a note
    # about the bug rather than the bug.
    ('PIPESTATUS inside a quoted heredoc is text',
     "git commit -F - <<'EOF'\nnote: ${PIPESTATUS[0]} is a bash-ism\nEOF",
     False, False, ''),
    # An UNquoted delimiter expands, so the same text really does read the
    # variable -- and in zsh it expands to empty. Denying is correct here.
    ('PIPESTATUS inside an unquoted heredoc is a real read',
     'git commit -F - <<EOF\nnote: ${PIPESTATUS[0]} was empty\nEOF', False, True,
     'does not exist in zsh'),
    ('grep for the pattern in docs', 'grep -rn "make check | tail" docs/',
     False, False, ''),
    ('single-quoted PIPESTATUS is text, not a read',
     "grep -rn '$PIPESTATUS' docs/", False, False, ''),
    ('echo of the offending form', 'echo "never run: make check | tail"',
     False, False, ''),

    # --- The break-glass prefix ----------------------------------------------
    ('override on a piped gate',
     'PIPE_GUARD_OVERRIDE=want-the-output-only make check | tail -30',
     False, False, ''),
    ('override on a lost background status',
     'PIPE_GUARD_OVERRIDE=log-only make check > tmp/c.log 2>&1; echo "EXIT=$?"',
     True, False, ''),
    ('override on a PIPESTATUS read',
     'PIPE_GUARD_OVERRIDE=demonstrating-the-bug echo $PIPESTATUS', False, False, ''),
    ('override as its own statement',
     'PIPE_GUARD_OVERRIDE=scoped-to-this-call; make check | tail -5',
     False, False, ''),
    ('quoted override value',
     'PIPE_GUARD_OVERRIDE="reading output, not status" make check | tail -5',
     False, False, ''),
    # An empty value is the switch-it-off form, so it buys nothing.
    ('empty override still denies', 'PIPE_GUARD_OVERRIDE= make check | tail -30',
     False, True, ''),
    ('override named in a commit message',
     'git commit -m "docs: PIPE_GUARD_OVERRIDE=x make check | tail is the escape"',
     False, False, ''),
    ('override quoted, gate really piped',
     'echo "PIPE_GUARD_OVERRIDE=x" | make check | tail -5', False, True, ''),
    ('a different variable is not the override',
     'PIPE_GUARD=x make check | tail -30', False, True, ''),

    # --- Non-gate commands piped into filters --------------------------------
    ('git log', 'git log --oneline | head -5', False, False, ''),
    ('git diff', 'git diff origin/main | head -40', False, False, ''),
    ('gh pr list', 'gh pr list | head -20', False, False, ''),
    ('cat a log', 'cat tmp/check.log | tail -30', False, False, ''),
    ('kubectl get', 'kubectl get pods -n app | grep Running', False, False, ''),
    ('make help is informational', 'make help | grep check', False, False, ''),
    ('make -n prints, not runs', 'make -n check | head', False, False, ''),
    ('bare npm run lists scripts', 'npm run | head -20', False, False, ''),
    ('ls', 'ls -la | head', False, False, ''),
    ('ps piped', 'ps aux | grep python', False, False, ''),

    # --- Capability probes, not gate runs ------------------------------------
    # A --version/--help invocation prints and exits, so there is no result for
    # the pipe to swallow. Every registered gate is covered, not just the one
    # instance that gets reported.
    ('shellcheck --version piped', 'shellcheck --version | grep 0.11',
     False, False, ''),
    ('shellcheck -V piped', 'shellcheck -V | head -1', False, False, ''),
    ('make --version piped', 'make --version | head -1', False, False, ''),
    ('golangci-lint --version piped', 'golangci-lint --version | cat',
     False, False, ''),
    ('go test --help piped', 'go test --help | head', False, False, ''),
    ('go vet -h piped', 'go vet -h | head', False, False, ''),
    ('git pull --help piped', 'git pull --help | head', False, False, ''),
    ('pytest --help piped', 'pytest --help | head', False, False, ''),
    ('cargo build --help piped', 'cargo build --help | head', False, False, ''),
    ('scripts gate --help piped', 'scripts/ci/check-tools.sh --help | head',
     False, False, ''),
    ('./scripts gate -h piped', './scripts/ci/check-tools.sh -h | head',
     False, False, ''),
    ('backgrounded probe ending in echo',
     'shellcheck --version > tmp/v.log 2>&1; echo "EXIT=$?"', True, False, ''),

    # The catch the guard exists for, kept beside the exemption: the same tools
    # doing real work still deny.
    ('shellcheck on a script still denies', 'shellcheck scripts/x.sh | tail',
     False, True, ''),
    ('go test -v is verbose, not a version probe', 'go test -v ./... | tail -20',
     False, True, ''),
    # `-v` is --version to make and verbose to `go test`. Exempting it would
    # exempt the case above, so the short form stays denied.
    ('make -v stays denied', 'make -v | head -1', False, True, ''),
    # A probe flag inside a quoted argument is one word, not a flag: matching
    # parsed words rather than the joined head is what keeps these gates.
    ('commit message naming --version still denies',
     'git commit -m "chore: bump --version output" | tee tmp/c.log',
     False, True, ''),
    ('backgrounded commit naming --help still denies',
     'git commit -m "docs: --help text" > tmp/c.log 2>&1; echo "EXIT=$?"',
     True, True, ''),

    # --- A backgrounded gate whose status the last statement drops -----------
    ('the canonical lost background status',
     'make check > tmp/check.log 2>&1; echo "EXIT=$?"', True, True,
     'task notification reports success'),
    ('background gate then an unrelated last statement',
     'make check > tmp/check.log 2>&1; grep -c FAILED tmp/check.log', True, True, ''),
    ('background scripts gate',
     'bash scripts/docs/lint-backlog.sh > tmp/l.log 2>&1; echo "EXIT=$?"',
     True, True, ''),
    ('background git push',
     'git push -u origin HEAD > tmp/p.log 2>&1; echo "EXIT=$?"', True, True, ''),
    ('leading segment before the gate',
     'mkdir -p tmp; make check > tmp/c.log 2>&1; echo "EXIT=$?"', True, True, ''),
    # `||` swallows the failure it was written to report.
    ('|| fallback swallows it',
     'make check > tmp/c.log 2>&1 || echo "gate failed"', True, True, ''),
    # `&` is the other spelling, and loses the status even in the foreground.
    ('trailing & forks, foreground call', 'make check > tmp/c.log 2>&1 &',
     False, True, ''),
    ('backgrounded subshell ending in echo',
     '(make check > tmp/c.log 2>&1; echo "EXIT=$?")', True, True, ''),
    # pipefail and $pipestatus are pipe mitigations; neither re-raises a status
    # the last statement already discarded.
    ('pipefail does not mitigate this',
     'set -o pipefail; make check > tmp/c.log 2>&1; echo "EXIT=$?"', True, True, ''),

    # --- Backgrounded forms that keep the status -----------------------------
    ('the documented fix re-raises it',
     'make check > tmp/check.log 2>&1; rc=$?; echo "EXIT=$rc"; exit $rc',
     True, False, ''),
    ('gate is the last statement', 'make check > tmp/check.log 2>&1',
     True, False, ''),
    ('&& chain ending in the gate',
     'mkdir -p tmp && make check > tmp/check.log 2>&1', True, False, ''),
    ('&& chain starting with the gate',
     'make check > tmp/c.log 2>&1 && echo "clean"', True, False, ''),
    # An explicit `exit 0` is a deliberate discard, and the escape hatch for a
    # background call whose status genuinely does not matter.
    ('explicit exit 0 is deliberate',
     'make check > tmp/c.log 2>&1; echo "EXIT=$?"; exit 0', True, False, ''),
    # The SAME command in the foreground is the documented correct form: the
    # echo prints the real status where it can be read.
    ('foreground redirect-then-echo is correct',
     'make check > tmp/check.log 2>&1; echo "EXIT=$?"', False, False, ''),
    ('background non-gate loses nothing worth denying',
     'gh run list > tmp/r.log 2>&1; echo "EXIT=$?"', True, False, ''),
    ('background watcher script by absolute path',
     'bash /Users/x/.claude/plugins/pr-sentinel/watch.sh 1288', True, False, ''),
    ('background echo naming the bug form',
     'echo "never background: make check; echo EXIT=$?"', True, False, ''),
    ('background grep for the pattern',
     'grep -rn "make check" docs/ > tmp/o.log 2>&1; echo "EXIT=$?"',
     True, False, ''),

    # --- A gate sequenced before a state change with `;` ---------------------
    ('check then push', 'make check; git push', False, True,
     'runs the second whatever the first returned'),
    ('test then publish', 'npm test; npm publish', False, True, ''),
    ('add then commit', 'git add .; git commit -m "wip"', False, True, ''),
    ('plan then apply', 'terraform plan; terraform apply -auto-approve',
     False, True, ''),
    ('gate then an intervening command then a mutator',
     'make check; echo done; git push', False, True, ''),
    ('&& is the correct form', 'make check && git push', False, False, ''),
    ('&& then a trailing mutator is still gated',
     'make check && git add . && git commit -m x', False, False, ''),
    ('two gates, no state change', 'make lint; make test', False, False, ''),
    ('mutator first, gate second', 'git push; make check', False, False, ''),
    ('non-gate before a mutator', 'echo hi; git push', False, False, ''),
    # Inside a subshell the sequence is that subshell's own business.
    ('sequence nested in a subshell', '(make check; git push)', False, False, ''),
    ('sequence named in a commit message',
     'git commit -m "docs: never write make check; git push"', False, False, ''),
]


class TestDecide(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.reg = shipped_registry()

    def test_table(self):
        for name, cmd, bg, want, substr in CASES:
            with self.subTest(name):
                got = pg.decide(cmd, bg, self.reg)
                if want:
                    self.assertTrue(got, 'want a deny, got silence\ncmd: %r' % cmd)
                else:
                    self.assertFalse(
                        got, 'want silence, got a deny\ncmd: %r\nreason: %s'
                        % (cmd, got))
                if substr:
                    self.assertIn(substr, got, 'reason missing %r' % substr)


class TestPositiveControl(unittest.TestCase):
    """The case that must fire, so an all-clear cannot be a broken harness.

    This is the original bug in its plainest form. If it ever passes silently,
    the suite is reporting on a guard that is not running -- not on a codebase
    that stopped tripping the rule.
    """

    CANONICAL = 'make check 2>&1 | tail -30; echo "EXIT=$?"'

    def test_canonical_false_green_is_denied(self):
        reason = pg.decide(self.CANONICAL, False, shipped_registry())
        self.assertTrue(reason, 'the positive control did not fire')
        self.assertIn("exit status is the filter's", reason)

    def test_control_goes_silent_when_the_rule_is_removed(self):
        """The other half of the control: the suite can tell a real deny from a
        rule that fires on everything."""
        self.assertFalse(pg.decide(self.CANONICAL, False, pg.Registry({})))


class TestRegistry(unittest.TestCase):
    def test_shipped_patterns_compile(self):
        reg = shipped_registry()
        self.assertEqual([], reg.errors)

    def test_shipped_patterns_are_anchored(self):
        """An unanchored pattern searches the whole head, which is how a rule
        starts matching text that merely mentions a command."""
        with open(REGISTRY, encoding='utf-8') as fh:
            data = json.load(fh)
        for key in ('gates', 'exempt', 'mutators'):
            for p in data.get(key) or []:
                with self.subTest(p):
                    self.assertTrue(p.startswith('^'),
                                    'pattern not anchored to command position')

    def test_empty_registry_never_denies(self):
        """Detection is driven by the registry, not by an incidental match."""
        empty = pg.Registry({})
        self.assertFalse(pg.decide('make check 2>&1 | tail -30', False, empty))
        self.assertFalse(pg.decide('make check > c.log 2>&1; echo "EXIT=$?"',
                                   True, empty))
        self.assertFalse(pg.decide('make check; git push', False, empty))

    def test_bad_pattern_is_dropped_not_fatal(self):
        """A bad edit degrades the guard; it never breaks the tool."""
        reg = pg.Registry({'gates': [r'^make(\s|$)', '*not a regexp']})
        self.assertEqual(1, len(reg.errors))
        self.assertTrue(pg.decide('make check | tail', False, reg),
                        'the surviving pattern should still deny')

    def test_posix_bracket_classes_are_translated(self):
        """A pattern copied from an ERE registry must not silently become a
        character set of `:aceps`."""
        reg = pg.Registry({'gates': ['^make([[:space:]]|$)']})
        self.assertEqual([], reg.errors)
        self.assertTrue(pg.decide('make check | tail', False, reg))
        self.assertFalse(pg.decide('makefoo check | tail', False, reg))

    def test_project_file_extends_the_defaults(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, '.claude'))
            with open(os.path.join(root, '.claude', 'pipe-guard.json'), 'w') as fh:
                json.dump({'gates': [r'^bazelisk(\s|$)']}, fh)
            reg = pg.load_registry(root)
            self.assertTrue(pg.decide('bazelisk test //... | tail', False, reg))
            self.assertTrue(pg.decide('make check | tail', False, reg),
                            'a project file must not drop the defaults')

    def test_project_file_can_replace_the_defaults(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, '.claude'))
            with open(os.path.join(root, '.claude', 'pipe-guard.json'), 'w') as fh:
                json.dump({'replace': True, 'gates': [r'^bazelisk(\s|$)']}, fh)
            reg = pg.load_registry(root)
            self.assertTrue(pg.decide('bazelisk test //... | tail', False, reg))
            self.assertFalse(pg.decide('make check | tail', False, reg))


class TestUnparseable(unittest.TestCase):
    """A command this guard cannot parse gets silence, not a guess."""

    CASES = ["make check | tail 'unterminated", 'make check | | tail',
             'for do done', 'make check &&', '((']

    def test_silent(self):
        reg = shipped_registry()
        for cmd in self.CASES:
            for bg in (False, True):
                with self.subTest(cmd=cmd, bg=bg):
                    self.assertFalse(pg.decide(cmd, bg, reg))


class TestPrecedence(unittest.TestCase):
    def test_pipe_verdict_wins_over_background(self):
        """Both routes lose the same status; the pipe reason names the nearer
        cause."""
        reason = pg.decide('make check 2>&1 | tail -30; echo "EXIT=$?"', True,
                           shipped_registry())
        self.assertIn("exit status is the filter's", reason)

    def test_pipestatus_wins_over_the_pipe(self):
        reason = pg.decide('make check | tail; echo ${PIPESTATUS[0]}', False,
                           shipped_registry())
        self.assertIn('does not exist in zsh', reason)

    def test_every_reason_carries_the_override(self):
        reg = shipped_registry()
        for cmd, bg in (('make check | tail', False),
                        ('make check > c.log 2>&1; echo hi', True),
                        ('make check; git push', False),
                        ('echo $PIPESTATUS', False)):
            with self.subTest(cmd):
                self.assertIn('PIPE_GUARD_OVERRIDE=<reason>',
                              pg.decide(cmd, bg, reg))


class TestHookEndToEnd(unittest.TestCase):
    """Invoke the hook the way Claude Code does: JSON on stdin, JSON on stdout."""

    def run_hook(self, payload):
        proc = subprocess.run([sys.executable, SCRIPT], input=json.dumps(payload),
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(0, proc.returncode, proc.stderr)
        return proc.stdout.strip()

    def test_denies_the_canonical_false_green(self):
        out = self.run_hook({
            'tool_name': 'Bash', 'cwd': REPO,
            'tool_input': {'command': 'make check 2>&1 | tail -30'}})
        payload = json.loads(out)['hookSpecificOutput']
        self.assertEqual('PreToolUse', payload['hookEventName'])
        self.assertEqual('deny', payload['permissionDecision'])
        self.assertIn("exit status is the filter's",
                      payload['permissionDecisionReason'])

    def test_never_asks(self):
        """A deny reaches the model; an ask reaches the user, and the model
        never learns why the command was wrong."""
        for cmd, bg in (('make check | tail -5', False),
                        ('make check > c.log 2>&1; echo "EXIT=$?"', True),
                        ('make check; git push', False)):
            with self.subTest(cmd):
                out = self.run_hook({'tool_name': 'Bash', 'cwd': REPO,
                                     'tool_input': {'command': cmd,
                                                    'run_in_background': bg}})
                self.assertEqual(
                    'deny',
                    json.loads(out)['hookSpecificOutput']['permissionDecision'])

    def test_stays_silent_on_a_clean_command(self):
        out = self.run_hook({
            'tool_name': 'Bash', 'cwd': REPO,
            'tool_input': {'command': 'make check > tmp/c.log 2>&1; echo "EXIT=$?"'}})
        self.assertEqual('', out)

    def test_stays_silent_on_other_tools(self):
        out = self.run_hook({
            'tool_name': 'Read', 'cwd': REPO,
            'tool_input': {'file_path': '/etc/hosts'}})
        self.assertEqual('', out)

    def test_malformed_payload_does_not_break_the_tool(self):
        proc = subprocess.run([sys.executable, SCRIPT], input='not json',
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(0, proc.returncode)
        self.assertEqual('', proc.stdout.strip())

    def test_background_flag_is_read_from_the_payload(self):
        cmd = 'make check > tmp/c.log 2>&1; echo "EXIT=$?"'
        base = {'tool_name': 'Bash', 'cwd': REPO}
        fg = self.run_hook(dict(base, tool_input={'command': cmd}))
        bg = self.run_hook(dict(base, tool_input={'command': cmd,
                                                  'run_in_background': True}))
        self.assertEqual('', fg)
        self.assertIn('task notification reports success', bg)


class TestSegmentation(unittest.TestCase):
    """The ported layer, exercised directly -- these are the parts whose failure
    mode is silent in both directions."""

    def segs(self, cmd):
        tokens, _, _ = pg.tokenize(cmd)
        return pg.split_segments(tokens)

    def test_heredoc_body_is_not_a_command(self):
        segs = self.segs("cat <<'EOF'\nmake check | tail\nEOF")
        heads = [' '.join(pg.head_words(s)) for s in segs]
        self.assertNotIn('make check', heads)

    def test_redirect_targets_leave_the_head(self):
        segs = self.segs('make check > tmp/out.log 2>&1')
        self.assertEqual(['make check'],
                         [' '.join(pg.head_words(s)) for s in segs])

    def test_parens_are_transparent_to_the_next_operator(self):
        segs = self.segs('(cd sub && go test ./...) | tail')
        gate = [s for s in segs if pg.head_words(s)[:2] == ['go', 'test']][0]
        self.assertEqual('|', pg.next_op(gate.post_ops))

    def test_operator_runs_split(self):
        tokens, _, _ = pg.tokenize('(cd x); make check')
        self.assertIn(')', tokens)
        self.assertIn(';', tokens)

    def test_pipe_stderr_operator_is_one_token(self):
        tokens, _, _ = pg.tokenize('make check |& tail')
        self.assertIn('|&', tokens)

    def test_wrappers_peel_to_the_real_command(self):
        for cmd, want in (('time make check', 'make check'),
                          ('sudo make install', 'make install'),
                          ('bash scripts/x.sh', 'scripts/x.sh'),
                          ('LC_ALL=C make check', 'make check'),
                          ('time LC_ALL=C make check', 'make check')):
            with self.subTest(cmd):
                segs = self.segs(cmd)
                self.assertEqual(want, ' '.join(pg.head_words(segs[0])))

    def test_unbalanced_quotes_defer(self):
        tokens, _, _ = pg.tokenize("make check 'unterminated")
        self.assertIsNone(tokens)


if __name__ == '__main__':
    unittest.main()
