"""Assert the plugin ships the files it invokes in a runnable state.

1.0.0 shipped scripts/run-python-hook.cmd at mode 644. The shell refused it,
the hook exited 126 having written nothing, and Claude Code reads a PreToolUse
hook that produced no decision as no objection -- so every Bash call proceeded
and the guard never fired once. That is the failure this plugin exists to
catch, in the plugin itself: a check reporting clean because it never ran.

Modes are read from the git index, not the working tree, because the index is
what gets packaged and because Windows checkouts do not carry an execute bit at
all -- a working-tree assertion would fail on Windows CI while missing the
regression everywhere else.
"""
import glob
import json
import os
import re
import shlex
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOKS_JSON = os.path.join(REPO, 'hooks', 'hooks.json')
PLUGIN_ROOT = '${CLAUDE_PLUGIN_ROOT}'
BANG_LINE = re.compile(r'^!`(.+)`\s*$', re.MULTILINE)


def index_modes():
    """Path -> octal mode, as recorded in the git index."""
    out = subprocess.check_output(['git', 'ls-files', '-s'], cwd=REPO,
                                  universal_newlines=True)
    modes = {}
    for line in out.splitlines():
        meta, path = line.split('\t', 1)
        modes[path] = int(meta.split()[0], 8)
    return modes


def wired_commands():
    """(source, command line) for every command the plugin wires up."""
    commands = []
    with open(HOOKS_JSON, encoding='utf-8') as fh:
        config = json.load(fh)
    for matcher in config['hooks'].values():
        for entry in matcher:
            for hook in entry['hooks']:
                commands.append(('hooks/hooks.json', hook['command']))
    for path in sorted(glob.glob(os.path.join(REPO, 'commands', '*.md'))):
        with open(path, encoding='utf-8') as fh:
            body = fh.read()
        rel = os.path.relpath(path, REPO).replace(os.sep, '/')
        for match in BANG_LINE.finditer(body):
            commands.append((rel, match.group(1)))
    return commands


def plugin_paths(command):
    """Repo-relative paths the command names, in argument order."""
    return [word[len(PLUGIN_ROOT):].lstrip('/')
            for word in shlex.split(command) if word.startswith(PLUGIN_ROOT)]


class TestPackaging(unittest.TestCase):

    def setUp(self):
        self.commands = wired_commands()
        self.assertTrue(self.commands, 'no wired commands found to check')

    def test_wired_paths_exist(self):
        for source, command in self.commands:
            for path in plugin_paths(command):
                with self.subTest(source=source, path=path):
                    self.assertTrue(os.path.isfile(os.path.join(REPO, path)),
                                    '%s invokes %s, which does not exist'
                                    % (source, path))

    def test_directly_invoked_files_are_executable(self):
        modes = index_modes()
        checked = 0
        for source, command in self.commands:
            paths = plugin_paths(command)
            # Only the first word is exec'd. A path in argument position is
            # read by an interpreter named ahead of it and needs no exec bit.
            if not paths or not command.lstrip('"\' ').startswith(PLUGIN_ROOT):
                continue
            with self.subTest(source=source, path=paths[0]):
                self.assertTrue(modes.get(paths[0], 0) & 0o111,
                                '%s invokes %s directly, but it is mode %s in '
                                'the git index -- the shell will refuse it'
                                % (source, paths[0],
                                   oct(modes.get(paths[0], 0))))
            checked += 1
        self.assertTrue(checked, 'no directly-invoked file was checked')


if __name__ == '__main__':
    unittest.main()
