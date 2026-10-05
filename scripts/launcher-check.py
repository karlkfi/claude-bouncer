#!/usr/bin/env python3
"""Verify every plugin's Windows hook launcher is the same file, kept the same way.

Each guard launches its hook through its own copy of `run-python-hook.cmd`, a
batch/POSIX polyglot that has to sit beside the script it runs. Nothing synced
the copies: `sync-lib.py` writes modules into `plugins/<name>/lib/` behind a
`#` banner, which is not a comment to cmd.exe and would break the polyglot's
first line. So a fix landing in four copies leaves the fifth guard behind, and
no gate notices.

Three assertions over every tracked copy, each naming the copy that fails:

  1. One digest across all copies, read from the working tree.
  2. Mode 100755 in the index.
  3. `git check-attr eol` is `lf`, and the file holds no carriage return. A
     CRLF checkout turns the shell half into a syntax error.

There is no root copy to sync from. With every copy identical, a disagreement
names the minority and leaves which side is right to the reader.

  python3 scripts/launcher-check.py [ROOT]
"""
import collections
import hashlib
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = 'run-python-hook.cmd'


def git(root, *args):
    return subprocess.run(['git', '-C', root] + list(args), check=True,
                          stdout=subprocess.PIPE, text=True).stdout


def copies(root):
    """(path, index mode) for every tracked copy under plugins/."""
    out = []
    for line in git(root, 'ls-files', '-s', '--', 'plugins/*/' + NAME,
                    'plugins/*/*/' + NAME).splitlines():
        meta, path = line.split('\t', 1)
        out.append((path, meta.split()[0]))
    return sorted(out)


def check_digests(root, found):
    groups = collections.defaultdict(list)
    for path, _ in found:
        with open(os.path.join(root, path), 'rb') as fh:
            groups[hashlib.sha256(fh.read()).hexdigest()[:12]].append(path)
    if len(groups) < 2:
        return []
    return ['%s: %s' % (digest, ', '.join(paths))
            for digest, paths in sorted(groups.items(),
                                        key=lambda kv: len(kv[1]))]


def check_modes(found):
    return ['%s is %s in the index, not 100755' % (path, mode)
            for path, mode in found if mode != '100755']


def check_line_endings(root, found):
    problems = []
    for path, _ in found:
        eol = git(root, 'check-attr', 'eol', '--', path).rsplit(':', 1)[1]
        if eol.strip() != 'lf':
            problems.append('%s has eol %s; pin it with `*.cmd text eol=lf`'
                            % (path, eol.strip()))
        with open(os.path.join(root, path), 'rb') as fh:
            if b'\r' in fh.read():
                problems.append('%s holds a carriage return' % path)
    return problems


def main(argv):
    root = argv[1] if len(argv) > 1 else ROOT
    found = copies(root)
    if not found:
        print('no tracked %s under plugins/; nothing was checked' % NAME)
        return 1
    failed = False
    for label, problems in (
            ('every copy is identical', check_digests(root, found)),
            ('every copy is executable', check_modes(found)),
            ('every copy is pinned LF', check_line_endings(root, found))):
        print('%-28s %s' % (label, 'ok (%d copies)' % len(found)
                            if not problems else 'FAILED'))
        for problem in problems:
            print('  %s' % problem)
        failed = failed or bool(problems)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
