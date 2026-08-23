#!/usr/bin/env python3
"""Verify every action in `.github/workflows/` is pinned to a commit SHA.

A tag is a pointer its owner can move. `actions/checkout@v4` runs whatever v4
points at on the day, which makes every CI run in this repository depend on a
third party not rewriting a tag -- in a repository whose whole subject is
refusing to run things unchecked.

Two assertions, because pinning alone only makes the problem quieter:

  1. Every `uses:` names a 40-character commit SHA. A tag or a branch is a
     mutable reference and fails.
  2. Every pin carries a trailing `# <version>` comment. A bare SHA is
     unreviewable -- nobody can tell which release it is without resolving it
     against the network -- and Dependabot reads that comment to know what it
     is bumping from, so a pin without one is frozen for good.

Local actions (`uses: ./...`) are in-tree and exempt: they move with the
commit that runs them. Anything else that is not a SHA fails, which is the
direction to fail in.

Prints one line per assertion and exits 1 on any failure.

  python3 scripts/action-pin-check.py

`read_uses()` and the two `check_*` functions are the importable half.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.path.join(ROOT, '.github', 'workflows')

# `uses:` with an optional `- ` step opener, capturing the reference and
# whatever trailing comment follows it.
USES_RE = re.compile(r'^\s*(?:-\s+)?uses:\s*(\S+)\s*(?:#\s*(.*?))?\s*$')
SHA_RE = re.compile(r'^[0-9a-f]{40}$')


def workflow_files(directory=WORKFLOWS):
    return sorted(os.path.join(directory, name)
                  for name in os.listdir(directory)
                  if name.endswith(('.yml', '.yaml')))


def read_uses(directory=WORKFLOWS):
    """Every `uses:` under the directory, as (file, lineno, value, comment).

    Hand-parsed rather than through a YAML library, for the reason
    path-filter-check.py gives: the tests run on the stdlib alone, and a
    `uses:` line is fixed enough to read directly.
    """
    found = []
    for path in workflow_files(directory):
        with open(path) as f:
            for lineno, line in enumerate(f.read().splitlines(), 1):
                match = USES_RE.match(line)
                if match:
                    found.append((os.path.basename(path), lineno,
                                  match.group(1), match.group(2)))
    return found


def _ref(value):
    """The part after the `@`, or None for a local or unversioned action."""
    _, sep, ref = value.partition('@')
    return ref if sep else None


def check_refs_are_pinned(uses):
    problems = []
    for name, lineno, value, _comment in uses:
        if value.startswith('./'):
            continue                      # in-tree, moves with the commit
        ref = _ref(value)
        if ref is None:
            problems.append('%s:%d %s carries no ref at all'
                            % (name, lineno, value))
        elif not SHA_RE.match(ref):
            problems.append('%s:%d %s is a mutable ref, not a commit SHA'
                            % (name, lineno, value))
    return problems


def check_pins_name_a_version(uses):
    problems = []
    for name, lineno, value, comment in uses:
        ref = _ref(value)
        if ref is None or not SHA_RE.match(ref):
            continue                      # already reported by assertion 1
        if not comment:
            problems.append('%s:%d %s has no trailing `# <version>` comment, '
                            'so nobody can tell which release it pins'
                            % (name, lineno, value))
    return problems


def main():
    uses = read_uses()
    failed = False
    for label, problems in (
            ('every action is SHA-pinned', check_refs_are_pinned(uses)),
            ('every pin names its version', check_pins_name_a_version(uses))):
        print('%-32s %s' % (label, 'ok' if not problems else 'FAILED'))
        for problem in problems:
            print('  %s' % problem)
        failed = failed or bool(problems)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
