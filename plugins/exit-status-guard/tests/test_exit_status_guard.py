"""Table-driven tests for the exit-status-guard decision logic.

Both directions are asserted because both fail silently. A rule that stops
matching lets the original bug back in: a failing gate piped into `tail` reports
success and reads exactly like a real green. A rule that matches too much denies
every `git show`, `grep`, and commit message that merely NAMES a gate -- and
this runs on every Bash call.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, 'scripts', 'bash-exit-status-guard.py')
REGISTRY = os.path.join(REPO, 'exit-status-guard.json')


def load_module():
    """Import the hook script, whose filename is not a valid module name."""
    spec = importlib.util.spec_from_file_location('bash_exit_status_guard', SCRIPT)
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


def make_scratchpad(root, session):
    """Build Claude Code's session layout under ``root`` and return the leaf.

    Spelled out here rather than borrowed from the guard, so the layout the
    suggested rewrites depend on is pinned by the suite rather than asserted
    against itself.
    """
    path = os.path.join(root, '-Users-someone-workspace-proj', session,
                        'scratchpad')
    os.makedirs(path)
    return path


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

    # --- PIPESTATUS is not portable across shells ----------------------------
    ('PIPESTATUS[0] after a gate',
     'make check 2>&1 | tail -5; echo "EXIT=${PIPESTATUS[0]}"', False, True,
     'reads $PIPESTATUS to recover'),
    ('bare $PIPESTATUS, no gate', 'ls -l | wc -l; echo $PIPESTATUS', False, True,
     'reads $PIPESTATUS to recover'),
    # zsh's lowercase spelling suppresses nothing: the array holds the most
    # recent pipeline only, so a read cannot be tied to the pipeline it claims
    # to recover -- before one, or after a different one.
    ('lowercase $pipestatus does not suppress the pipe verdict',
     'make check 2>&1 | tail -5; echo "EXIT=${pipestatus[1]}"', False, True,
     "exit status is the filter's"),
    ('$pipestatus does not reach a later pipeline',
     'make check | tail; echo ${pipestatus[1]}; make lint | tail', False, True,
     "exit status is the filter's"),
    ('$pipestatus read before any pipeline',
     'echo ${pipestatus[1]}; make check | tail', False, True,
     "exit status is the filter's"),

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
    # variable, which is the read the rule is about. Denying is correct here.
    ('PIPESTATUS inside an unquoted heredoc is a real read',
     'git commit -F - <<EOF\nnote: ${PIPESTATUS[0]} was empty\nEOF', False, True,
     'reads $PIPESTATUS to recover'),
    # A heredoc opened inside `"$(…)"` -- how a multi-paragraph commit message
    # gets written without quoting every line. Bash reopens quoting inside the
    # substitution, so the `<<` is unquoted to the shell; read flat, the body
    # survived and its own quotes decided where the newlines landed, which read
    # as a `;` between two commands the author had joined with `&&` (#23).
    ('heredoc in a quoted substitution, phrase wrapped across lines',
     'git commit -aqF "$(cat <<\'MSG\'\nsubject\n\nhe asked "a question\n'
     'spanning lines" here\nMSG\n)" && git push', False, False, ''),
    ('the same message with the phrase on one line',
     'git commit -aqF "$(cat <<\'MSG\'\nsubject\n\nhe asked "a question" here\n'
     'MSG\n)" && git push', False, False, ''),
    # The delimiter's quoting decides expansion, not whether the `<<` is seen,
    # so the unquoted spelling has to go quiet too.
    ('the same, with an unquoted delimiter',
     'git commit -aqF "$(cat <<MSG\nsubject\n\nhe asked "a question\n'
     'spanning lines" here\nMSG\n)" && git push', False, False, ''),
    # The other direction: with the body now visible, an unquoted delimiter
    # inside the substitution expands, so a real read there is a real read.
    ('PIPESTATUS in an unquoted heredoc inside a quoted substitution',
     'git commit -aqF "$(cat <<MSG\nnote: ${PIPESTATUS[0]} was empty\nMSG\n)"',
     False, True, 'reads $PIPESTATUS to recover'),
    ('a gate piped inside a quoted substitution',
     'echo "$(make check | tail -30)"', False, True, 'piped into a filter'),
    ('a shift inside a quoted substitution is not a heredoc',
     'echo "$((1<<3))" && git push', False, False, ''),
    # An ODD number of quotes in the body defeats `_scan_dollar_paren`'s flat
    # quote tracking, so no body is found to recurse into and the strip does not
    # happen. shlex then aborts on the unbalanced quote and the whole call
    # defers -- silence, which is the direction a hook is allowed to fail in.
    ('an unbalanced quote in the body defers rather than denying',
     'git commit -aqF "$(cat <<\'MSG\'\nhe said "hello there\nMSG\n)" && git push',
     False, False, ''),
    ('grep for the pattern in docs', 'grep -rn "make check | tail" docs/',
     False, False, ''),
    ('single-quoted PIPESTATUS is text, not a read',
     "grep -rn '$PIPESTATUS' docs/", False, False, ''),
    ('echo of the offending form', 'echo "never run: make check | tail"',
     False, False, ''),

    # --- The break-glass prefix ----------------------------------------------
    ('override on a piped gate',
     'EXIT_STATUS_GUARD_OVERRIDE=want-the-output-only make check | tail -30',
     False, False, ''),
    ('override on a lost background status',
     'EXIT_STATUS_GUARD_OVERRIDE=log-only make check > tmp/c.log 2>&1; echo "EXIT=$?"',
     True, False, ''),
    ('override on a PIPESTATUS read',
     'EXIT_STATUS_GUARD_OVERRIDE=demonstrating-the-bug echo $PIPESTATUS', False, False, ''),
    ('override as its own statement',
     'EXIT_STATUS_GUARD_OVERRIDE=scoped-to-this-call; make check | tail -5',
     False, False, ''),
    ('quoted override value',
     'EXIT_STATUS_GUARD_OVERRIDE="reading output, not status" make check | tail -5',
     False, False, ''),
    # An empty value is the switch-it-off form, so it buys nothing.
    ('empty override still denies', 'EXIT_STATUS_GUARD_OVERRIDE= make check | tail -30',
     False, True, ''),
    ('override named in a commit message',
     'git commit -m "docs: EXIT_STATUS_GUARD_OVERRIDE=x make check | tail is the escape"',
     False, False, ''),
    ('override quoted, gate really piped',
     'echo "EXIT_STATUS_GUARD_OVERRIDE=x" | make check | tail -5', False, True, ''),
    # Q170: bash strips a word's quotes AFTER deciding what the word is, so
    # `'NAME=v'` is a program it looks for and fails to find -- it arms
    # nothing. Written as its own statement, so the gate stays the head of the
    # segment after it and the deny is what the arming would have lifted; the
    # inline `'NAME=v' make check | tail` form makes `make` an argument
    # instead, and bash runs no gate there for a status to be lost from. The
    # quoted-VALUE rows above are the other direction and must keep passing.
    ('a single-quoted override statement arms nothing',
     "'EXIT_STATUS_GUARD_OVERRIDE=why'; make check | tail -5", False, True, ''),
    ('a double-quoted override statement arms nothing',
     '"EXIT_STATUS_GUARD_OVERRIDE=why"; make check | tail -5', False, True, ''),
    ('a quoted `=` in an override statement arms nothing',
     'EXIT_STATUS_GUARD_OVERRIDE"="why; make check | tail -5', False, True, ''),
    # Q170's keyword half. `strip_sh_keywords` matched the quote-stripped
    # word, so a quoted reserved word came off and the assignment behind it
    # was read as the override. Bash runs `'if' NAME=v :` as a program called
    # `if` and sets nothing, so the gate in the next segment really does lose
    # its status -- and the guard was disarmed by a word the shell never
    # honoured. `'time'` is the sharpest: unquoted it IS the keyword.
    ('a single-quoted keyword arms nothing',
     "'if' EXIT_STATUS_GUARD_OVERRIDE=why :; make check | tail -5", False, True, ''),
    ('a double-quoted keyword arms nothing',
     '"if" EXIT_STATUS_GUARD_OVERRIDE=why :; make check | tail -5', False, True, ''),
    ('a quoted `time` arms nothing',
     "'time' EXIT_STATUS_GUARD_OVERRIDE=why :; make check | tail -5", False, True, ''),
    # The controls: written plainly these are reserved words, bash reaches the
    # assignment, and the override stands.
    ('a plain keyword still arms',
     'if EXIT_STATUS_GUARD_OVERRIDE=why :; then :; fi; make check | tail -5',
     False, False, ''),
    ('a plain `time` still arms',
     'time EXIT_STATUS_GUARD_OVERRIDE=why :; make check | tail -5', False, False, ''),
    # The same peel, read the other way round: with the keyword quoted, `make`
    # is an ARGUMENT to a program bash cannot find, so no gate runs and there
    # is no status to lose. Denying it was a false positive on a command that
    # never executes.
    ('a quoted keyword makes the gate an argument',
     "'if' make check | tail -5", False, False, ''),
    ('a plain keyword leaves the gate a gate',
     'if make check | tail -5; then :; fi', False, True, ''),
    ('a plain `time` leaves the gate a gate',
     'time make check | tail -5', False, True, ''),
    ('a different variable is not the override',
     'EXIT_STATUS_GUARD=x make check | tail -30', False, True, ''),
    # `env` is the other direction again, and the one `peel_wrappers` has to
    # get right twice in one loop: its first pass is command position, and the
    # pass after `env` comes off is OPERAND position, where bash has already
    # removed the quotes. So `env 'A=1' make check | tail` really does run the
    # gate and really does lose its status. Driven on bash 5.3.15:
    # `env 'A=1' bash -c 'echo $A'` prints 1.
    ('a quoted env operand still reaches the gate',
     "env 'A=1' make check | tail -5", False, True,
     "exit status is the filter's"),
    ('a double-quoted env operand still reaches the gate',
     'env "A=1" make check | tail -5', False, True,
     "exit status is the filter's"),
    ('several quoted env operands still reach the gate',
     "env 'A=1' 'B=2' go test ./... | tail -5", False, True,
     "exit status is the filter's"),
    # The control for those three: `nohup` does NOT consume an assignment
    # operand -- it execs a program called `A=1` and exits 127 -- so no gate
    # runs and the guard must stay quiet. Same for `command`.
    ('a quoted operand after nohup runs no gate',
     "nohup 'A=1' make check | tail -5", False, False, ''),
    ('a quoted operand after command runs no gate',
     "command 'A=1' make check | tail -5", False, False, ''),
    ('the 1.x prefix still lifts a deny',
     'PIPE_GUARD_OVERRIDE=want-the-output-only make check | tail -30',
     False, False, ''),
    # `NAME+=v` is an assignment in command position, so it arms like `NAME=v`
    # (Q174). bash 5.3.15: `SG_OVR+=x printenv SG_OVR` prints `x`.
    ('an append-spelled override lifts a deny',
     'EXIT_STATUS_GUARD_OVERRIDE+=want-the-output-only make check | tail -30',
     False, False, ''),
    # The inline row above cannot fail on its own: a guard that misreads the
    # prefix leaves it as the command head, which hides the gate and so denies
    # nothing either way. As its own statement the gate is still in plain view,
    # so only a guard that reads the append spelling keeps this from denying.
    ('an append-spelled override as its own statement',
     'EXIT_STATUS_GUARD_OVERRIDE+=scoped-to-this-call; make check | tail -5',
     False, False, ''),
    ('an empty append override still denies',
     'EXIT_STATUS_GUARD_OVERRIDE+= make check | tail -30', False, True, ''),
    # A `+` anywhere but directly before the `=` is part of no operator, so the
    # word is a command name and the assignment run has ended. As its own
    # statement, so the gate stays in plain view: inline, the unparsed word
    # takes the command head and hides `make check`, and the resulting silence
    # would say nothing about whether the override armed.
    ('a plus inside the name does not arm the override',
     'EXIT_STATUS+_GUARD_OVERRIDE=x; make check | tail -30', False, True, ''),
    ('empty 1.x prefix still denies',
     'PIPE_GUARD_OVERRIDE= make check | tail -30', False, True, ''),
    ('the 1.x prefix named in a commit message',
     'git commit -m "docs: PIPE_GUARD_OVERRIDE=x was the 1.x escape hatch"',
     False, False, ''),

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
    # `pipefail` is a pipe mitigation; it does not re-raise a status the last
    # statement already discarded.
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
    # Measured 2026-09-06 on 2.0.1, backgrounded: denied with "`make check`
    # runs in the background, but this call's exit status is its LAST
    # statement's -- an echo exits 0 whatever the gate did". The `|` inside
    # `$((rc|rc2))` had tokenized as a pipe, so the `exit` re-raising both
    # statuses read as a pipeline ending in `rc2`.
    ('exit re-raising two statuses through an arithmetic expansion',
     'cd /repo; S=/scratch; make check > $S/check1.log 2>&1; rc=$?; '
     'echo "EXIT=$rc" >> $S/check1.log; '
     'make queue MERGED=true > $S/queue1.log 2>&1; rc2=$?; '
     'echo "EXIT=$rc2" >> $S/queue1.log; exit $((rc|rc2))', True, False, ''),
    # The trigger was the unquoted `$((`, not the `|`: `$((rc+rc2))` denied too.
    ('exit summing two statuses through an arithmetic expansion',
     'make check > tmp/c.log 2>&1; rc=$?; make lint > tmp/l.log 2>&1; rc2=$?; '
     'exit $((rc+rc2))', True, False, ''),
    # The mask reaches only the `$((` spelling, and only the shlex input.
    ('a pipe outside the arithmetic is still a pipe',
     'make check | tail -5; exit $((rc|rc2))', False, True,
     "exit status is the filter's"),
    ('a spaced $( (…) ) is a substitution around a subshell, not arithmetic',
     'out=$( (make check | tail -1) )', False, True,
     "exit status is the filter's"),
    ('PIPESTATUS read inside arithmetic is still a read',
     'make check | tail; exit $((${PIPESTATUS[0]}|0))', False, True,
     'reads $PIPESTATUS to recover'),

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
    # A continuation joins one logical line, so this is the `&&` form above
    # written across two lines -- it must read as `&&`, not as a sequence (#8).
    ('&& across a line continuation',
     'make check \\\n  && git push', False, False, ''),
    ('&& across a continuation, indented and repeated',
     'make check \\\n  && git add . \\\n  && git commit -m x',
     False, False, ''),
    # The other direction: a bare newline really does run the second command
    # whatever the first returned, so the boundary itself has to keep denying.
    ('a bare newline before a mutator is still a sequence',
     'make check\ngit push', False, True, 'is sequenced before'),
    # Measured 2026-09-06 on 2.0.1: denied with "`git commit -q -F -` is
    # sequenced before `git commit -q -m docs(queue): complete Q131` with
    # `;`" -- and the command holds no `;`. The separator is the newline
    # after the heredoc line, which really does run the second commit whatever
    # the first returned, so the deny stands and the reason names the newline.
    ('heredoc commit, a newline, then a second commit',
     'git reset -q -- docs/queue/Q131.md && echo "=== staged" && '
     "git diff --cached --stat && git commit -q -F - <<'MSG'\n"
     'fix(bash,hook): one\nMSG\n'
     'echo "commit1: $(git log --oneline -1)" && git add -u docs/queue/Q131.md '
     '&& git diff --cached --stat && git commit -q -m "docs(queue): complete '
     'Q131" && echo done', False, True, 'with a newline'),
    # The rewrite that reason hands over: the `&&` on the line carrying `<<`,
    # ahead of the body. Bash reads the body after the whole line.
    ('heredoc commit joined with && on the << line',
     "git commit -q -F - <<'MSG' && git add -u docs/queue/Q131.md && "
     'git commit -q -m "docs(queue): complete Q131"\nfix(bash,hook): one\nMSG',
     False, False, ''),
    ('&& then a trailing mutator is still gated',
     'make check && git add . && git commit -m x', False, False, ''),
    ('two gates, no state change', 'make lint; make test', False, False, ''),
    ('mutator first, gate second', 'git push; make check', False, False, ''),
    ('non-gate before a mutator', 'echo hi; git push', False, False, ''),
    # Inside a subshell the sequence is that subshell's own business.
    ('sequence nested in a subshell', '(make check; git push)', False, False, ''),
    ('sequence named in a commit message',
     'git commit -m "docs: never write make check; git push"', False, False, ''),

    # --- The capture-and-restore rewrite the deny itself recommends ----------
    # SEQUENCED_REASON hands over `cmd > log; rc=$?; restore; [ "$rc" -ne 0 ]
    # || exit 1` for a gate whose failure is the assertion. Where the restore is
    # itself a registry mutator the guard used to deny its own suggestion, so a
    # session copying it verbatim was denied a second time (Q106).
    ('the recommended restore form with git reset',
     'make check > tmp/c.log 2>&1; rc=$?; git reset --hard; [ "$rc" -ne 0 ] || exit 1',
     False, False, ''),
    ('the recommended restore form with kubectl delete',
     'make check > tmp/c.log 2>&1; rc=$?; kubectl delete -f f.yaml; '
     '[ "$rc" -ne 0 ] || exit 1', False, False, ''),
    ('a restore whose capture is read by an echo',
     'make check > tmp/c.log 2>&1; rc=$?; helm uninstall rel; echo "EXIT=$rc"',
     False, False, ''),
    # shlex removes the quoting, so the defensive spelling is the same capture.
    ('a quoted capture is the same capture',
     'make check > tmp/c.log 2>&1; rc="$?"; git reset --hard; echo "EXIT=$rc"',
     False, False, ''),
    # A two-step teardown. Most restores are gates too, so the second step is
    # what regresses if a skipped restore is allowed to become the new gate:
    # the capture search would restart after it and find nothing.
    ('a teardown of more than one step',
     'make check > tmp/c.log 2>&1; rc=$?; git reset --hard; '
     'kubectl delete -f x.yaml; [ "$rc" -ne 0 ] || exit 1', False, False, ''),

    # The other direction, one condition dropped at a time. Each of the three
    # is load-bearing on its own, which is what keeps the exemption from
    # widening into "a sequence with an assignment in it".
    ('a restore with no capture at all',
     'make check; git reset --hard', False, True, 'is sequenced before'),
    ('a capture the restore is not followed by a read of',
     'make check > tmp/c.log 2>&1; rc=$?; git reset --hard', False, True, ''),
    ('a recheck with no capture to read',
     'make check > tmp/c.log 2>&1; git reset --hard; [ "$rc" -ne 0 ] || exit 1',
     False, True, ''),
    # The screen that matters most: capturing a status does not make a publish
    # conditional on it, so the same shape around `git push` still denies.
    ('a publish in the capture form is not a restore',
     'make check > tmp/c.log 2>&1; rc=$?; git push; [ "$rc" -ne 0 ] || exit 1',
     False, True, 'git push'),
    ('a publish after a legitimate restore still denies',
     'make check > tmp/c.log 2>&1; rc=$?; git reset --hard; git push; '
     '[ "$rc" -ne 0 ] || exit 1', False, True,
     '`make check` is sequenced before `git push`'),
    # An inline assignment sets a variable for one command and leaves nothing
    # for a later segment to read, so it is not a capture.
    # A capture inside a subshell dies with it, so `$rc` outside is empty and
    # the recheck it appears to feed tests nothing.
    ('a capture inside a subshell escapes nothing',
     'make check; (rc=$?); git reset --hard; echo "EXIT=$rc"', False, True, ''),
    ('an inline assignment is not a capture',
     'make check > tmp/c.log 2>&1; rc=$? git reset --hard; [ "$rc" -ne 0 ] || exit 1',
     False, True, ''),
    # A restore is a mutator first: the screens still run, so a read of the
    # same subcommand is neither.
    ('kubectl rollout undo is a restore, not a free pass',
     'make check; kubectl rollout undo deploy/foo', False, True, ''),

    # --- A mutation hanging off a test of the captured status ---------------
    # The restore form with the order reversed: capture, test, and join the
    # publish to the test with `&&`. The status is consulted, not ignored.
    ('a publish gated on a test of the capture',
     'make check > tmp/c.log 2>&1; rc=$?; [ "$rc" -eq 0 ] && git push',
     False, False, ''),
    ('the [[ spelling of the test',
     'make check > tmp/c.log 2>&1; rc=$?; [[ $rc -eq 0 ]] && git push',
     False, False, ''),
    ('the test builtin',
     'make check > tmp/c.log 2>&1; rc=$?; test "$rc" -eq 0 && git push',
     False, False, ''),
    # Every command in an unbroken `&&` chain ran only because the test passed.
    ('a publish further along the tested chain',
     'make check > tmp/c.log 2>&1; rc=$?; [ "$rc" -eq 0 ] && echo ok && git push',
     False, False, ''),
    ('a push status gating a workflow dispatch, with the log read between',
     'git push -u origin HEAD > tmp/push.log 2>&1; rc=$?; echo "push EXIT=$rc"; '
     'tail -3 tmp/push.log; [ "$rc" -eq 0 ] && gh workflow run e2e.yml --ref b '
     '&& gh run list --workflow e2e.yml --limit 3', False, False, ''),
    # The other direction: a test is what makes the chain conditional, and the
    # chain has to reach the mutator unbroken.
    ('a read of the capture is not a test of it',
     'make check > tmp/c.log 2>&1; rc=$?; echo "$rc" && git push',
     False, True, 'is sequenced before'),
    ('a test of some other variable',
     'make check > tmp/c.log 2>&1; rc=$?; [ "$other" -eq 0 ] && git push',
     False, True, ''),
    ('a test the publish is sequenced after rather than joined to',
     'make check > tmp/c.log 2>&1; rc=$?; [ "$rc" -eq 0 ]; git push',
     False, True, ''),
    ('a publish on the || side of the test',
     'make check > tmp/c.log 2>&1; rc=$?; [ "$rc" -eq 0 ] && echo ok || git push',
     False, True, ''),
    ('a second publish outside the tested chain',
     'make check > tmp/c.log 2>&1; rc=$?; [ "$rc" -eq 0 ] && git push; gh release create v1',
     False, True, 'gh release create'),
    ('a test with no capture to read',
     'make check > tmp/c.log 2>&1; [ "$rc" -eq 0 ] && git push',
     False, True, ''),

    # --- Read forms of a subcommand that also writes -------------------------
    # `git tag` and `kubectl rollout` each have a read form and a write form
    # under one subcommand, so a head match alone casts the read as the publish
    # (#11). A read changes no state, so there is nothing for `&&` to gate and
    # the deny has no correct rewrite.
    ('bare git tag lists', 'make check; git tag', False, False, ''),
    ('git tag -l lists', 'make check; git tag -l sometag', False, False, ''),
    ('git tag --list lists', 'make check; git tag --list', False, False, ''),
    ('git tag -n lists with annotations', 'make check; git tag -n5', False, False, ''),
    ('kubectl rollout status reads',
     'make check; kubectl rollout status deploy/foo', False, False, ''),
    # The roles landed exactly backwards here: the write was picked as the gate
    # and the read as the mutator.
    ('a write then a read of the same subcommand',
     'git tag -a t4 -m t4 HEAD; git tag -l t4', False, False, ''),
    # A probe changes nothing either, and `is_mutator` screened neither.
    ('a probe is not a state change', 'make check; git push --help', False, False, ''),
    ('a dry run publishes nothing',
     'make check; kubectl apply --dry-run=client -f x.yaml', False, False, ''),
    # The listing forms are reads for the pipe rule too: output is the point.
    ('git tag piped', 'git tag | tail -5', False, False, ''),
    ('git tag -l piped', 'git tag -l "v1.*" | head', False, False, ''),
    ('kubectl rollout history piped',
     'kubectl rollout history deploy/foo | tail', False, False, ''),

    # The other direction, which is how an over-broad exemption would show:
    # every write form of the same subcommand still has to classify.
    ('git tag -a writes', 'make check; git tag -a v1.0.0 -m "release"',
     False, True, 'is sequenced before'),
    ('git tag -d writes', 'make check; git tag -d v1.0.0', False, True, ''),
    ('git tag -f writes', 'make check; git tag -f v1.0.0', False, True, ''),
    # `git tag <name>` with no flag at all creates the tag.
    ('git tag with a name writes', 'make check; git tag v1.0.0', False, True, ''),
    ('kubectl rollout restart writes',
     'make check; kubectl rollout restart deploy/foo', False, True, ''),
    ('kubectl rollout undo writes',
     'make check; kubectl rollout undo deploy/foo', False, True, ''),
    ('kubectl apply still writes', 'make check; kubectl apply -f x.yaml',
     False, True, ''),
    # `rollout status` is exempted from the mutator list, not from the gates:
    # it waits for a condition, so a pipe still swallows the answer.
    ('kubectl rollout status is still a gate',
     'kubectl rollout status deploy/foo | tail -5', False, True,
     "exit status is the filter's"),

    # --- Read forms the enumeration missed (#19) -----------------------------
    # `git tag`'s listing flags were named one at a time, and git has more of
    # them than the row did: `--sort` and friends were read as gates and denied
    # for being piped. The write forms are registered now and the rest is a
    # read, so the side that grows every git release is the unregistered one.
    ('git tag --sort lists', 'git tag --sort=-v:refname | head -5',
     False, False, ''),
    ('git tag --column lists', 'git tag --column | head', False, False, ''),
    ('git tag -i lists', 'git tag -i | head', False, False, ''),
    ('git tag --omit-empty lists', 'git tag --omit-empty | head',
     False, False, ''),
    # Quoted, which is how anyone writes it. Unquoted, `(` splits the segment
    # and the head truncates before the gate -- silence for the wrong reason.
    ('git tag --format lists', 'git tag --format=\'%(refname)\' | head -3',
     False, False, ''),
    ('git tag --sort before a state change is still a read',
     'make check; git tag --sort=-v:refname', False, False, ''),
    # `git stash` and `git worktree` had no read exemption at all: the `list`
    # row was `gh`-only. A read verb in the subcommand path is structural now.
    ('git stash list', 'git stash list | head', False, False, ''),
    ('git stash show', 'git stash show -p | head', False, False, ''),
    ('git worktree list', 'git worktree list | head', False, False, ''),
    ('git worktree list before a state change',
     'make check; git worktree list', False, False, ''),

    # The other direction. Every write form of the same three subcommands, and
    # the two shapes an over-broad read screen would swallow: a read verb in an
    # operand, and one that is a target name rather than a subcommand.
    ('git tag -a is still a gate', 'git tag -a v1.0.0 -m release | tee log',
     False, True, "exit status is the filter's"),
    ('git tag with a name is still a gate', 'git tag v1.0.0 | tee log',
     False, True, ''),
    ('bundled write flags write', 'make check; git tag -am release v1.0.0',
     False, True, 'is sequenced before'),
    ('long write flags write', 'make check; git tag --annotate v1 --message x',
     False, True, ''),
    ('git tag -s writes', 'make check; git tag -s v1.0.0 -m release',
     False, True, ''),
    ('git tag --delete writes', 'make check; git tag --delete v1.0.0',
     False, True, ''),
    ('git stash is still a gate', 'git stash | tee log', False, True, ''),
    ('git stash push is still a gate', 'git stash push | tee log',
     False, True, ''),
    ('git worktree add is still a gate', 'git worktree add ../wt | tee log',
     False, True, ''),
    ('a read verb as an operand is not a read',
     'make check; git commit -m show', False, True, 'is sequenced before'),
    ('a make target named show is not a read', 'make show | tail',
     False, True, "exit status is the filter's"),
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
        for key in ('gates', 'exempt', 'mutators', 'restores'):
            for p in data.get(key) or []:
                with self.subTest(p):
                    self.assertTrue(p.startswith('^'),
                                    'pattern not anchored to command position')

    def test_every_restore_is_also_a_mutator(self):
        """`is_restore` is consulted only after `is_mutator` has said yes, so a
        restore naming a command the mutator list misses is a dead entry --
        registered, never reached, and silent about it."""
        reg = shipped_registry()
        self.assertTrue(reg.restores, 'registry lists no restores')
        for cmd in ('git reset --hard', 'kubectl delete -f x.yaml',
                    'kubectl rollout undo deploy/foo', 'helm uninstall rel',
                    'helm rollback rel 1', 'terraform destroy -auto-approve',
                    'tofu destroy'):
            words = cmd.split()
            with self.subTest(cmd):
                self.assertTrue(reg.is_restore(words), 'not matched as a restore')
                self.assertTrue(reg.is_mutator(words),
                                'a restore the mutator list does not reach')

    def test_a_restore_that_is_not_registered_stays_a_plain_mutator(self):
        """The other direction: `git push` is a mutator and must never read as
        a restore, whatever shape it is sequenced in."""
        reg = shipped_registry()
        for cmd in ('git push', 'npm publish', 'kubectl apply -f x.yaml'):
            with self.subTest(cmd):
                self.assertFalse(reg.is_restore(cmd.split()))

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
            with open(os.path.join(root, '.claude', 'exit-status-guard.json'), 'w') as fh:
                json.dump({'gates': [r'^bazelisk(\s|$)']}, fh)
            reg = pg.load_registry(root)
            self.assertTrue(pg.decide('bazelisk test //... | tail', False, reg))
            self.assertTrue(pg.decide('make check | tail', False, reg),
                            'a project file must not drop the defaults')

    def test_a_project_exempt_separates_targets_sharing_a_prefix(self):
        """`make backlog` renders the queue and `make backlog-lint` checks it.

        Only the project's Makefile knows which of the two asserts something,
        so widening the shipped `exempt` alternation cannot tell them apart --
        the first assertion is what fails if anyone tries.
        """
        exempt = [r'^make(\s+-C\s+\S+)?\s+(backlog|backlog-next)(\s|$)']
        self.assertTrue(
            pg.decide('make backlog | head', False, shipped_registry()),
            'the shipped registry must not guess a project target')
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, '.claude'))
            with open(os.path.join(root, '.claude', 'exit-status-guard.json'), 'w') as fh:
                json.dump({'exempt': exempt}, fh)
            reg = pg.load_registry(root)
            self.assertFalse(pg.decide('make backlog | head', False, reg))
            self.assertTrue(pg.decide('make backlog-lint | tail -5', False, reg),
                            'a longer target sharing the prefix is still a gate')

    def test_project_file_can_replace_the_defaults(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, '.claude'))
            with open(os.path.join(root, '.claude', 'exit-status-guard.json'), 'w') as fh:
                json.dump({'replace': True, 'gates': [r'^bazelisk(\s|$)']}, fh)
            reg = pg.load_registry(root)
            self.assertTrue(pg.decide('bazelisk test //... | tail', False, reg))
            self.assertFalse(pg.decide('make check | tail', False, reg))


class TestLegacyNames(unittest.TestCase):
    """The 1.x spellings still work, and the current ones still win.

    The rename does not reach a downstream repo's `.claude/` directory or the
    docs that tell a session which prefix to type, so both keep working. The
    negative direction is the one that matters: a fallback that shadows the
    current name would make a rename invisible to the repo that performed it.
    """

    def project(self, root, name, gates):
        os.makedirs(os.path.join(root, '.claude'), exist_ok=True)
        with open(os.path.join(root, '.claude', name), 'w') as fh:
            json.dump({'gates': gates}, fh)

    def test_the_1x_project_filename_is_still_read(self):
        with tempfile.TemporaryDirectory() as root:
            self.project(root, 'pipe-guard.json', [r'^bazelisk(\s|$)'])
            reg = pg.load_registry(root)
            self.assertTrue(pg.decide('bazelisk test //... | tail', False, reg))

    def test_the_current_filename_wins_over_the_1x_one(self):
        with tempfile.TemporaryDirectory() as root:
            self.project(root, 'pipe-guard.json', [r'^bazelisk(\s|$)'])
            self.project(root, 'exit-status-guard.json', [r'^pants(\s|$)'])
            reg = pg.load_registry(root)
            self.assertTrue(pg.decide('pants test :: | tail', False, reg))
            self.assertFalse(pg.decide('bazelisk test //... | tail', False, reg),
                             'the 1.x file must not be merged in as well')

    def test_the_1x_registry_env_var_is_still_read(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'reg.json')
            with open(path, 'w') as fh:
                json.dump({'gates': [r'^pants(\s|$)']}, fh)
            os.environ['PIPE_GUARD_REGISTRY'] = path
            try:
                reg = pg.load_registry()
            finally:
                del os.environ['PIPE_GUARD_REGISTRY']
            self.assertTrue(pg.decide('pants test :: | tail', False, reg))
            self.assertFalse(pg.decide('make check | tail', False, reg),
                             'the named registry replaces the shipped one')


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
        self.assertIn('reads $PIPESTATUS to recover', reason)

    def test_every_reason_carries_the_override(self):
        reg = shipped_registry()
        for cmd, bg in (('make check | tail', False),
                        ('make check > c.log 2>&1; echo hi', True),
                        ('make check; git push', False),
                        ('echo $PIPESTATUS', False)):
            with self.subTest(cmd):
                self.assertIn('EXIT_STATUS_GUARD_OVERRIDE=<reason>',
                              pg.decide(cmd, bg, reg))

    def test_every_reason_names_the_project_registry(self):
        """The override and the issue tracker are both per-call exits. A
        session denied on a report its project runs routinely has no other way
        to learn the durable fix exists."""
        reg = shipped_registry()
        for cmd, bg in (('make check | tail', False),
                        ('make check > c.log 2>&1; echo hi', True),
                        ('make check; git push', False),
                        ('echo $PIPESTATUS', False)):
            with self.subTest(cmd):
                self.assertIn('.claude/exit-status-guard.json',
                              pg.decide(cmd, bg, reg))

    def test_no_reason_names_the_shell_the_tool_runs(self):
        """Which shell the Bash tool runs is a per-machine setting the guard
        never reads, so a reason that names one is wrong wherever the setting
        differs -- and a remedy derived from it is worse than none. Both
        PIPESTATUS reasons asserted zsh and prescribed `$pipestatus`, which is
        not an array in bash and expands to empty.

        `zsh` is the whole assertion because it is the only shell either reason
        ever named; `bash` is not, since a reason may legitimately say the
        array is a bash feature.
        """
        reg = shipped_registry()
        for cmd, bg in (('make check | tail', False),
                        ('make check > c.log 2>&1; echo hi', True),
                        ('make check; git push', False),
                        ('echo $PIPESTATUS', False),
                        ('make check | tail; echo ${PIPESTATUS[0]}', False)):
            with self.subTest(cmd):
                reason = pg.decide(cmd, bg, reg)
                self.assertTrue(reason)
                self.assertNotIn('zsh', reason.lower())


class TestSequencedRemedy(unittest.TestCase):
    """`&&` is the answer for most sequencing denials, and not for all of them.

    A mutation control mutates a file, runs a gate that is *required* to fail,
    and restores. `&&` skips the restore on the expected failure and leaves the
    tree mutated, so the reason names the capture form beside it -- second, and
    scoped to the case, because the common denial does want `&&`.
    """

    def setUp(self):
        self.reason = pg.decide('make check; git push', False,
                                shipped_registry(), '/scratch')

    def test_the_and_form_still_leads(self):
        self.assertIn('Join them with `&&`', self.reason)
        self.assertLess(self.reason.index('Join them with `&&`'),
                        self.reason.index('rc=$?'),
                        'the common case has to be read first')

    def test_the_capture_form_is_named(self):
        self.assertIn('rc=$?; restore; [ "$rc" -ne 0 ] || exit 1',
                      self.reason)


class TestSequencedSeparator(unittest.TestCase):
    """The reason names the separator it found.

    A newline sequences as `;` does, and a denial naming `;` for one reads as
    a parser fault to the session it reaches: its command holds none, so the
    rewrite the reason carries is not tried. The heredoc is the shape that
    leaves a newline between a gate and a mutator, and it is also where the
    `&&` has nowhere obvious to go, so that reason says where.
    """

    HEREDOC = "git commit -F - <<'MSG'\nsubject\nMSG\ngit push"

    @classmethod
    def setUpClass(cls):
        cls.reg = shipped_registry()

    def test_semicolon_is_named(self):
        reason = pg.decide('make check; git push', False, self.reg, '/scratch')
        self.assertIn('` with `;`, which runs', reason)
        self.assertNotIn('newline', reason)

    def test_newline_is_named(self):
        reason = pg.decide('make check\ngit push', False, self.reg, '/scratch')
        self.assertIn('` with a newline, which', reason)
        self.assertNotIn('with `;`', reason)

    def test_the_heredoc_reason_places_the_and(self):
        reason = pg.decide(self.HEREDOC, False, self.reg, '/scratch')
        self.assertIn('` with a newline', reason)
        self.assertIn("`cmd <<'EOF' && next`", reason)
        self.assertLess(reason.index('Join them with `&&`'),
                        reason.index('rc=$?'),
                        'the common case has to be read first')
        self.assertTrue(reason.endswith(pg.OVERRIDE_TAIL),
                        'the override stays last')

    def test_the_placed_and_is_the_correct_form(self):
        """The rewrite has to run: bash reads the body after the whole line."""
        self.assertEqual('', pg.decide(
            "git commit -F - <<'MSG' && git push\nsubject\nMSG",
            False, self.reg, '/scratch'))


class TestSuggestedLogPath(unittest.TestCase):
    """The rewrite a denied session copies has to be a command that runs.

    `tmp/` is a build-output name, commonly gitignored and so absent from a
    fresh checkout. A redirect into a directory that is not there fails before
    the gate runs, and reports a status that cannot be told apart from a gate
    that ran and failed -- handed to a session at the moment it is copying the
    text verbatim.
    """

    # One denial per reason that names a log file.
    DENIALS = (('make check | tail -5', False),
               ('make check > c.log 2>&1; echo "EXIT=$?"', True),
               ('echo $PIPESTATUS', False),
               ('make check; git push', False))

    @classmethod
    def setUpClass(cls):
        cls.reg = shipped_registry()

    def test_every_template_carries_both_placeholders(self):
        """A template that loses one names no path, or names one uncreated."""
        for name in ('PIPESTATUS_REASON', 'PIPED_REASON', 'LOST_STATUS_REASON',
                     'SEQUENCED_REASON', 'SEQUENCED_NEWLINE_REASON'):
            with self.subTest(name):
                template = getattr(pg, name)
                self.assertIn(pg.LOG_PLACEHOLDER, template)
                self.assertIn(pg.MKDIR_PLACEHOLDER, template)

    def test_no_placeholder_reaches_the_model(self):
        for cmd, bg in self.DENIALS:
            for scratch in ('', '/scratch'):
                with self.subTest(cmd=cmd, scratch=scratch):
                    reason = pg.decide(cmd, bg, self.reg, scratch)
                    self.assertNotIn(pg.LOG_PLACEHOLDER, reason)
                    self.assertNotIn(pg.MKDIR_PLACEHOLDER, reason)

    def test_scratchpad_is_named_when_there_is_one(self):
        for cmd, bg in self.DENIALS:
            with self.subTest(cmd):
                reason = pg.decide(cmd, bg, self.reg, '/scratch')
                self.assertIn('/scratch/out.log', reason)
                self.assertNotIn('tmp/out.log', reason)
                self.assertNotIn('mkdir', reason)

    def test_the_fallback_creates_the_directory_it_names(self):
        """With no scratchpad to name, the suggestion carries its own mkdir."""
        for cmd, bg in self.DENIALS:
            with self.subTest(cmd):
                self.assertIn('mkdir -p tmp && cmd > tmp/out.log',
                              pg.decide(cmd, bg, self.reg))


class TestScratchDir(unittest.TestCase):
    """Resolving the session scratchpad, whose layout is Claude Code's own."""

    SESSION = '2d9352ff-105f-4c53-b2a2-9c13f8ce5cae'

    def test_found_by_scanning_for_the_session(self):
        """Found without knowing the slug, which is undocumented and varies."""
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, '-Users-someone-other-project',
                                     'a-different-session', 'scratchpad'))
            want = make_scratchpad(root, self.SESSION)
            self.assertEqual(want, pg.scratch_dir(self.SESSION, root))

    def test_a_path_that_is_not_there_is_never_named(self):
        """A layout that moved underneath this degrades to the mkdir form.

        The root holds another session, so returning '' means the directory was
        confirmed rather than merely assembled.
        """
        with tempfile.TemporaryDirectory() as root:
            make_scratchpad(root, 'a-different-session')
            self.assertEqual('', pg.scratch_dir(self.SESSION, root))

    def test_junk_session_id_builds_no_path(self):
        with tempfile.TemporaryDirectory() as root:
            make_scratchpad(root, self.SESSION)
            for session in ('', '../../etc', 'a/b', self.SESSION + '\n'):
                with self.subTest(session=session):
                    self.assertEqual('', pg.scratch_dir(session, root))

    def test_a_root_that_cannot_be_listed_is_silent(self):
        self.assertEqual('', pg.scratch_dir(self.SESSION, '/no/such/root'))


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

    def test_every_deny_names_the_guard_first(self):
        """The reason is all the model gets, so it is the only place a verdict
        can say which of several installed guards spoke."""
        for cmd, bg in (('make check | tail -5', False),
                        ('make check | tail -5; exit ${PIPESTATUS[0]}', False),
                        ('make check > c.log 2>&1; echo "EXIT=$?"', True),
                        ('make check; git push', False)):
            with self.subTest(cmd):
                out = self.run_hook({'tool_name': 'Bash', 'cwd': REPO,
                                     'tool_input': {'command': cmd,
                                                    'run_in_background': bg}})
                reason = json.loads(out)['hookSpecificOutput'][
                    'permissionDecisionReason']
                self.assertTrue(reason.startswith('exit-status-guard: '),
                                'reason opens %r' % reason[:40])

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

    def test_scratchpad_is_read_from_the_payload(self):
        """The wiring main() does: the payload's session id resolves to a path.

        Asserted through the process rather than against `scratch_dir`, because
        a payload field read under the wrong name would leave every unit test
        green and every suggested rewrite pointing at `tmp/`.
        """
        root = pg.scratch_root()
        if not root:
            self.skipTest('no per-user scratch root on this platform')
        made_root = not os.path.isdir(root)
        session = 'exit-status-guard-suite-%d' % os.getpid()
        project = os.path.join(root, 'exit-status-guard-suite-project')
        path = os.path.join(project, session, 'scratchpad')
        os.makedirs(path, mode=0o700)     # Claude Code's root is per-UID private
        try:
            out = self.run_hook({
                'tool_name': 'Bash', 'cwd': REPO, 'session_id': session,
                'tool_input': {'command': 'make check | tail -5'}})
        finally:
            shutil.rmtree(root if made_root else project)
        reason = json.loads(out)['hookSpecificOutput']['permissionDecisionReason']
        self.assertIn(path + '/out.log', reason)

    def test_suggested_rewrite_needs_no_directory_that_may_not_exist(self):
        """No scratchpad in the payload, so the rewrite has to make its own."""
        out = self.run_hook({
            'tool_name': 'Bash', 'cwd': REPO,
            'tool_input': {'command': 'make check | tail -5'}})
        reason = json.loads(out)['hookSpecificOutput']['permissionDecisionReason']
        self.assertIn('mkdir -p tmp && cmd > tmp/out.log', reason)

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

    def test_heredoc_inside_a_quoted_substitution_is_seen(self):
        """The bug in #23, at the layer it happened: the body was never stripped.

        Asserted on the stripped string rather than the verdict, because the
        verdict only turned on where the body's own quotes fell -- the same
        command went quiet or denied depending on where a line wrapped.
        """
        cmd = ('git commit -aqF "$(cat <<\'MSG\'\nsubject\n\n'
               'he asked "a question\nspanning lines" here\nMSG\n)" && git push')
        cleaned = pg.strip_heredoc_bodies(cmd)
        self.assertNotIn('spanning lines', cleaned)
        self.assertEqual('&&', pg.next_op(self.segs(cmd)[0].post_ops))

    def test_an_unquoted_delimiter_there_still_expands(self):
        """Seeing the heredoc is what makes its delimiter's quoting readable."""
        expanded = []
        pg.strip_heredoc_bodies('echo "$(cat <<MSG\nbody\nMSG\n)"', expanded)
        self.assertEqual(['body\nMSG\n'], expanded)
        expanded = []
        pg.strip_heredoc_bodies("echo \"$(cat <<'MSG'\nbody\nMSG\n)\"", expanded)
        self.assertEqual([], expanded)

    def test_an_unterminated_substitution_still_strips_the_body(self):
        """An unterminated `$(` is quiet, not a crash -- and still sees the heredoc.

        The old copy pre-scanned for the balanced `)` and gave up without one,
        leaving the body in place. The shared parser tracks context as it goes,
        so there is nothing to give up on: the body is dropped to end-of-input,
        which is what bash does with an unterminated heredoc. That is also the
        answer this guard wants -- `make check | tail` written inside a body is
        data, and a copy of it left in the cleaned text is precisely the false
        positive stripping bodies exists to prevent.
        """
        cmd = 'echo "$(cat <<EOF\nmake check | tail\nEOF'
        self.assertEqual('echo "$(cat <<EOF\n', pg.strip_heredoc_bodies(cmd))

    def test_redirect_targets_leave_the_head(self):
        segs = self.segs('make check > tmp/out.log 2>&1')
        self.assertEqual(['make check'],
                         [' '.join(pg.head_words(s)) for s in segs])

    def test_parens_are_transparent_to_the_next_operator(self):
        segs = self.segs('(cd sub && go test ./...) | tail')
        gate = [s for s in segs if pg.head_words(s)[:2] == ['go', 'test']][0]
        self.assertEqual('|', pg.next_op(gate.post_ops))

    def test_continuation_does_not_become_a_boundary(self):
        """The bug in #8: the `&&` was there, behind a newline that hid it."""
        segs = self.segs('make check \\\n  && git push')
        gate = segs[0]
        self.assertEqual(('&&',), gate.post_ops)
        self.assertEqual('&&', pg.next_op(gate.post_ops))

    def test_a_continuation_before_a_pipe(self):
        """The fold is upstream of every rule, so rule 1 sees through it too."""
        segs = self.segs('make check \\\n  | tail -5')
        self.assertEqual(['make check', 'tail -5'],
                         [' '.join(pg.head_words(s)) for s in segs])
        self.assertEqual('|', pg.next_op(segs[0].post_ops))

    def test_a_bare_newline_is_still_a_boundary(self):
        segs = self.segs('make check\ngit push')
        self.assertEqual('\n', pg.next_op(segs[0].post_ops))

    def test_arithmetic_expansion_is_one_word(self):
        """`$((rc|rc2))` holds no pipe: its parens are punctuation to shlex,
        and unmasked the `|` between them split the segment."""
        segs = self.segs('exit $((rc|rc2))')
        self.assertEqual(1, len(segs))
        self.assertEqual(['exit', pg.ARITHMETIC_WORD], segs[0].tokens)

    def test_arithmetic_is_masked_only_on_the_way_into_shlex(self):
        _, cleaned, _ = pg.tokenize('exit $((rc|rc2))')
        self.assertIn('$((rc|rc2))', cleaned)

    def test_a_spaced_substitution_is_not_arithmetic(self):
        segs = self.segs('out=$( (make check | tail -1) )')
        self.assertIn('|', [pg.next_op(s.post_ops) for s in segs])

    def test_arithmetic_in_single_quotes_is_left_to_shlex(self):
        segs = self.segs("echo '$((a|b))' && git push")
        self.assertEqual(['echo', '$((a|b))'], segs[0].tokens)

    def test_a_continuation_in_single_quotes_stays_literal(self):
        segs = self.segs("echo 'a\\\nb' && git push")
        self.assertEqual(['echo', 'a\\\nb'], segs[0].tokens)

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
