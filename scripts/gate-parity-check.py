#!/usr/bin/env python3
"""Verify every gate `make check` runs is also run by CI, on every pull request.

`make check` names its gates as the `check:` target's prerequisites, and
`.github/workflows/tests.yml` re-lists them as steps. Nothing kept the two in
step: a gate added to `check:` and never to the workflow passes every local run
and is never run where a pull request is decided, and the checks list shows
nothing missing. That is the same go-green-by-not-running shape
`path-filter-check.py` exists for, arriving through the other list.

A prerequisite counts as reached when a step in a job that runs on every pull
request -- no `if:`, so no path filter can skip it -- runs it from the
repository root, by one of:

  1. `make <target>`, or
  2. the target's own one-line recipe, with `$(PYTHON)` read as `python3`.

Two gates are reached another way, and each is checked on its own terms in
`CARRIED`: `plugin-tests` is one job per plugin, and `backlog-lint` runs inside
the root test suite, which reads its flags out of the recipe.

Prints one line per assertion and exits 1 on any failure.

  python3 scripts/gate-parity-check.py

`read_makefile()`, `read_workflow()` and `check_parity()` are the importable
half.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAKEFILE = os.path.join(ROOT, 'Makefile')
WORKFLOW = os.path.join(ROOT, '.github', 'workflows', 'tests.yml')

TARGET_RE = re.compile(r'^([A-Za-z0-9_.-]+):(?!=)(.*)$')
VAR_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)\s*:?=\s*(.*)$')
JOB_RE = re.compile(r'^  ([a-z0-9][a-z0-9-]*):$')
STEP_RE = re.compile(r'^      - (.*)$')
KEY_RE = re.compile(r'^([a-z-]+):\s*(.*)$')


def _logical_lines(lines):
    """Join backslash-continued lines, as make does."""
    out, buf = [], ''
    for line in lines:
        if line.endswith('\\'):
            buf += line[:-1] + ' '
            continue
        out.append(buf + line)
        buf = ''
    if buf:
        out.append(buf)
    return out


def read_makefile(path=MAKEFILE):
    """(variables, {target: (prerequisites, recipe commands)})."""
    with open(path) as f:
        lines = _logical_lines(f.read().splitlines())
    variables, targets, current = {}, {}, None
    for line in lines:
        if line.startswith('\t'):
            if current:
                targets[current][1].append(line.strip().lstrip('@').strip())
            continue
        current = None
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        var = VAR_RE.match(line)
        if var and not TARGET_RE.match(line):
            variables[var.group(1)] = var.group(2).strip()
            continue
        target = TARGET_RE.match(line)
        if target and not target.group(1).startswith('.'):
            current = target.group(1)
            targets[current] = (target.group(2).split(), [])
    return variables, targets


def read_workflow(path=WORKFLOW):
    """{job: {'guarded': bool, 'steps': [(run command, working dir)]}}.

    Hand-parsed for the same reason as `path-filter-check.py`: the tests run on
    the stdlib alone, and the file's own indentation fixes the shapes.
    """
    with open(path) as f:
        lines = f.read().splitlines()
    jobs, job, step, block, default_wd = {}, None, None, None, None

    def close_step():
        if job and step is not None and step.get('run') is not None:
            jobs[job]['steps'].append(
                (step['run'].strip(), step.get('wd') or default_wd))

    for line in lines:
        job_match = JOB_RE.match(line)
        if job_match:
            close_step()
            job, step, block, default_wd = job_match.group(1), None, None, None
            jobs[job] = {'guarded': False, 'steps': []}
            continue
        if not job:
            continue
        if block is not None:
            if line.startswith(' ' * (block + 1)) or not line.strip():
                step['run'] += line.strip() + '\n'
                continue
            block = None
        if line.startswith('    if:'):
            jobs[job]['guarded'] = True
        elif line.startswith('        working-directory:') and step is None:
            default_wd = line.split(':', 1)[1].strip()
        step_match = STEP_RE.match(line)
        if step_match:
            close_step()
            step, body = {}, step_match.group(1)
        elif step is not None and line.startswith('        ') \
                and not line.startswith('         '):
            body = line.strip()
        else:
            continue
        key = KEY_RE.match(body)
        if not key:
            continue
        if key.group(1) == 'run':
            if key.group(2) in ('|', '>'):
                step['run'], block = '', 8
            else:
                step['run'] = key.group(2)
        elif key.group(1) == 'working-directory':
            step['wd'] = key.group(2)
    close_step()
    return jobs


def _root_commands(jobs):
    """Every command a job runs from the root on every pull request."""
    return [cmd for spec in jobs.values() if not spec['guarded']
            for cmd, wd in spec['steps'] if wd in (None, '.')]


def _normal(cmd):
    return ' '.join(cmd.replace('$(PYTHON)', 'python3').split())


def _reached(target, recipe, commands):
    make = re.compile(r'(^|\s)make\s+(\S+\s+)*%s(\s|$)' % re.escape(target),
                      re.M)
    if any(make.search(cmd) for cmd in commands):
        return True
    if len(recipe) != 1:
        return False
    want = _normal(recipe[0])
    return any(want in _normal(line) or
               want.replace('python3 ', 'python ', 1) in _normal(line)
               for cmd in commands for line in cmd.splitlines())


def _plugin_jobs(variables, targets, jobs):
    """`plugin-tests` loops over $(PLUGINS); CI gives each its own job."""
    missing = []
    for plugin in variables.get('PLUGINS', '').split():
        wd = 'plugins/%s' % plugin
        if not any(w == wd for cmd, w in jobs.get(plugin, {}).get('steps', [])):
            missing.append('plugin-tests: no job %r runs a step in %s'
                           % (plugin, wd))
    return missing


def _carried_by_lib_test(target):
    """The root suite reads this recipe's flags rather than restating them."""
    def check(variables, targets, jobs):
        problems = []
        suite = os.path.join(ROOT, 'tests', 'test_backlog.py')
        with open(suite) as f:
            if "'%s'" % target not in f.read():
                problems.append('%s: tests/test_backlog.py no longer reads its '
                                'flags from the recipe' % target)
        lib = targets.get('lib-test', ([], []))[1]
        if not _reached('lib-test', lib, _root_commands(jobs)):
            problems.append('%s: carried by lib-test, which CI does not run'
                            % target)
        return problems
    return check


CARRIED = {
    'plugin-tests': _plugin_jobs,
    'backlog-lint': _carried_by_lib_test('backlog-lint'),
}


def check_parity(variables, targets, jobs, carried=CARRIED):
    if 'check' not in targets:
        return ['the Makefile has no `check` target']
    commands = _root_commands(jobs)
    problems = []
    for gate in targets['check'][0]:
        if gate in carried:
            problems += carried[gate](variables, targets, jobs)
        elif gate not in targets:
            problems.append('%s: named by `check:` but has no recipe' % gate)
        elif not _reached(gate, targets[gate][1], commands):
            problems.append('%s: no unguarded root step runs `make %s` or its '
                            'recipe' % (gate, gate))
    return problems


def main():
    variables, targets = read_makefile()
    problems = check_parity(variables, targets, read_workflow())
    gates = len(targets.get('check', ([], []))[0])
    print('%-32s %s' % ('every check gate runs in CI',
                        'ok (%d gates)' % gates if not problems else 'FAILED'))
    for problem in problems:
        print('  %s' % problem)
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
