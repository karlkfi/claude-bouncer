# ---------------------------------------------------------------------
# VENDORED COPY -- do not edit. Source: lib/bouncer_wrappers.py
# Regenerate with: python3 scripts/sync-lib.py
# ---------------------------------------------------------------------
"""Command wrappers: the options each one takes, and how many words one uses.

Every guard peels wrappers (`sudo`, `env`, `timeout`, ...) off a command so
the command they run is judged as if typed bare, and every peel has to know
which wrapper options take a separate value. Both arity errors hide the
command: a value missed is left standing as the command word (`sudo -u root
git`), and a value invented swallows it (`env -i git`). Each guard used to keep
its own table, so each new option was fixed once per guard and missed by every
guard nobody drove (Q233).

This module holds the record and the per-word arity walk, and nothing else.
The peel loop stays in each guard, because what a guard does with what it
peeled is where they differ: prod-guard merges `env` assignments into the
target's environment, foreground-guard carries state through the loop,
branch-guard defers a wrapped `allow`, and workspace-guard declines to peel
past a flag outside the grammar. Which wrappers a guard peels at all is its own
choice too, so no guard iterates this table's keys.
"""
import collections

# Each row is the wrapper's own option grammar -- short flags taking no value,
# short flags taking one (attached or the next word), short flags whose value
# is attached or absent, and long options as `none`/`req`/`opt` -- written as a
# closed list, so a reader can tell an option the wrapper lacks from one nobody
# recorded. `external` marks a separate program, so a `cd` behind it moves
# nothing; only `command` and `builtin` run the shell's own `cd`.
#
# The rows are the BSD and GNU union, read from each tool's usage: sudo
# 1.9.17p2, macOS env/nice/stdbuf/time/caffeinate, GNU coreutils 9.11,
# util-linux 2.41.5 (flock from its 2.41.2 source), OpenBSD doas.
# util-linux's short `-h` and `-V` are off its rows, so a closed-list reader
# loses the grammar there rather than judging a command that never runs. Which
# letters put a wrapper in a mode that runs nothing is each guard's own map.
Wrapper = collections.namedtuple(
    'Wrapper', 'short_none short_val short_opt long external')
_COMMON_LONG = {'help': 'none', 'version': 'none'}
WRAPPER_GRAMMAR = {
    'env': Wrapper('iv0', 'uCSPa', '', dict(_COMMON_LONG, **{
        'ignore-environment': 'none', 'null': 'none', 'unset': 'req',
        'chdir': 'req', 'split-string': 'req', 'argv0': 'req',
        'block-signal': 'opt', 'default-signal': 'opt', 'ignore-signal': 'opt',
        'list-signal-handling': 'none', 'debug': 'none'}), True),
    # `-h` is `h::` to getopt, so only an attached value is its own; sudo's
    # parse then takes the next word as the host when the token is exactly
    # `-h` and the word is neither an option nor a NAME=value, and reads any
    # other `-h` as `--help`. sudo 1.8.8 through 1.9.17 ran the command under a
    # host (CVE-2025-32462), so the row reads `-h` as taking a value, and a
    # guard that minds the help form checks the word after it (Q158).
    # sudo's getopt takes `-a`, `-c`, `-r` and `-t` in every build, and a
    # build without BSD auth, login classes or SELinux rejects them after
    # reading their value, so they are value-taking whatever the usage lists.
    'sudo': Wrapper('AbBEeHiKklnNPSsVv', 'acCDghprRtTuU', '', dict(
        _COMMON_LONG, **{
            'askpass': 'none', 'auth-type': 'req', 'background': 'none',
            'bell': 'none', 'close-from': 'req', 'chdir': 'req',
            'preserve-env': 'opt', 'edit': 'none', 'group': 'req',
            'set-home': 'none', 'host': 'req', 'login': 'none',
            'remove-timestamp': 'none', 'reset-timestamp': 'none',
            'list': 'none', 'non-interactive': 'none', 'no-update': 'none',
            'preserve-groups': 'none', 'prompt': 'req', 'chroot': 'req',
            'role': 'req', 'stdin': 'none', 'shell': 'none', 'type': 'req',
            'command-timeout': 'req', 'other-user': 'req', 'user': 'req',
            'validate': 'none', 'login-class': 'req'}), True),
    'nice': Wrapper('', 'n', '', dict(_COMMON_LONG, adjustment='req'), True),
    'nohup': Wrapper('', '', '', _COMMON_LONG, True),
    'timeout': Wrapper('fpv', 'ks', '', dict(_COMMON_LONG, **{
        'foreground': 'none', 'preserve-status': 'none', 'verbose': 'none',
        'kill-after': 'req', 'signal': 'req'}), True),
    'stdbuf': Wrapper('', 'ioe', '', dict(
        _COMMON_LONG, input='req', output='req', error='req'), True),
    'setsid': Wrapper('cfw', '', '', dict(
        _COMMON_LONG, ctty='none', fork='none', wait='none'), True),
    'ionice': Wrapper('t', 'cnpPu', '', dict(_COMMON_LONG, **{
        'class': 'req', 'classdata': 'req', 'pid': 'req', 'pgid': 'req',
        'uid': 'req', 'ignore': 'none'}), True),
    'chrt': Wrapper('abdfimoprRv', 'DPT', '', dict(_COMMON_LONG, **{
        'all-tasks': 'none', 'batch': 'none', 'deadline': 'none',
        'fifo': 'none', 'idle': 'none', 'max': 'none', 'other': 'none',
        'pid': 'none', 'rr': 'none', 'reset-on-fork': 'none',
        'verbose': 'none', 'sched-runtime': 'req', 'sched-period': 'req',
        'sched-deadline': 'req'}), True),
    # `taskset -c` changes how the mask operand reads; it takes no value.
    'taskset': Wrapper('acp', '', '', dict(_COMMON_LONG, **{
        'all-tasks': 'none', 'cpu-list': 'none', 'pid': 'none'}), True),
    # OpenBSD's doas(1), which opendoas follows; it has no long options.
    'doas': Wrapper('Lns', 'Cau', '', {}, True),
    # macOS caffeinate(8); `-h` prints usage and is off the row.
    'caffeinate': Wrapper('dimsu', 'tw', '', {}, True),
    # util-linux flock(1). Its `-c` is not an option: it is read as a word
    # after the lock file, so it is off the row.
    'flock': Wrapper('sexnoFu', 'wE', '', dict(_COMMON_LONG, **{
        'shared': 'none', 'exclusive': 'none', 'unlock': 'none',
        'nonblocking': 'none', 'nb': 'none', 'timeout': 'req', 'wait': 'req',
        'conflict-exit-code': 'req', 'close': 'none', 'no-fork': 'none',
        'verbose': 'none', 'fcntl': 'none'}), True),
    # expect's `unbuffer`, from its man page rather than a binary.
    'unbuffer': Wrapper('p', '', '', {}, True),
    # GNU and BSD `time` together; `-o` names a file the wrapper itself writes.
    'time': Wrapper('aphlqvV', 'fo', '', dict(_COMMON_LONG, **{
        'append': 'none', 'portability': 'none', 'quiet': 'none',
        'verbose': 'none', 'format': 'req', 'output': 'req'}), True),
    # GNU and BSD `xargs` together; `-a` names a file it reads its input from.
    'xargs': Wrapper('0prtxo', 'aEIdLnPsJRS', 'eil', dict(_COMMON_LONG, **{
        'null': 'none', 'arg-file': 'req', 'delimiter': 'req', 'eof': 'opt',
        'replace': 'opt', 'max-lines': 'opt', 'max-args': 'req',
        'max-procs': 'req', 'max-chars': 'req', 'interactive': 'none',
        'verbose': 'none', 'exit': 'none', 'no-run-if-empty': 'none',
        'open-tty': 'none', 'process-slot-var': 'req',
        'show-limits': 'none'}), True),
    'command': Wrapper('pvV', '', '', {}, False),
    'builtin': Wrapper('', '', '', {}, False),
    'exec': Wrapper('cl', 'a', '', {}, True),
}


def value_options(w):
    """The options of grammar row ``w`` that take a separate value, spelled
    as typed: `-u` and `--user`. An optional value is attached or absent, so
    those are not among them."""
    return frozenset(['-' + c for c in w.short_val]
                     + ['--' + k for k, a in w.long.items() if a == 'req'])


# The value-only view, for a guard that skips any other option as a bare flag.
WRAPPER_VALUE_OPTS = {name: value_options(w)
                      for name, w in WRAPPER_GRAMMAR.items()}


def wrapper_option(argv, value_opts):
    """(words consumed, value-taking option or None, its value) for the option
    word at argv[0]. A short word is walked a character at a time, so a bundle
    (`-iu NAME`) takes its value and the walk stops at the first value-taking
    letter, whose value is the rest of the word (`-uroot`). A long word matches
    by unique prefix, as getopt_long does: `--ch DIR` is `--chdir DIR`. A
    prefix of two value-taking spellings is getopt's error, so nothing runs
    behind it and the word is taken alone, as is `--`, which ends the options."""
    tok = argv[0]
    nxt = argv[1] if len(argv) > 1 else None
    if tok == '--':
        return 1, None, None
    if tok.startswith('--'):
        name, eq, value = tok.partition('=')
        matches = [o for o in value_opts if o.startswith('--') and o.startswith(name)]
        if name in value_opts:
            matches = [name]
        if len(matches) != 1:
            return 1, None, None
        return (1, matches[0], value) if eq else (2, matches[0], nxt)
    for pos, char in enumerate(tok[1:], start=2):
        if '-' + char in value_opts:
            if pos == len(tok):
                return 2, '-' + char, nxt
            return 1, '-' + char, tok[pos:]
    return 1, None, None
