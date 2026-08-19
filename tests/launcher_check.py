"""Drive the hook end to end through scripts/run-python-hook.cmd.

The unittest suite runs bash-exit-status-guard.py with sys.executable, so the launcher
-- the thing hooks.json actually invokes -- is never executed. It is a
cmd.exe/POSIX polyglot: two independent code paths, neither of them covered.

A break in it does not fail loudly. Claude Code treats a failed PreToolUse hook
as a non-blocking error and lets the tool call proceed, so a launcher that
stops resolving an interpreter leaves the guard silently unenforcing.

Both directions are asserted, and only the pair is meaningful: a launcher that
never runs the script produces the same empty stdout as a clean command, and
the shim exits 0 on several failure paths by design, so neither emptiness nor
exit status alone can tell a working launcher from a dead one.

Deliberately not named test_*, because `unittest discover` must not pick it up
-- on Windows the launcher needs cmd.exe, not sys.executable.

Usage: python tests/launcher_check.py
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS_JSON = os.path.join(REPO, 'hooks', 'hooks.json')
LAUNCHER = os.path.join('scripts', 'run-python-hook.cmd')
HOOK_SCRIPT = 'bash-exit-status-guard.py'

DENY_COMMAND = 'make check 2>&1 | tail -30'
CLEAN_COMMAND = 'make check > tmp/c.log 2>&1; echo "EXIT=$?"'
DENY_REASON = "exit status is the filter's"


def wired_command():
    """The command line hooks.json wires up, with the plugin root filled in."""
    with open(HOOKS_JSON, encoding='utf-8') as fh:
        hook = json.load(fh)['hooks']['PreToolUse'][0]['hooks'][0]
    return hook['command'].replace('${CLAUDE_PLUGIN_ROOT}', REPO)


def run_launcher(command):
    """Invoke the launcher as hooks.json does, with a hook payload on stdin.

    POSIX runs the wired command line through `sh -c`, not `sh <launcher>`.
    The two differ on exactly one thing: `sh <launcher>` reads the file as a
    script and never needs its execute bit, so it passes on a mode-644 launcher
    that Claude Code cannot run at all -- which is how 1.0.0 shipped a guard
    that had never fired.
    """
    if os.name == 'nt':
        argv = ['cmd', '/c', LAUNCHER, HOOK_SCRIPT]
    else:
        argv = ['sh', '-c', wired_command()]
    payload = {'tool_name': 'Bash', 'cwd': REPO,
               'tool_input': {'command': command}}
    return subprocess.run(argv, cwd=REPO, input=json.dumps(payload),
                          capture_output=True, text=True, timeout=60)


def report(proc):
    return 'exit %s\nstdout: %r\nstderr: %s' % (
        proc.returncode, proc.stdout, proc.stderr.strip())


def check_denies(failures):
    proc = run_launcher(DENY_COMMAND)
    if proc.returncode != 0:
        failures.append('denying payload: launcher failed\n%s' % report(proc))
        return
    try:
        decision = json.loads(proc.stdout)['hookSpecificOutput']
    except (ValueError, KeyError, TypeError):
        failures.append('denying payload: no decision on stdout\n%s'
                        % report(proc))
        return
    if decision.get('permissionDecision') != 'deny':
        failures.append('denying payload: decision was %r, want "deny"\n%s'
                        % (decision.get('permissionDecision'), report(proc)))
    if DENY_REASON not in decision.get('permissionDecisionReason', ''):
        failures.append('denying payload: reason lacks %r\n%s'
                        % (DENY_REASON, report(proc)))


def check_stays_silent(failures):
    proc = run_launcher(CLEAN_COMMAND)
    if proc.returncode != 0:
        failures.append('clean payload: launcher failed\n%s' % report(proc))
        return
    if proc.stdout.strip():
        failures.append('clean payload: stdout was not empty\n%s' % report(proc))


def main():
    failures = []
    check_denies(failures)
    check_stays_silent(failures)

    for failure in failures:
        sys.stderr.write('FAIL: %s\n' % failure)
    if failures:
        sys.stderr.write('%d launcher check(s) failed\n' % len(failures))
        return 1
    print('launcher check OK: %s -> deny, clean command -> silent' % LAUNCHER)
    return 0


if __name__ == '__main__':
    sys.exit(main())
