#!/usr/bin/env python3
"""PreToolUse hook: prompt (ask) when a guarded command targets a file
outside the workspace; allow when it only touches workspace files or pipes.

Reads the hook JSON on stdin, emits a PreToolUse decision on stdout.
"""
import sys, os, json, re, shlex, shutil, fnmatch, collections, tempfile

# POSIX command-prefix assignment: NAME starts with letter/underscore,
# followed by letters/digits/underscores, then `=`. Anything after the `=`
# (including empty) is the value. Bash treats one or more of these tokens
# at the start of a simple command as inline env exports for that command;
# they do not change the command name lookup.
ASSIGNMENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')

# --- Literal variable propagation (issue 58) --------------------------------
# `SP=/path; tail $SP/x` binds SP to a literal earlier in the same command
# string; bash expands `$SP` deterministically, so the hook can too and run
# the resolved path through the normal workspace check instead of flagging it
# as runtime-expanded. Everything below only ever narrows what is propagated:
# any uncertainty drops (poisons) the variable, which restores today's `ask`.

# A plain `$NAME` / `${NAME}` use. Parameter-expansion operators (`${f:-x}`,
# `${f%.*}`, ...) deliberately don't match — the `$` stays in the token and
# keeps the runtime-expanded `ask`.
VAR_USE_RE = re.compile(r'\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))')

IDENT_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')

# Token that mutates a shell variable outside the plain-assignment form:
# `f=…` (command prefix), `f+=…` (append), `f[0]=…` (array element),
# `f++`/`f--` (arithmetic).
ASSIGNISH_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)(\+?=|\[|\+\+|--)')

# Chars that make an assignment RHS unsafe to treat as a literal, checked
# AFTER shlex quote removal: expansions (`$`, backticks), glob metachars
# (`*?[` — an unquoted use of the variable would glob), and word-splitting
# chars (whitespace for the default IFS; `:` so a PATH-style value can't be
# split by an IFS the hook didn't see into pieces the single-token check misses).
IMPURE_VALUE_CHARS = frozenset(' \t\n$`*?[:')

# The same test minus the glob metachars, for `for VAR in <list>` items only: a
# pattern there is its own proxy for the paths it expands to (see
# `literal_for_item`).
IMPURE_ITEM_CHARS = IMPURE_VALUE_CHARS - frozenset('*?[')

# The one `:` that isn't a PATH-style separator: a Windows drive prefix (Q48).
# Without an exemption every Windows absolute path is impure, so propagation is
# dead on the platform — including the `~/` form, whose expansion carries the
# drive letter. The separator must follow the colon because bash tilde-expands
# after a `:` in an assignment RHS (`f=C:~/x` -> `C:/Users/…`), which the
# leading-`~` rule in literal_assignment_value doesn't model.
DRIVE_PREFIX_RE = re.compile(r'^[A-Za-z]:[\\/]')

# ...and the exemption applies only where os.path resolves a drive. On POSIX
# `C:/x` is a relative directory literally named `C:`, so exempting it there
# would give up the PATH-style protection for nothing. splitdrive is the
# discriminator rather than os.name for the reason claude_tmp_root() uses
# hasattr: honouring the drive is the actual condition — it is what realpath and
# isabs downstream will do with the value.
DRIVE_PATHS = bool(os.path.splitdrive('C:/x')[0])

# Names bash treats specially — assigning them does not make `$NAME` expand
# to the assigned literal (dynamic values, readonly, or reset by the shell).
NEVER_PROPAGATE = frozenset({
    '_', 'IFS', 'PWD', 'OLDPWD', 'RANDOM', 'SRANDOM', 'SECONDS', 'LINENO',
    'BASHPID', 'PPID', 'UID', 'EUID', 'GROUPS', 'EPOCHSECONDS',
    'EPOCHREALTIME', 'BASH_SUBSHELL', 'BASH_COMMAND', 'PIPESTATUS',
    'FUNCNAME', 'DIRSTACK',
})

# Commands that can assign to ANY variable invisibly -> the whole map dies.
POISON_ALL_CMDS = frozenset({'eval', 'source', '.'})

# Builtins/keywords that assign to variables named by their arguments
# (`read f`, `for f in …`, `declare f=…`, `printf -v f …`, `unset f`, ...).
ARG_ASSIGNER_CMDS = frozenset({
    'read', 'readarray', 'mapfile', 'getopts', 'declare', 'typeset',
    'local', 'readonly', 'export', 'unset', 'let', 'printf', 'for', 'select',
})

# Reserved words that may prefix the real command in a group (`while read f`,
# `if f=…`) — skipped before dispatching the poison rules above.
SH_KEYWORDS = frozenset({
    'while', 'until', 'if', 'then', 'elif', 'else', 'do', 'done', 'fi',
    'case', 'esac', 'in', 'time', 'function', '!', '{', '}', '[[', ']]',
})

# Command separators and redirect operators (after shlex punctuation grouping).
SEPARATORS = {'|', '||', '&&', '&', ';', '\n', '(', ')'}
REDIR = {'>', '>>', '<', '<<', '<<<', '>|', '&>', '&>>'}
# fd-duplication operators: `2>&1`, `1>&2`, `2>&-` (close), `0<&3`. The token
# after one is a duplication/close target (a bare fd number or `-`), NOT a file
# — unless it's a filename, in which case bash's `>&file` redirects to it (Q20).
DUP = {'>&', '<&'}

# Every char shlex treats as punctuation (see `punctuation_chars` in main).
# A token built only from these is an operator run; anything else is a word
# (so a quoted filename containing one of these survives normalization).
PUNCT_CHARS = frozenset(';()<>|&\n')

# Longest-match vocabulary for splitting glued operator runs (Q27). Built from
# the SEPARATORS/REDIR/DUP sets — sorted longest-first — so it can never drift
# from what the group-splitting loop understands. Every single punctuation char
# is one of these operators, so any pure-punctuation run decomposes completely.
_OPERATORS = tuple(sorted(SEPARATORS | REDIR | DUP, key=len, reverse=True))

# Characters that may precede an unquoted `#` for it to start a comment: bash
# only comments at the start of a word (after whitespace, a newline, or an
# operator). `$#`, `${#x}`, and mid-word `file#1` are not comments.
COMMENT_PRECEDERS = frozenset(' \t\n;|&()<>')

# A `$` introduces a bash expansion only when followed by a name char, digit,
# `{`, `(`, or a special-parameter char (`# ? $ ! @ * -`). Any other `$` —
# trailing, or before e.g. `.`/`/` — is literal, so a token containing only
# such dollars is a plain filename, not a runtime expansion. Command
# substitution split across tokens (`$` + `(`) is re-glued by
# glue_dollar_paren() before this is consulted.
EXPANSION_RE = re.compile(r'\$[A-Za-z0-9_{(#?$!@*-]')

# SPEC commands that write or mutate files. The ALLOWED_READ_PREFIXES exemption
# (see allowed_read_prefixes()) does NOT apply to these commands, even if the
# target path is under an allowed prefix — write access to Claude-owned dirs
# is not exempt from the workspace check. Read-classified commands can also be
# flipped into write mode by a flag — see WRITE_MODE_FLAGS.
WRITE_COMMANDS = frozenset({'cp', 'mv', 'tee', 'rm'})

# Flags that flip an otherwise read-only SPEC command into file-writing mode
# (Q36): sed/yq in-place editing, gawk's `-i inplace` include, sort's `-o OUT`.
# When one is present the whole invocation is treated like a WRITE_COMMANDS
# member — the read-prefix exemption applies to none of its files. `short`
# letters match anywhere in a short-option cluster (`-ni`, `-i.bak`); `long`
# options match bare or with an `=value`. Matching is deliberately loose (gawk's
# `-i` counts even when the included library isn't `inplace`): a false positive
# only ever downgrades a silent allow to `ask` for files under an allowed
# prefix — never the reverse — and leaves in-workspace files untouched.
WRITE_MODE_FLAGS = {
    'sed':  {'short': 'i', 'long': ('--in-place',)},
    'awk':  {'short': 'i', 'long': ('--include',)},
    'yq':   {'short': 'i', 'long': ('--inplace',)},
    'sort': {'short': 'o', 'long': ('--output',)},
}


def has_write_mode_flag(cmd_name, tokens):
    """True when ``tokens`` (argv including the command word) carries a
    write-mode flag for ``cmd_name`` per WRITE_MODE_FLAGS. Scanning stops at
    the end-of-options ``--`` marker, after which everything is positional."""
    spec = WRITE_MODE_FLAGS.get(cmd_name)
    if not spec:
        return False
    for t in tokens[1:]:
        if t == '--':
            return False
        if len(t) < 2 or t[0] != '-':
            continue
        if t.startswith('--'):
            if t.split('=', 1)[0] in spec['long']:
                return True
        elif spec['short'] in t[1:]:
            return True
    return False

# Well-known device / FD paths that are safe to read or write regardless of
# workspace boundary. Matched against the raw token before realpath, because
# `/dev/stdin` resolves to `/dev/fd/0` on darwin and `/proc/self/fd/0` on Linux.
ALLOWED_DEVICES = frozenset({
    '/dev/null', '/dev/zero',
    '/dev/stdin', '/dev/stdout', '/dev/stderr',
    '/dev/tty', '/dev/random', '/dev/urandom',
})


def is_allowed_device(path):
    """True for well-known device paths and `/dev/fd/N` FD references."""
    if path in ALLOWED_DEVICES:
        return True
    if path.startswith('/dev/fd/'):
        rest = path[len('/dev/fd/'):]
        return rest.isdigit()
    return False


# --- Git Bash (MSYS) path forms ----------------------------------------------
# On native Windows the Bash tool is Git Bash, which resolves a leading-slash
# path through the MSYS mount table. The hook is a native Python process, where
# the same string is drive-relative — so the two disagree, and the guard named
# `D:\etc\passwd` in a prompt for a command that reads `C:\Program Files\Git\
# etc\passwd`, while MSYS-form configuration entries matched nothing at all
# (Q52). These rules are the table Git for Windows ships, read off a
# windows-latest runner (Git 2.55, MSYSTEM=MINGW64):
#
#     C:/Program Files/Git          on /
#     C:/Program Files/Git/usr/bin  on /bin
#     <%TMP%>                       on /tmp   (usertemp)
#     C: on /c, D: on /d, ...                 (cygdrive prefix `/`)
#
# The drive rule is generic rather than per-existing-drive: `/x/y` becomes
# `X:\y` on a machine with no X: drive, which is what Git Bash does too.
#
# A user-edited /etc/fstab can add mounts the hook can't see, and MSYS's virtual
# paths (`/proc`) have no native form. Both fall through to the `/` rule, which
# leaves the failure direction where it already was — an outside-workspace
# prompt naming a path that isn't quite right, never a silent allow.
MSYS_DRIVE_RE = re.compile(r'^/([A-Za-z])(?:/(.*))?$')

# Relative path of the MSYS bash inside its own root, used to confirm a
# candidate root actually is one. `bin/bash.exe` is a wrapper Git for Windows
# adds; `usr/bin/bash.exe` is the MSYS bash itself and is what marks the root.
_MSYS_ROOT_MARKER = os.path.join('usr', 'bin', 'bash.exe')

_msys_root_cached = ()          # () = not yet computed (None is a real answer)


def msys_root():
    """Native path of Git Bash's ``/``, or None when no Git Bash is found.

    Located from the bash Claude Code runs: ``CLAUDE_CODE_GIT_BASH_PATH`` when
    set (it names that bash exactly), else ``bash``/``git`` on PATH. Each
    candidate's ancestors are walked until one holds ``usr/bin/bash.exe``, which
    both finds the root from any depth (``bin/bash.exe``, ``usr/bin/bash.exe``,
    ``cmd/git.exe`` all land on it) and rejects a false positive — on a machine
    without Git for Windows, ``bash`` on PATH is the WSL launcher in
    ``C:\\Windows\\System32``, whose ancestors carry no such marker. Returning
    None there is the point: the guard keeps its old drive-relative reading
    rather than inventing a root.

    Cached for the life of the process — the PATH scan is a few dozen stats,
    and it only runs when a command actually names a non-drive MSYS path.
    """
    global _msys_root_cached
    if _msys_root_cached != ():
        return _msys_root_cached
    _msys_root_cached = None
    cands = [os.environ.get('CLAUDE_CODE_GIT_BASH_PATH'),
             shutil.which('bash'), shutil.which('git')]
    for exe in cands:
        if not exe:
            continue
        d = os.path.dirname(os.path.abspath(exe))
        while True:
            try:
                if os.path.isfile(os.path.join(d, _MSYS_ROOT_MARKER)):
                    _msys_root_cached = d
                    return d
            except OSError:
                break
            parent = os.path.dirname(d)
            if parent == d:
                break
            d = parent
    return None


def msys_tmp():
    """Native path of Git Bash's ``/tmp`` — the ``usertemp`` mount, ``%TMP%``.

    Realpath'd, because ``%TMP%`` commonly arrives in 8.3 short form
    (``C:\\Users\\RUNNER~1\\AppData\\Local\\Temp``) while the file argument it
    has to compare against is realpath'd to the long form. The exact-prefix
    allowlist branch would survive that (it realpaths too), but the glob branch
    only normalizes — so a short name there exempted nothing at all.
    """
    return os.path.realpath(tempfile.gettempdir())


def msys_to_native(raw):
    """Rewrite a leading-slash Git Bash path into the native path it names.

    Returns ``raw`` unchanged on POSIX (where the string already means what it
    says), for anything that isn't a leading-slash path, and for a non-drive
    path when :func:`msys_root` finds no Git Bash.

    The result keeps forward slashes and is deliberately left un-normalized:
    ntpath reads either separator, and every caller hands the value to
    ``realpath``, which resolves ``..`` through symlinks rather than lexically.
    """
    if not DRIVE_PATHS or not raw.startswith('/'):
        return raw
    m = MSYS_DRIVE_RE.match(raw)
    if m:
        return m.group(1).upper() + ':/' + (m.group(2) or '')
    if raw == '/tmp' or raw.startswith('/tmp/'):
        return msys_tmp().rstrip('/\\') + raw[len('/tmp'):]
    root = msys_root()
    if root is None:
        return raw
    root = root.rstrip('/\\')
    if raw == '/bin' or raw.startswith('/bin/'):
        return root + '/usr/bin' + raw[len('/bin'):]
    return root + raw


def resolved_home():
    """Absolute path of the current user's home directory, or None.

    Resolved with ``expanduser`` rather than ``$HOME`` because the hook is
    launched through ``run-python-hook.cmd``, so on Windows it inherits a
    cmd.exe environment where ``HOME`` is unset (Q40, Q43). ``expanduser``
    reads ``USERPROFILE`` (then ``HOMEDRIVE``/``HOMEPATH``) there, matching the
    ``os.homedir()`` that Claude Code uses to pick where to write, and on POSIX
    falls back to the pwd database when ``HOME`` is unset. It returns ``~``
    unchanged when nothing resolves, hence the isabs check.
    """
    home = os.path.expanduser('~')
    return home if os.path.isabs(home) else None


def claude_projects_dir():
    """Realpath of Claude Code's per-user project-data dir, ``~/.claude/projects/``.

    Claude Code writes session and sub-agent data (workflow journals, task
    output indices, etc.) under this directory. Reading these files back is
    not the boundary this hook guards: the data is written by the harness
    itself, not by external inputs. Returns None if the home directory or the
    path cannot be resolved.

    Without a home directory the prefix would vanish and every read of Claude's
    own session data would prompt (Q40), so it resolves via
    :func:`resolved_home` rather than ``$HOME``.
    """
    home = resolved_home()
    if home is None:
        return None
    try:
        return os.path.realpath(os.path.join(home, '.claude', 'projects'))
    except OSError:
        return None


def allowed_read_prefixes(base):
    """Resolved list of absolute path prefixes exempt from the workspace check
    for **read-only** guarded commands (see WRITE_COMMANDS for exclusions).

    Default: Claude Code's per-user project-data dir (~/.claude/projects/).
    Additive extension via WORKSPACE_GUARD_READ_ALLOW_PREFIXES (split on
    ``os.pathsep`` or a comma). Each entry goes through :func:`resolve_from`
    against ``base`` (the tool's cwd) so platform symlinks (e.g. /tmp ->
    /private/tmp on macOS) resolve correctly and a drive-relative Windows entry
    lands on the drive its file arguments do. Entries that cannot be resolved
    are skipped (fail-open on config, fail-safe on the boundary: a bad entry
    just loses its exemption).
    """
    defaults = []
    cpd = claude_projects_dir()
    if cpd:
        defaults.append(cpd)
    extras = _split_pathlist(os.environ.get('WORKSPACE_GUARD_READ_ALLOW_PREFIXES', ''))
    out = []
    for p in defaults + extras:
        rp = resolve_from(base, p)
        if rp is not None:
            out.append(rp)
    return out


def claude_tmp_root():
    """Realpath of Claude Code's per-user temp root.

    Claude Code stores each session's background-task output under
    ``<root>/<encoded-project>/<session-uuid>/tasks/<id>.output``. This layout
    is an undocumented internal convention — there is no hook field that names
    it — so we infer the root. If Claude Code ever relocates the dir, paths
    simply stop matching the allow below and revert to ``ask`` (fail-safe), so
    inferring it never weakens the boundary.

    On POSIX the root is ``/tmp/claude-<uid>`` (mode 0700, per-UID). Windows
    has no ``os.getuid()`` and no per-UID suffix: the root is ``claude`` inside
    the per-user temp dir (``%LOCALAPPDATA%\\Temp\\claude``, verified against a
    live install). ``hasattr`` is the discriminator rather than ``os.name``
    because the missing call is the actual condition. The Windows root is
    partly environment-derived (``tempfile.gettempdir()`` honours ``TMP`` /
    ``TEMP``), which does not widen the boundary: a tampered value makes paths
    fail to match and fall back to ``ask``, and the allow below additionally
    requires the *running* session's own uuid as a path segment.
    """
    if hasattr(os, 'getuid'):
        return os.path.realpath('/tmp/claude-%d' % os.getuid())
    return os.path.realpath(os.path.join(tempfile.gettempdir(), 'claude'))


def is_session_tmp_path(rp, session_id, tmp_root):
    """True when resolved path ``rp`` is THIS session's own Claude-managed
    scratch — i.e. inside ``tmp_root`` AND carrying ``session_id`` as a path
    segment (the ``<session-uuid>`` directory).

    Reading back the agent's own background-task output is not the boundary
    this hook guards, so such paths are allowed silently. The scope is
    deliberately per-session, NOT the whole ``/tmp/claude-<uid>`` root: another
    session's or project's task output can contain secrets, and allowing a
    different session to read it would be a cross-context leak. Matched against
    the resolved realpath (not the raw token), so a symlink planted in the temp
    dir that escapes the root resolves outside ``tmp_root`` and is still
    flagged. An empty ``session_id`` (hook field absent) disables the allow. (Q21)
    """
    if not session_id:
        return False
    if rp != tmp_root and not rp.startswith(tmp_root + os.sep):
        return False
    return session_id in rp.split(os.sep)


def claude_session_project_dir(session_id, tmp_root):
    """Resolved path of the current session's Claude-managed *project* scratch
    dir — the ``<tmp_root>/<project-slug>`` that contains this session.

    Claude Code lays out background-task scratch as
    ``<tmp_root>/<project-slug>/<session-uuid>/tasks/<id>.output``. Sibling
    sessions of the SAME project (a dispatcher plus its parallel workers) share
    the ``<project-slug>`` parent, so a dispatcher tailing a worker's output
    reads a path under this dir. We locate it by scanning ``tmp_root`` for the
    single ``<project-slug>`` child that already holds a ``<session_id>``
    subdirectory — ground truth from the filesystem, so it does NOT depend on
    Claude's undocumented slug-encoding (which differs between a worktree cwd
    and the main checkout, and could change without notice). This is a
    directory ``listdir``/``isdir`` scan only — no file contents are read.

    Returns None when ``session_id`` is empty, ``tmp_root`` can't be listed, or
    no project dir holds this session (the sibling-read exemption then simply
    doesn't apply and such paths keep prompting — the secure-by-default
    direction). (#61)
    """
    if not session_id:
        return None
    try:
        slugs = os.listdir(tmp_root)
    except OSError:
        return None
    for slug in slugs:
        proj_dir = os.path.join(tmp_root, slug)
        try:
            if os.path.isdir(os.path.join(proj_dir, session_id)):
                return os.path.realpath(proj_dir)
        except OSError:
            continue
    return None


def path_at_or_under(rp, root):
    """True when ``rp`` is ``root`` itself or lives below it. Uses the os.sep
    boundary so `/tmpfoo` is NOT considered under `/tmp`."""
    return rp == root or rp.startswith(root + os.sep)


# Host-wide temp roots. A guarded file argument that resolves at or under one of
# these — AFTER symlink and $TMPDIR resolution — is "host temp": shared across
# every session/process and every worktree, colliding between concurrent runs
# and living outside the project root. Such a path gets a stronger, constructive
# `deny` (steering to a repo-local gitignored scratch dir) instead of the usual
# outside-workspace `ask`. These are the POSIX names; the platform's own temp
# dir is added by host_temp_roots(), which is what covers Windows. The list is
# extensible; see host_temp_roots().
HOST_TEMP_DEFAULT_ROOTS = ('/tmp', '/var/tmp')


def _split_pathlist(raw):
    """Split a `:`/`,`-separated env list into non-empty, stripped entries."""
    if not raw:
        return []
    parts = []
    for chunk in raw.replace(os.pathsep, ',').split(','):
        chunk = chunk.strip()
        if chunk:
            parts.append(chunk)
    return parts


def resolve_from(base, raw):
    """Realpath ``raw``, resolving a non-absolute path against ``base``.

    Every configured path — a host-temp root, an allowlist entry, a read-allow
    prefix — is ultimately compared against a file argument, and file arguments
    resolve against the tool's cwd. Both sides must therefore be interpreted
    under the same rule.

    On POSIX ``/tmp`` is absolute, so ``base`` never changes the answer and this
    is exactly the old ``realpath``. On Windows it is not: a leading slash names
    a directory on *whichever drive is current*, and ``os.path.isabs`` reports
    False for it. Resolving such a root from the hook process's own cwd put it
    on a different drive than the file arguments, so the comparison silently
    never matched — a host-temp ``deny`` degraded to a plain ``ask``, and a
    configured allowlist entry stopped matching anything at all.

    A leading-slash entry is first read as Git Bash reads it (see
    :func:`msys_to_native`), so `/c/Users/me/shared` names the directory the
    user meant rather than a `c` folder on whichever drive is current.

    Returns None when the path can't be resolved (callers skip it: fail-open on
    config, fail-safe on the boundary)."""
    return _resolve_literal(base, msys_to_native(raw))


def _resolve_literal(base, raw):
    """:func:`resolve_from` without the MSYS reading — the raw string taken at
    face value. Only ``host_temp_roots`` wants this, to keep both readings."""
    if raw and not os.path.isabs(raw):
        raw = os.path.join(base, raw)
    try:
        return os.path.realpath(raw)
    except OSError:
        return None


def host_temp_roots(base):
    """Resolved set of host-temp roots: the defaults, any extra roots from
    ``WORKSPACE_GUARD_TMP_ROOTS`` (additive — never replaces the defaults, so the
    boundary can't be weakened by clearing it), ``$TMPDIR`` if set, and on
    Windows the platform temp dir the POSIX names don't cover.

    Each root goes through :func:`resolve_from` against ``base`` (the tool's
    cwd), so a path under macOS's ``/tmp -> /private/tmp`` symlink or a
    ``$TMPDIR`` under ``/var/folders/...`` is matched after the file argument is
    itself resolved, and a drive-relative Windows root lands on the same drive
    the file arguments do. A root that can't be resolved is skipped
    (fail-open).

    A leading-slash root is added under BOTH readings: what Git Bash makes of it
    (``/tmp`` -> ``%TMP%``, the reading that matches a command's own arguments)
    and the drive-relative one it had before Q52 (``<drive>\\tmp``). An extra
    root only ever widens a ``deny`` tier, so keeping the second costs nothing
    and can't be a regression for whoever relied on it."""
    raw = list(HOST_TEMP_DEFAULT_ROOTS)
    raw += _split_pathlist(os.environ.get('WORKSPACE_GUARD_TMP_ROOTS', ''))
    tmpdir = os.environ.get('TMPDIR')
    if tmpdir:
        raw.append(tmpdir)
    # Windows has no $TMPDIR, and its host-wide temp dir is %TMP%/%TEMP% —
    # which the POSIX names above miss entirely, so a scratch write to the one
    # directory this tier exists to catch got a plain `ask` instead of the
    # steered `deny`. tempfile.gettempdir() is where that lands. Gated on the
    # same discriminator as claude_tmp_root() (which already pays for the call
    # there): the missing $TMPDIR is the actual condition, and on POSIX
    # gettempdir() would only re-derive $TMPDIR-or-/tmp while adding its probe
    # to every Bash call.
    if not hasattr(os, 'getuid'):
        raw.append(tempfile.gettempdir())
    out = set()
    for r in raw:
        if not r:
            continue
        for cand in (msys_to_native(r), r):
            rp = _resolve_literal(base, cand)
            if rp is not None:
                out.add(rp)
    return out


def is_host_temp(rp, roots):
    """True when resolved path ``rp`` is at or under any host-temp root."""
    return any(path_at_or_under(rp, r) for r in roots)


def host_temp_action():
    """`deny` (default) or `ask` for host-temp paths, from
    ``WORKSPACE_GUARD_TMP_ACTION``. Any unrecognised value falls back to the
    secure default (`deny`)."""
    v = (os.environ.get('WORKSPACE_GUARD_TMP_ACTION') or 'deny').strip().lower()
    return v if v in ('deny', 'ask') else 'deny'


def host_temp_allowlist():
    """Opt-in escape hatch: resolved paths matching one of these patterns are
    NOT treated as host temp (they fall through to normal handling, i.e. allowed
    when they'd otherwise only be flagged for being host temp). Empty by default
    — this is a documented trade-off for the rare tool that genuinely needs
    ``/tmp``. From ``WORKSPACE_GUARD_TMP_ALLOW``."""
    return _split_pathlist(os.environ.get('WORKSPACE_GUARD_TMP_ALLOW', ''))


def matches_allowlist(rp, patterns, base):
    """True when resolved path ``rp`` matches an allowlist entry. An entry with
    glob metacharacters is matched with ``fnmatch``; otherwise it's an exact or
    directory-prefix match, resolved first so a configured ``/tmp/ok`` matches
    the realpath ``/private/tmp/ok`` on macOS.

    Both forms are normalized against ``base`` (the tool's cwd) for the reason
    in :func:`resolve_from` — otherwise a Windows entry written ``/tmp/ok``
    resolves on a different drive than the path it is meant to exempt and the
    knob silently does nothing. The glob form is normalized rather than
    realpath'd so its metacharacters survive, and takes its own
    :func:`msys_to_native` pass for the same reason ``resolve_from`` does.

    Matching is tried against ``rp`` and, on macOS, against ``rp`` with the
    leading ``/private`` stripped — so a user-written glob like ``/tmp/build-*``
    still matches the resolved ``/private/tmp/build-42`` without forcing users to
    know about the platform symlink."""
    cands = [rp]
    if rp.startswith('/private/'):
        cands.append(rp[len('/private'):])
    for p in patterns:
        if not p:
            continue
        if any(c in p for c in '*?['):
            pat = msys_to_native(p)
            pat = pat if os.path.isabs(pat) else os.path.join(base, pat)
            if any(fnmatch.fnmatch(c, os.path.normpath(pat)) for c in cands):
                return True
            continue
        rp_pat = resolve_from(base, p) or p
        stem = rp_pat.rstrip(os.sep)
        if any(c == rp_pat or path_at_or_under(c, stem) for c in cands):
            return True
    return False


def scratch_dir_name():
    """Repo-local scratch dir named in the deny message (default ``tmp/``), from
    ``WORKSPACE_GUARD_SCRATCH_DIR``."""
    return (os.environ.get('WORKSPACE_GUARD_SCRATCH_DIR') or 'tmp/').strip() or 'tmp/'


def session_scratchpad(session_id, session_proj_dir):
    """Resolved path of the ``scratchpad/`` dir Claude Code hands THIS session,
    or None when it can't be located.

    Laid out as ``<tmp_root>/<project-slug>/<session-uuid>/scratchpad``, so it
    sits inside the tree :func:`is_session_tmp_path` exempts for reads *and*
    writes — the second legitimate destination for a temp file, alongside the
    repo-local scratch dir. Gated on an ``os.path.isdir`` stat (no file contents
    read) for the same reason :func:`build_scratch_hint` gates the repo-local
    name: steering an agent at a directory that isn't there just trades a deny
    for a "no such file or directory". (Q56)
    """
    if not (session_id and session_proj_dir):
        return None
    path = os.path.join(session_proj_dir, session_id, 'scratchpad')
    try:
        return path if os.path.isdir(path) else None
    except OSError:
        return None


def build_scratch_hint(proj, scratch, scratchpad=None):
    """One-line guidance steering off host temp toward a repo-local scratch dir.

    Names the dir concretely when it already exists under the project root
    (an ``os.path.isdir`` stat — no file contents are read), otherwise tells the
    user to create and gitignore it. When ``scratchpad`` is given (see
    :func:`session_scratchpad`) it is named as the other allowed destination,
    so an agent denied on ``/tmp`` doesn't infer the harness-managed scratchpad
    is off-limits too and litter the worktree instead. Closes with the two
    config knobs."""
    name = scratch.rstrip('/') or 'tmp'
    rel = './' + name + '/'
    present = False
    try:
        present = os.path.isdir(os.path.join(proj, name))
    except OSError:
        present = False
    if present:
        where = "Use the repo-local scratch dir `%s` instead (keep it gitignored)." % rel
    else:
        where = ("Create a gitignored `%s` at the repo root (add `/%s/` to "
                 "`.gitignore`) and use `%s` instead." % (name, name, rel))
    if scratchpad:
        where += (" This session's own scratchpad `%s` is allowed read-write "
                  "too — prefer it for a throwaway that shouldn't outlive the "
                  "session." % scratchpad)
    return ("Host-wide temp is shared across every session and worktree and "
            "lives outside the project root, so concurrent runs collide and the "
            "write escapes the workspace. " + where
            + " To soften this to a prompt set WORKSPACE_GUARD_TMP_ACTION=ask; "
            "to exempt a specific path set WORKSPACE_GUARD_TMP_ALLOW.")


# --- Sibling-checkout (git worktree) detection --------------------------------
# When a session runs inside a git worktree, a WRITE that lands in a *sibling
# checkout of the same repo* — the primary checkout or another worktree —
# silently targets the wrong branch (often `main`, or another session's in-flight
# branch). We detect this by resolving the offending path's enclosing checkout
# and comparing its git common-dir to the session's: same common-dir + a
# different checkout root == a sibling checkout. This is the `--git-common-dir`
# equivalence, done per-path so we never enumerate every worktree (a repo can
# have dozens). A path in an *unrelated* git repo has a different common-dir and
# is never treated as a sibling.
#
# Only tiny git metadata files are read (`.git`, `commondir`, `HEAD`) — never
# version-controlled file contents, no network. Any read/parse failure yields
# None (fail-safe: the path keeps its normal outside `ask`; the boundary is
# never weakened).

def _read_git_meta(path, limit=8192):
    """Read a small git metadata file, or None on any error. Size-capped."""
    try:
        with open(path, 'r') as f:
            return f.read(limit)
    except OSError:
        return None


def _resolve_checkout(start_dir):
    """Walk up from ``start_dir`` to the nearest enclosing git checkout.

    Returns ``{'root', 'admin', 'common'}`` (all realpaths) or None:
      * ``.git`` is a **directory** -> main checkout; admin == common == that dir.
      * ``.git`` is a **file** (``gitdir: <admin>``) -> linked worktree; the
        common-dir comes from ``<admin>/commondir`` (falling back to the
        ``<common>/worktrees/<name>`` layout).
      * no enclosing ``.git`` -> None (not a git repo).
    """
    d = start_dir
    seen = set()
    while d and d not in seen:
        seen.add(d)
        gitpath = os.path.join(d, '.git')
        try:
            is_dir = os.path.isdir(gitpath)
            is_file = os.path.isfile(gitpath)
        except OSError:
            is_dir = is_file = False
        if is_dir:
            common = os.path.realpath(gitpath)
            return {'root': os.path.realpath(d), 'admin': common, 'common': common}
        if is_file:
            content = _read_git_meta(gitpath)
            m = re.match(r'\s*gitdir:\s*(.+?)\s*$', content or '')
            if not m:
                return None                       # malformed -> fail-safe
            admin = m.group(1)
            if not os.path.isabs(admin):
                admin = os.path.join(d, admin)
            admin = os.path.realpath(admin)
            cc = _read_git_meta(os.path.join(admin, 'commondir'))
            if cc and cc.strip():
                cc = cc.strip()
                common = cc if os.path.isabs(cc) else os.path.join(admin, cc)
            else:
                common = os.path.join(admin, os.pardir, os.pardir)
            return {'root': os.path.realpath(d), 'admin': admin,
                    'common': os.path.realpath(common)}
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def resolve_session_worktree(proj):
    """Resolve the session's checkout from ``proj``.

    Returns the ``_resolve_checkout`` dict augmented with ``in_worktree`` (True
    when the session's ``.git`` is a linked-worktree admin dir, i.e. admin !=
    common), or None when ``proj`` isn't in a git repo. Sibling detection is a
    no-op unless ``in_worktree`` — this is the "no-op when the session isn't in
    a worktree" rule.
    """
    co = _resolve_checkout(proj)
    if co is None:
        return None
    co['in_worktree'] = (co['admin'] != co['common'])
    return co


def _branch_label(admin):
    """Human-readable branch of the checkout whose admin dir is ``admin``.

    ``ref: refs/heads/X`` -> ``X``; a detached SHA -> ``(detached <sha12>)``;
    unreadable -> None."""
    head = _read_git_meta(os.path.join(admin, 'HEAD'))
    if not head:
        return None
    head = head.strip()
    if head.startswith('ref:'):
        ref = head[len('ref:'):].strip()
        if ref.startswith('refs/heads/'):
            return ref[len('refs/heads/'):]
        return ref
    return '(detached %s)' % head[:12] if head else None


def sibling_checkout_for(rp, session):
    """If resolved path ``rp`` lies inside another checkout of the SAME repo as
    the session worktree, return ``(root, branch)``; else None.

    ``session`` is the ``resolve_session_worktree`` dict. Self-gates: returns
    None unless the session is itself a linked worktree, so callers can invoke
    it unconditionally.
    """
    if not session or not session.get('in_worktree'):
        return None
    co = _resolve_checkout(os.path.dirname(rp))
    if co is None:
        return None
    if co['common'] != session['common']:
        return None                               # different repo -> not sibling
    if co['root'] == session['root']:
        return None                               # same checkout -> not sibling
    return (co['root'], _branch_label(co['admin']))


def guard_override():
    """Reason string from ``WORKSPACE_GUARD_OVERRIDE``, or None when unset.

    Downgrades the two cross-workspace denies to ``ask`` — a write into a
    sibling checkout, and an unanchored process kill — for work that
    deliberately reaches past this session's own checkout."""
    v = (os.environ.get('WORKSPACE_GUARD_OVERRIDE') or '').strip()
    return v or None


# Per-command parsing spec:
#   consume:    flag -> N following tokens to skip (flag *values*, never files)
#   file_flags: flag -> (N_consumed, [indices among consumed that ARE files])
#   prog:       number of leading positionals that are program/pattern, not files
#   prog_suppressed_by: if any flag here is present, prog drops to 0
SPEC = {
    'grep': {'consume': {'-e':1,'--regexp':1,'-m':1,'--max-count':1,'-A':1,
                         '-B':1,'-C':1,'-d':1,'-D':1,'--color':1,'--colour':1,
                         '--binary-files':1,'--include':1,'--exclude':1},
             'file_flags': {'-f':(1,[0]),'--file':(1,[0])},
             'prog':1, 'prog_suppressed_by':['-e','--regexp','-f','--file']},
    # ripgrep: flag set diverges from grep enough that aliasing mis-parses
    # `rg -g '*.py' PAT path` (Q3). Own row with rg's arg-taking flags;
    # no `--include`/`--exclude` (rg uses `-g`/`--glob`); no `-d`/`-D`.
    'rg':   {'consume': {'-e':1,'--regexp':1,'-m':1,'--max-count':1,
                         '-A':1,'--after-context':1,
                         '-B':1,'--before-context':1,
                         '-C':1,'--context':1,
                         '-g':1,'--glob':1,'--iglob':1,
                         '-t':1,'--type':1,'-T':1,'--type-not':1,
                         '--type-add':1,'--type-clear':1,
                         '-M':1,'--max-columns':1,
                         '--max-filesize':1,'--max-depth':1,
                         '-r':1,'--replace':1,
                         '-E':1,'--encoding':1,
                         '--engine':1,'--pre':1,
                         '--sort':1,'--sortr':1,
                         '--context-separator':1,
                         '--field-context-separator':1,
                         '--field-match-separator':1,
                         '--regex-size-limit':1,'--dfa-size-limit':1,
                         '--path-separator':1,
                         '--color':1,'--colors':1,
                         '--hostname-bin':1},
             'file_flags': {'-f':(1,[0]),'--file':(1,[0]),
                            '--ignore-file':(1,[0])},
             'prog':1, 'prog_suppressed_by':['-e','--regexp','-f','--file']},
    'sed':  {'consume': {'-e':1,'--expression':1,'-l':1,'--line-length':1},
             'file_flags': {'-f':(1,[0]),'--file':(1,[0])},
             'prog':1, 'prog_suppressed_by':['-e','--expression','-f','--file']},
    'awk':  {'consume': {'-v':1,'--assign':1,'-F':1,'--field-separator':1},
             'file_flags': {'-f':(1,[0]),'--file':(1,[0])},
             'prog':1, 'prog_suppressed_by':['-f','--file'],
             'skip_assignments':True},
    'jq':   {'consume': {'--indent':1,'--arg':2,'--argjson':2},
             'file_flags': {'-f':(1,[0]),'--from-file':(1,[0]),
                            '--slurpfile':(2,[1]),'--rawfile':(2,[1])},
             'prog':1, 'prog_suppressed_by':['-f','--from-file']},
    # Q10: yq (both kislyuk Python wrapper and mikefarah Go variants).
    # Sibling row to jq rather than alias — flag sets diverge.
    #
    # Single-value flags (mikefarah `-o yaml`, `-I 2`, `--expression .x`,
    # kislyuk `-w 80`) are deliberately NOT declared as consume. If they
    # were, mikefarah's expression-omitted form (`yq -o json /etc/passwd`)
    # would consume the value, treat the file as the prog positional, and
    # silently allow. Leaving them unknown means the value becomes prog and
    # the file is correctly identified — secure-by-default.
    #
    # Only 2-arg flags (--arg NAME VAL, --argjson NAME VAL, --slurpfile
    # VAR FILE, --rawfile VAR FILE) are declared so NAME/VAL don't leak as
    # positionals/files.
    #
    # `-f` is a file_flag (correct for kislyuk's jq-pass-through; for
    # mikefarah's `-f`/`--front-matter` string value the token resolves
    # cwd-relative — harmless allow). `--from-file` is identical in both
    # variants. mikefarah's `--split-exp-file` is also a file flag.
    'yq':   {'consume': {'--arg':2,'--argjson':2},
             'file_flags': {'-f':(1,[0]),'--from-file':(1,[0]),
                            '--slurpfile':(2,[1]),'--rawfile':(2,[1]),
                            '--split-exp-file':(1,[0])},
             'prog':1,
             'prog_suppressed_by':['-f','--from-file','--expression']},
    'cat':  {'consume':{}, 'file_flags':{}, 'prog':0},
    'head': {'consume':{'-n':1,'-c':1,'--lines':1,'--bytes':1},'file_flags':{},'prog':0},
    'tail': {'consume':{'-n':1,'-c':1,'--lines':1,'--bytes':1},'file_flags':{},'prog':0},
    # Q9: cat-shape read-side commands that ALSO have file-naming flags.
    # Aliasing these to `cat` would silently drop the file flag (false
    # negative), so they get their own rows. Pure cat-clones with no
    # file-naming flag are listed in ALIASES below.
    'sort': {'consume':{'-S':1,'--buffer-size':1,
                        '-T':1,'--temporary-directory':1,
                        '-t':1,'--field-separator':1,
                        '-k':1,'--key':1,
                        '--batch-size':1,'--compress-program':1,
                        '--parallel':1,'--random-source':1},
             'file_flags':{'-o':(1,[0]),'--output':(1,[0]),
                           '--files0-from':(1,[0])},
             'prog':0},
    'wc':   {'consume':{}, 'file_flags':{'--files0-from':(1,[0])}, 'prog':0},
    'diff': {'consume':{'-D':1,'--ifdef':1,
                        '-F':1,'--show-function-line':1,
                        '-I':1,'--ignore-matching-lines':1,
                        '-L':1,'--label':1,
                        '-S':1,'--starting-file':1,
                        '-W':1,'--width':1,
                        '-x':1,'--exclude':1,
                        '-X':1,'--exclude-from':1,
                        '-U':1,'--unified':1,
                        '-C':1,'--context':1,
                        '--horizon-lines':1,'--tabsize':1,
                        '--line-format':1,
                        '--old-line-format':1,'--new-line-format':1,
                        '--unchanged-line-format':1,
                        '--group-format':1,
                        '--old-group-format':1,'--new-group-format':1,
                        '--unchanged-group-format':1,
                        '--changed-group-format':1},
             'file_flags':{'--from-file':(1,[0]),'--to-file':(1,[0])},
             'prog':0},
    'file': {'consume':{'-e':1,'--exclude':1,'--exclude-quiet':1,
                        '-F':1,'--separator':1,
                        '-m':1,'--magic-file':1,
                        '-P':1,'--parameter':1},
             'file_flags':{'-f':(1,[0]),'--files-from':(1,[0])},
             'prog':0},
    'hexdump':{'consume':{'-e':1,'-n':1,'-s':1},
               'file_flags':{'-f':(1,[0])},
               'prog':0},
    # Q37: cat-shape readers whose SECOND positional is an OUTPUT file
    # (`uniq IN OUT`, `xxd IN OUT`). Aliasing them to `cat` classified every
    # operand as a read, so the read-prefix exemption silently allowed the
    # write. Own rows so their value-taking flags don't shift positional
    # indices; OUTPUT_POSITIONALS (below the row table) marks operand 1+ as
    # write-context. Keep file_flags empty and prog 0 — the per-operand write
    # classification in analyze_command assumes the returned file list is
    # exactly the positional operands in order.
    'uniq': {'consume':{'-f':1,'--skip-fields':1,
                        '-s':1,'--skip-chars':1,
                        '-w':1,'--check-chars':1},
             'file_flags':{}, 'prog':0},
    # xxd long options are single-dash (`-cols`, `-seek`); `-R` takes a
    # `when` value. Attached forms (`-c16`) parse as unknown flags and fall
    # through harmlessly.
    'xxd':  {'consume':{'-c':1,'-cols':1,'-g':1,'-groupsize':1,
                        '-l':1,'-len':1,'-s':1,'-seek':1,
                        '-o':1,'-n':1,'-name':1,'-R':1},
             'file_flags':{}, 'prog':0},
    # Q11: write/mutation commands. All positionals are file paths (sources
    # and destinations alike) — the workspace check doesn't care which is
    # which, so `prog:0` over the whole positional list is sufficient.
    #
    # `-t DIR`/`--target-directory` names the destination directory: file_flag
    # so DIR participates in the workspace check. `-T`/`--no-target-directory`
    # is a no-arg flag that affects bash's interpretation, not ours.
    # Other cp/mv flags (`-r`, `-R`, `-a`, `-p`, `-i`, `-f`, `-n`, `-v`,
    # `-d`, `-l`, `-s`, `-b`, `-u`, etc.) are zero-arg and fall through
    # harmlessly. Value-taking flags like `--suffix`/`-S` or `--reflink WHEN`
    # are deliberately not declared: their values (`.bak`, `always`) leak as
    # positional file tokens, which then resolve cwd-relative and are
    # harmless allows — the secure-by-default direction.
    'cp':   {'consume':{},
             'file_flags':{'-t':(1,[0]),'--target-directory':(1,[0])},
             'prog':0},
    'mv':   {'consume':{},
             'file_flags':{'-t':(1,[0]),'--target-directory':(1,[0])},
             'prog':0},
    # `tee`: all positionals are output files. Zero-arg flags (`-a`,
    # `--append`, `-i`, `--ignore-interrupts`, `-p`) fall through.
    'tee':  {'consume':{}, 'file_flags':{}, 'prog':0},
    # `rm`: all positionals are removal targets. Every documented flag in
    # GNU/BSD rm (`-r`, `-R`, `--recursive`, `-f`, `--force`, `-i`, `-I`,
    # `--interactive`, `-v`, `--verbose`, `-d`, `--dir`, `-P`, `-x`,
    # `--one-file-system`, `--preserve-root`, `--no-preserve-root`) is
    # zero-arg, so neither `consume` nor `file_flags` is needed. Inline-value
    # forms like `--preserve-root=all` are split by `split_eq`; the unknown
    # `--preserve-root` key falls through and the value is discarded.
    'rm':   {'consume':{}, 'file_flags':{}, 'prog':0},
}
# Pure cat-shape readers — aliased to `cat`. cat's spec (no consume flags,
# no file flags, prog:0) matches every tool here: positional files only,
# no program/pattern token, no file-naming flags. Value-taking flags like
# `tac -s SEP` mean SEP is treated as a positional/file; in practice SEP
# resolves lexically inside cwd, so the false-positive risk is negligible.
ALIASES = {'egrep':'grep','fgrep':'grep','gawk':'awk','mawk':'awk',
           'less':'cat','more':'cat',
           'tac':'cat','rev':'cat','nl':'cat',
           'od':'cat',
           'strings':'cat','cmp':'cat',
           'zcat':'cat','gzcat':'cat','bzcat':'cat','xzcat':'cat'}

# Read-classified commands whose trailing positionals are OUTPUT files (Q37):
# value = index of the first output operand among the file positionals
# (`uniq IN OUT` / `xxd IN OUT` write operand 1). Those operands are checked
# as writes — no read-prefix exemption, and sibling-checkout writes deny —
# while earlier operands stay reads. Only valid for SPEC rows with no
# file_flags and prog 0, where files_in_command() returns exactly the
# positional operands in order (a unit test pins this row shape).
OUTPUT_POSITIONALS = {'uniq': 1, 'xxd': 1}


def strip_env_prefix(tokens):
    """Drop leading POSIX `NAME=VALUE` command-prefix assignments.

    `LC_ALL=C cat /etc/passwd` tokenizes with the assignment at index 0;
    without stripping, the SPEC lookup misses and the hook defers. Bash
    treats one or more such tokens at the start of a simple command as
    inline env exports — the real command begins at the first non-assignment
    token.
    """
    i = 0
    while i < len(tokens) and ASSIGNMENT_RE.match(tokens[i]):
        i += 1
    return tokens[i:]


def strip_sh_keywords(tokens):
    """Drop leading shell reserved words that may prefix the real command.

    `until grep … /outside`, `if cat /outside`, `do tail /outside` (a loop-body
    group), `! grep …`, `time cat …`, `{ cat …; }`: bash recognises the reserved
    word in command position and the guarded command follows it. Left in place,
    the leading keyword becomes ``tokens[0]`` and the SPEC / dd / ln lookups miss,
    so the whole group defers — a silent gap in the guard (Q28). Mirrors the
    keyword-skip `poison_vars` already does before its assignment rules.

    Stripped BEFORE strip_env_prefix because bash's order in a simple command is
    reserved-word(s), then inline env assignments, then the command name
    (`until LC_ALL=C grep …`).
    """
    i = 0
    while i < len(tokens) and tokens[i] in SH_KEYWORDS:
        i += 1
    return tokens[i:]


def strip_comments(cmd):
    """Remove unquoted `#` comments, keeping the newline that ends each one.

    shlex's built-in comment handling (`commenters='#'`) swallows the comment
    AND its trailing newline, so the next line's tokens merge into the
    commented line's command group — `tee log # note\\nEXIT=${PIPESTATUS[0]}`
    read the assignment as a file arg of `tee` (false positive), and
    `echo hi # note\\ncat outside` hid the `cat` inside the unguarded `echo`
    group (false negative). shlex also starts a comment at a mid-word `#`
    (`file#1`), which bash does not. Comments are therefore stripped here
    with bash's actual rule — an unquoted `#` at the start of a word — and
    shlex comment processing is disabled in main().
    """
    out = []
    in_single = in_double = False
    i, n = 0, len(cmd)
    while i < n:
        c = cmd[i]
        if in_single:
            out.append(c)
            in_single = c != "'"
            i += 1
            continue
        if not in_double and c == "'":
            in_single = True
            out.append(c); i += 1
            continue
        if c == '\\' and i + 1 < n:                # escape survives both modes
            out.append(c); out.append(cmd[i+1]); i += 2
            continue
        if c == '"':
            in_double = not in_double
            out.append(c); i += 1
            continue
        if not in_double and c == '#' \
                and (not out or out[-1] in COMMENT_PRECEDERS):
            while i < n and cmd[i] != '\n':        # keep the newline itself
                i += 1
            continue
        out.append(c); i += 1
    return ''.join(out)


def _consume_heredoc_body(text, i, delim, strip_tabs):
    """Skip a heredoc body starting at ``i`` (first char after the command
    line's newline) up to and including the terminator line, or end-of-input.

    Body lines are compared RAW — no quote/expansion parsing — so an apostrophe,
    an unbalanced quote, `</div>`, or `func(` in the body can never affect the
    scan. A line equals the terminator when it is exactly ``delim`` (for
    ``<<-``, after stripping leading tabs). Returns the index just past the
    terminator's newline; on an unterminated body, ``len(text)`` (matching bash,
    which swallows to end-of-input)."""
    n = len(text)
    while i < n:
        j = i
        while j < n and text[j] != '\n':
            j += 1
        line = text[i:j]
        if (line.lstrip('\t') if strip_tabs else line) == delim:
            return j + 1 if j < n else n          # drop the terminator line
        i = j + 1 if j < n else n                 # drop this body line
    return n


def strip_heredoc_bodies(cmd, expanded=None):
    """Remove heredoc body text from the raw command string, before shlex.

    Bash slurps everything between the newline after a `<<WORD` / `<<-WORD`
    redirection and a line equal to WORD as literal stdin data. That body can
    hold anything — apostrophes, `</div>`, `func(`, an odd number of quotes —
    none of it shell syntax. Left in place, shlex either mis-tokenizes it (body
    text becomes phantom commands / file arguments) or, on an unbalanced quote,
    aborts the *entire* parse with ``ValueError`` so a real outside-workspace
    redirect on the command line goes unchecked (issue 83).

    Stripping the body from the RAW string up front (like ``strip_comments``)
    keeps shlex's input to shell syntax only. The `<<WORD` operator and its
    delimiter stay on the command line, so the redirect handling in
    ``files_in_command``, the `<<`-delimiter skip there, and the
    ``'<<' not in tokens`` propagation guard are all unchanged; a trailing
    `<<EOF > out` redirect still parses. The body and its terminator line are
    dropped.

    Command-line quote state is tracked so a `<<` inside a quoted string is not
    mistaken for a heredoc; an unquoted `#` comment is skipped for `<<`
    detection (its text is left for ``strip_comments`` to remove). Arithmetic
    `$((a<<b))` / `((a<<b))` regions are copied verbatim — their `<<` is a shift,
    not a redirection, so they never arm a bogus delimiter. `<<<` here-strings
    are a distinct operator and never match. A `<<` with no delimiter word arms
    nothing; an unterminated body swallows to end-of-input, both matching bash.

    Every body is dropped either way; pass a list as ``expanded`` to also
    collect, in order, the raw text of the ones whose delimiter carries no
    quote and no backslash (`` <<EOF ``, not `` <<'EOF' ``). That is bash's own
    expansion rule — a quoted delimiter makes the body literal, an unquoted one
    leaves `$(…)` live — so the command-substitution scan in ``analyze_command``
    sees exactly the bodies bash would evaluate (Q35). They come back separately
    rather than left in the returned string because a body is data, not syntax:
    inline, the apostrophe in a `don't` would open a quote for the rest of the
    scan and hide a live `$(…)` after it, in that body or on a later command
    line (Q50).
    """
    out = []
    i, n = 0, len(cmd)
    in_single = in_double = False
    last = ''                                     # last emitted char (word start)
    pending = []                                  # (delim, strip_tabs) in order
    while i < n:
        c = cmd[i]
        if in_single:
            out.append(c); last = c
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if c == '\\' and i + 1 < n:
                out.append(c); out.append(cmd[i+1]); last = cmd[i+1]; i += 2
                continue
            out.append(c); last = c
            if c == '"':
                in_double = False
            i += 1
            continue
        if c == '\\' and i + 1 < n:
            out.append(c); out.append(cmd[i+1]); last = cmd[i+1]; i += 2
            continue
        if c == "'":
            in_single = True; out.append(c); last = c; i += 1
            continue
        if c == '"':
            in_double = True; out.append(c); last = c; i += 1
            continue
        if c == '#' and (last == '' or last in COMMENT_PRECEDERS):
            while i < n and cmd[i] != '\n':       # comment: no `<<` detection
                out.append(cmd[i]); i += 1
            last = ')'                            # arbitrary non-word-start char
            continue
        if c == '(' and i + 1 < n and cmd[i+1] == '(':
            end = _skip_balanced_parens(cmd, i)   # `((…))` / `$((…))` arithmetic
            out.append(cmd[i:end]); last = ')'; i = end
            continue
        if c == '<' and i + 1 < n and cmd[i+1] == '<':
            if i + 2 < n and cmd[i+2] == '<':     # `<<<` here-string, not heredoc
                out.append('<<<'); last = '<'; i += 3
                continue
            out.append('<<'); i += 2
            strip_tabs = False
            if i < n and cmd[i] == '-':
                out.append('-'); i += 1; strip_tabs = True
            while i < n and cmd[i] in ' \t':      # optional space before delim
                out.append(cmd[i]); i += 1
            delim_chars = []
            quoted = False                        # any quoting -> literal body
            while i < n and cmd[i] not in ' \t\n;|&()<>':
                d = cmd[i]
                if d == "'":
                    quoted = True
                    out.append(d); i += 1
                    while i < n and cmd[i] != "'":
                        delim_chars.append(cmd[i]); out.append(cmd[i]); i += 1
                    if i < n:
                        out.append(cmd[i]); i += 1
                elif d == '"':
                    quoted = True
                    out.append(d); i += 1
                    while i < n and cmd[i] != '"':
                        if cmd[i] == '\\' and i + 1 < n:
                            delim_chars.append(cmd[i+1])
                            out.append(cmd[i]); out.append(cmd[i+1]); i += 2
                            continue
                        delim_chars.append(cmd[i]); out.append(cmd[i]); i += 1
                    if i < n:
                        out.append(cmd[i]); i += 1
                elif d == '\\' and i + 1 < n:
                    quoted = True
                    delim_chars.append(cmd[i+1])
                    out.append(d); out.append(cmd[i+1]); i += 2
                else:
                    delim_chars.append(d); out.append(d); i += 1
            delim = ''.join(delim_chars)
            if delim:
                pending.append((delim, strip_tabs, quoted))
            last = 'x'
            continue
        if c == '\n':
            out.append('\n'); last = '\n'; i += 1
            while pending and i < n:
                delim, strip_tabs, quoted = pending.pop(0)
                end = _consume_heredoc_body(cmd, i, delim, strip_tabs)
                if expanded is not None and not quoted:
                    expanded.append(cmd[i:end])
                i = end
            continue
        out.append(c); last = c; i += 1
    return ''.join(out)


def glue_dollar_paren(tokens):
    """Re-attach a `(` to a preceding word ending in `$`.

    `(` is a punctuation char, so `$(cmd)` tokenizes as `$` + `(` + … — the
    lone `$` looks like a literal filename (bash treats a `$` not followed by
    a name/brace/paren as literal, see EXPANSION_RE) and the command
    substitution would slip through as an allow. Gluing makes the word `$(`,
    which EXPANSION_RE recognises as a runtime expansion, while the `(` is
    kept in the stream so group splitting (and checking of guarded commands
    *inside* the substitution) is unchanged.
    """
    out = []
    for t in tokens:
        if t == '(' and out and out[-1].endswith('$'):
            out[-1] += '('
        out.append(t)
    return out


# Maximum command-substitution nesting the body scanner descends. Recursion
# already terminates (each body is a proper substring, strictly shorter), so
# this is only a backstop against pathological input; beyond it, deeper bodies
# aren't analyzed — a possible missed offender, never a fabricated one or a
# silent allow.
MAX_SUBST_DEPTH = 25

# Maximum candidates the loop-variable expansion will ever materialise — both
# the values a single loop variable may be bound to and the cross product a
# token using several of them stands for. A token naming k loop variables
# expands to the product of their candidate counts, so three nested `for`s over
# 256 literals each make `cat $a/$b/$c` 16.7M paths to realpath: the hook ran
# past two minutes, and a hook that doesn't answer is a non-blocking error, so
# the guard enforces nothing at all. Over-cap POISONS (the variable drops, the
# token stays runtime-expanded -> `ask`); it never truncates, which would check
# a prefix of the candidates and silently allow the rest.
MAX_LOOP_CANDIDATES = 256


def _skip_balanced_parens(text, start):
    """Step over a run of balanced parens beginning at ``start`` (a ``(``).

    Returns the index just past the matching close, or end-of-string on
    imbalance. Used to skip ``$((…))`` arithmetic expansion, which contains no
    command to guard.
    """
    i, n, depth = start, len(text), 0
    while i < n:
        c = text[i]
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _scan_dollar_paren(text, start):
    """Scan a ``$(`` body from ``start`` (just past ``$(``) to its matching ``)``.

    Returns ``(body, end)`` — the inner substring and the index just past the
    close — or ``(None, start)`` if no balanced terminator is found. Paren
    nesting, single/double quotes, and backslash escapes inside the body are
    tracked so a ``)`` inside a quoted string or a nested ``(…)``/``$(…)`` does
    not close early. Quote tracking is flat (it does not recurse into nested
    substitutions); on the exotic input where that mis-locates the close, the
    body handed to shlex is unbalanced and analysis defers for it — fail-safe.
    """
    i, n, depth = start, len(text), 0
    in_single = in_double = False
    while i < n:
        c = text[i]
        if in_single:
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if c == '\\':
                i += 2
                continue
            if c == '"':
                in_double = False
            i += 1
            continue
        if c == '\\':
            i += 2
            continue
        if c == "'":
            in_single = True
            i += 1
            continue
        if c == '"':
            in_double = True
            i += 1
            continue
        if c == '(':
            depth += 1
            i += 1
            continue
        if c == ')':
            if depth == 0:
                return (text[start:i], i + 1)
            depth -= 1
            i += 1
            continue
        i += 1
    return (None, start)


def _scan_backticks(text, start):
    """Scan a backtick body from ``start`` (just past the opening `` ` ``) to the
    next unescaped `` ` ``. Returns ``(body, end)`` or ``(None, start)`` when the
    body is unterminated."""
    i, n = start, len(text)
    while i < n:
        c = text[i]
        if c == '\\':
            i += 2
            continue
        if c == '`':
            return (text[start:i], i + 1)
        i += 1
    return (None, start)


def command_substitutions(text, quotes=True):
    """Extract the command-substitution bodies bash would evaluate in ``text``.

    Returns the inner command string of each ``$(…)`` and backtick ``` `…` ```
    substitution appearing in an UNQUOTED or DOUBLE-QUOTED context — the two
    contexts where bash performs command substitution. A substitution inside
    single quotes is a literal and is skipped, matching bash; ``$((…))``
    arithmetic (no command inside) is skipped too.

    Scans the RAW command string, never the post-shlex tokens: shlex strips the
    quotes, losing the single-vs-double distinction that decides whether a
    ``$(…)`` even substitutes. Only the OUTERMOST substitutions are returned —
    a nested ``$(… $(…) …)`` is found by re-scanning the returned body (the
    caller recurses). A substitution with no balanced terminator before
    end-of-input contributes nothing (fail-safe: a possible missed offender,
    never a fabricated one).

    With ``quotes=False`` a ``'`` or ``"`` is ordinary text and every
    substitution is live. That is how bash reads an unquoted heredoc body —
    quoting does not apply inside one — so the apostrophe in a `don't` must not
    switch the scanner off for the rest of the body (Q50). Backslash still
    escapes the next character, matching the body's own rule that a backslash
    quotes a following `$`, backtick, backslash, or newline.
    """
    bodies = []
    i, n = 0, len(text)
    in_single = in_double = False
    while i < n:
        c = text[i]
        if in_single:
            if c == "'":
                in_single = False
            i += 1
            continue
        if c == '\\':                              # escapes next char (not in '')
            i += 2
            continue
        if quotes and c == "'" and not in_double:
            in_single = True
            i += 1
            continue
        if quotes and c == '"':
            in_double = not in_double
            i += 1
            continue
        if c == '$' and i + 1 < n and text[i + 1] == '(':
            if i + 2 < n and text[i + 2] == '(':
                i = _skip_balanced_parens(text, i + 1)   # $((…)) arithmetic
                continue
            body, end = _scan_dollar_paren(text, i + 2)
            if body is None:
                break                              # unterminated -> stop
            bodies.append(body)
            i = end
            continue
        if c == '`':
            body, end = _scan_backticks(text, i + 1)
            if body is None:
                break                              # unterminated -> stop
            bodies.append(body)
            i = end
            continue
        i += 1
    return bodies


def literal_assignment_value(raw, allow_glob=False):
    """Return the literal an assignment RHS resolves to, or None if bash
    might expand or word-split it into something the hook can't predict.

    ``raw`` is post-shlex (quotes removed). A leading ``~``/``~/…`` is
    expanded like bash expands it in assignments; ``~user`` and an unresolvable
    home stay unresolvable. An empty value is rejected because ``f=(a b)``
    tokenizes as ``f=`` + a paren run — treating it as the scalar empty
    string would miss the array's real ``$f`` (its first element).

    ``allow_glob`` keeps ``*?[`` instead of rejecting them; only
    ``literal_for_item`` passes it, and only it carries the argument for why a
    pattern is safe to keep.

    On a drive-resolving platform a leading Windows drive prefix is exempt from
    the ``:`` rule (Q48); a second colon anywhere in the value still rejects it.
    """
    if not raw:
        return None
    if raw == '~' or raw.startswith('~/'):
        raw = expand_tilde(raw)
    if raw.startswith('~'):
        return None
    impure = IMPURE_ITEM_CHARS if allow_glob else IMPURE_VALUE_CHARS
    rest = raw[2:] if DRIVE_PATHS and DRIVE_PREFIX_RE.match(raw) else raw
    if any(c in impure for c in rest):
        return None
    return raw


def literal_for_item(raw):
    """Return the literal a `for VAR in <list>` item resolves to, or None when
    bash would expand it into paths the hook can't predict (issue 70).

    Reuses the assignment-RHS purity test (``literal_assignment_value``) — same
    tilde handling, same rejection of ``$``/backtick/whitespace — then
    ADDITIONALLY rejects brace items (``{a,b}``, ``a{1..3}``): unlike an
    assignment RHS, a for-list item IS brace-expanded by bash, so treating
    ``{a,b}`` as the literal string would miss the real ``a``/``b`` paths. A
    rejected item poisons the loop variable (today's runtime-expanded ``ask``).

    A glob item is kept, as the pattern itself (issue 99). ``*``, ``?`` and
    ``[…]`` never match ``/``, so every path bash expands the pattern into has
    the pattern's own segment structure and resolves against the same
    directory: realpath the pattern and the answer holds for the whole
    expansion. This is already how a glob written straight into a file argument
    is treated (``cat docs/*.md`` allows, ``cat /etc/*.conf`` asks), so the
    loop form now agrees with the direct form — including for a pattern that
    escapes (``../*.md``), which resolves outside and prompts rather than
    needing a separate containment rule. Braces are still rejected above, so
    ``x{,/../..}`` can't smuggle a shorter path past the proxy.

    The proxy holds only for patterns whose segment count is fixed. Under
    ``shopt -s globstar`` a ``**`` matches a variable number of segments,
    including zero (``docs/**`` expands to ``docs/`` as well as
    ``docs/sub/b.md``), so a trailing ``../`` in the loop body can climb higher
    at runtime than the pattern shows — a missed prompt, not an extra one.
    Globstar is off by default in bash; closing this needs match enumeration
    (Q47).
    """
    val = literal_assignment_value(raw, allow_glob=True)
    if val is None:
        return None
    if '{' in val or '}' in val:
        return None
    return val


def for_loop_binding(g, loopmap):
    """Classify a command group as a `for NAME in <list>; …` loop header.

    Returns:
      * ``(name, [values])`` — a list of literals and/or globs: record the
        candidate set so ``$NAME`` in a later file arg is validated against
        every value (issue 70), a glob standing for its whole expansion
        (issue 99).
      * ``(name, None)`` — a `for NAME in` whose list has any non-literal or
        brace item, an empty list, a list over MAX_LOOP_CANDIDATES values, or a
        `for NAME` with no `in` (iterates ``"$@"``): the caller drops NAME from
        the maps, keeping today's poison.
      * ``None`` — not a `for NAME in` header at all (the `for ((…))` arithmetic
        form tokenizes with ``(`` at ``g[1]``), so the caller's existing
        ``poison_vars`` path runs unchanged.

    Must be called on the post-substitution tokens: a list item like ``$SP/a``
    with ``SP`` a known literal has already been resolved, so the bound value
    matches what bash iterates.

    A list item may also use an enclosing loop's variable — ``for d in docs/*;
    do for f in "$d"/*.md`` — so items are first expanded over ``loopmap``, and
    the inner variable binds one candidate per (outer candidate, item) pair.
    The cross product is what bash actually visits, and each pair is a
    pattern-as-proxy in the issue 99 sense: the outer candidate contributes
    whole segments, so the concatenation still has the segment structure of
    every path it expands to. Expanding before the caller rebinds ``name``
    reads the outer value, which is what bash expands the list with even when
    the inner loop reuses the name.

    The value count is capped incrementally, per item as well as in total, so a
    list that would blow past MAX_LOOP_CANDIDATES stops being expanded at the
    cap rather than after materialising the whole cross product (Q46).
    """
    if len(g) < 2 or os.path.basename(g[0]) != 'for':
        return None
    name = g[1]
    if not IDENT_RE.fullmatch(name) or name in NEVER_PROPAGATE:
        return None
    if len(g) < 3 or g[2] != 'in':
        return (name, None)                       # `for NAME` over "$@"
    items = g[3:]
    if not items:
        return (name, None)                       # empty list -> body never runs
    values = []
    for it in items:
        cands = expand_loop_candidates(it, loopmap)
        if cands is None:
            return (name, None)                   # over-cap item -> poison
        for cand in cands:
            val = literal_for_item(cand)
            if val is None:
                return (name, None)               # non-literal item -> poison
            values.append(val)
            if len(values) > MAX_LOOP_CANDIDATES:
                return (name, None)               # over-cap list -> poison
    return (name, values)


def expand_loop_candidates(tok, loopmap):
    """Expand every `$NAME`/`${NAME}` whose NAME is a for-loop variable into the
    full set of concrete tokens bash iterates over (issue 70).

    ``loopmap`` maps a loop variable to its candidate list (recorded by
    ``for_loop_binding`` from a ``for NAME in …`` list of literals and globs). A
    token using such a variable stands for one path per candidate — and bash
    visits ALL of them — so the caller checks every expansion and prompts if ANY
    lands outside the workspace. The candidate set is bash's iteration list,
    with a glob candidate resolving where its whole expansion resolves
    (``literal_for_item``), so an outside path can never slip past; a
    stale/over-broad set only ever adds candidates, which can prompt but never
    wrongly allow.

    Returns ``[tok]`` unchanged when the token uses no loop variable (or holds a
    backtick the hook won't evaluate). Several distinct loop variables in one
    token expand as the cross product; order is deterministic (variable order,
    then candidate order) so reasons and tests stay stable.

    Returns None when that cross product would exceed MAX_LOOP_CANDIDATES — its
    size is the product of the per-variable candidate counts, so it is known
    before any expansion happens and the work is never done (Q46). Callers treat
    None as a poison: the token keeps the runtime-expanded ``ask`` it had before
    loop propagation existed. It is deliberately not a truncation to the first N
    candidates, which would check a prefix and silently allow the rest.
    """
    if not loopmap or '$' not in tok or '`' in tok:
        return [tok]
    names = []
    for m in VAR_USE_RE.finditer(tok):
        nm = m.group(1) or m.group(2)
        if nm in loopmap and nm not in names:
            names.append(nm)
    if not names:
        return [tok]
    total = 1
    for nm in names:
        total *= len(loopmap[nm])
        if total > MAX_LOOP_CANDIDATES:
            return None
    results = [tok]
    for nm in names:
        expanded = []
        for r in results:
            for val in loopmap[nm]:
                expanded.append(substitute_vars(r, {nm: val}))
        results = expanded
    return results


def substitute_vars(tok, varmap):
    """Replace plain `$NAME`/`${NAME}` uses whose literal value is known.

    Unknown names are left in place (the remaining `$` keeps today's
    runtime-expanded `ask`). Tokens containing backticks are returned
    untouched: they hold old-style command substitution the hook can't
    evaluate, and leaving the `$` alone keeps the secure default.
    """
    if not varmap or '$' not in tok or '`' in tok:
        return tok

    def repl(m):
        name = m.group(1) or m.group(2)
        return varmap[name] if name in varmap else m.group(0)

    return VAR_USE_RE.sub(repl, tok)


def apply_assignment_group(g, varmap, persists):
    """If ``g`` is nothing but variable assignments — ``NAME=VAL ...`` or
    ``export [-flag] NAME=VAL ...`` — fold them into ``varmap`` and return
    the list of assigned names; else return None with ``varmap`` untouched.

    Must be called on the PRE-substitution tokens: bash decides what is an
    assignment before expansion, so ``$f`` expanding to ``g=x`` runs a
    command named ``g=x`` rather than assigning ``g``.

    Values are substituted and applied left to right, matching bash
    (``a=x b=$a`` sets ``b`` from the new ``a``). A name is dropped from the
    map instead of set when its value isn't a provable literal, when the
    group can't persist to later commands (``persists`` False: inside a
    subshell, a pipeline segment, or backgrounded), or when bash treats the
    name specially (NEVER_PROPAGATE). Dropping only ever restores the
    runtime-expanded `ask`. A bare ``export NAME`` re-exports without
    changing the value, so it neither sets nor drops.
    """
    toks = g
    if toks and toks[0] == 'export':
        pairs = []
        for t in toks[1:]:
            if t.startswith('-'):
                continue
            if ASSIGNMENT_RE.match(t):
                pairs.append(t)
            elif not IDENT_RE.fullmatch(t):
                return None
    else:
        if not toks or not all(ASSIGNMENT_RE.match(t) for t in toks):
            return None
        pairs = toks
    names = []
    for t in pairs:
        name, raw = t.split('=', 1)
        names.append(name)
        val = literal_assignment_value(substitute_vars(raw, varmap))
        if val is None or not persists or name in NEVER_PROPAGATE:
            varmap.pop(name, None)
        else:
            varmap[name] = val
    return names


def poison_vars(g, varmap):
    """Conservatively drop map entries a (non-assignment) command group might
    mutate. Called on the post-substitution tokens so an expanded builtin
    name (``R=read; $R f``) is still recognised.

    ``eval``/``source``/``.`` can assign anything — the whole map dies.
    Arg-assigner builtins (`read f`, `for f in …`, `declare f=…`, ...) poison
    every argument that could be a variable name; an argument still holding a
    ``$`` names a variable the hook can't identify, so the whole map dies
    (and ``read`` & co. also clobber their implicit result vars). Any token
    shaped like a mutation (``f=…`` command prefix, ``f+=…``, ``f[0]=…``,
    ``f++``, or an ``f`` immediately followed by an ``=…`` token from a torn
    ``(( f = x ))``) poisons that name. Poisoning only removes entries, so
    it can only cause an `ask`, never an `allow`.
    """
    if not varmap:
        return
    rest = strip_sh_keywords(g)
    if rest:
        name0 = os.path.basename(rest[0])
        if name0 in POISON_ALL_CMDS:
            varmap.clear()
            return
        if name0 in ARG_ASSIGNER_CMDS:
            for t in rest[1:]:
                if '$' in t:
                    varmap.clear()
                    return
                m = IDENT_RE.match(t)
                if m:
                    varmap.pop(m.group(0), None)
            for n in ('REPLY', 'MAPFILE', 'OPTARG', 'OPTIND'):
                varmap.pop(n, None)
            return
    for j, t in enumerate(g):
        m = ASSIGNISH_RE.match(t)
        if m:
            varmap.pop(m.group(1), None)
        elif IDENT_RE.fullmatch(t) and j + 1 < len(g) \
                and g[j + 1].startswith('='):
            varmap.pop(t, None)


def clobbers_ifs(g):
    """True when group ``g`` may leave IFS holding a value the hook can't see.

    A changed IFS re-splits every later expansion, so a value the hook checks
    as one word can reach the command as several: under ``IFS=x``,
    ``f=docs/x/opt/secret`` resolves inside the workspace for the hook but
    reaches ``cat`` as ``docs/`` and ``/opt/secret`` (Q49).

    ``apply_assignment_group`` catches the plain and ``export NAME=`` forms.
    This catches the rest — ``eval``/``source``/``.``, which can set it
    invisibly, and the arg-assigner builtins whose arguments name it
    (``declare IFS=x``, ``read IFS``, ``printf -v IFS``, and the ``for IFS in
    …`` header ``for_loop_binding`` refuses to bind). An argument still holding
    a ``$`` names a variable the hook can't identify, so it counts too.

    Dispatch mirrors ``poison_vars`` so the two can't disagree about what a
    group assigns. That costs a ``select`` list containing a ``$`` an early end
    to propagation, which can only cause an `ask`.

    ``unset`` is exempt: bash word-splits on the default IFS while IFS is
    unset, and the default is what the hook already models.
    """
    rest = strip_sh_keywords(g)
    if not rest:
        return False
    name0 = os.path.basename(rest[0])
    if name0 in POISON_ALL_CMDS:
        return True
    if name0 == 'unset' or name0 not in ARG_ASSIGNER_CMDS:
        return False
    for t in rest[1:]:
        if '$' in t:
            return True
        m = IDENT_RE.match(t)
        if m and m.group(0) == 'IFS':
            return True
    return False


def split_operator_runs(tokens):
    """Split a glued operator-run token into its individual operators.

    shlex's `punctuation_chars` returns a run of adjacent operator characters
    as ONE token: `(cd x); …` tokenizes `);`, `((echo …` tokenizes `((`,
    `(…));` tokenizes `));`, a newline boundary glues as `;\\n`/`|\\n`/`\\n\\n`.
    None of those compound runs match the `SEPARATORS`/`REDIR`/`DUP` vocab the
    group-splitting loop keys on, so the command boundary is missed and the two
    commands merge into one group — the guarded command is then never isolated
    and the whole string defers (Q27), or (for newlines, Q18) the next line's
    tokens are read as file args.

    Splitting is applied ONLY to pure operator runs (every char in
    `PUNCT_CHARS`); a quoted filename that happens to contain an operator char
    (or a newline) is a word token with non-punctuation chars and is left
    intact. Each run is consumed greedily longest-first against `_OPERATORS`, so
    `&>>` wins over `&>` over `&` and `<<<` over `<<`. Every single operator
    char is itself in `_OPERATORS`, so the run always fully decomposes into
    valid `SEPARATORS`/`REDIR`/`DUP` tokens with no leftover.
    """
    out = []
    for t in tokens:
        if not t or not all(c in PUNCT_CHARS for c in t):
            out.append(t)
            continue
        i, n = 0, len(t)
        while i < n:
            for op in _OPERATORS:                 # longest-first greedy match
                if t.startswith(op, i):
                    out.append(op)
                    i += len(op)
                    break
            else:
                # Unreachable while PUNCT_CHARS == the single-char operators in
                # _OPERATORS (see the comment there). Kept as a total-function
                # guard: if that invariant ever drifts, emit the remainder as
                # one token and stop rather than spin — a merged group defers,
                # which is fail-safe, never a silent allow.
                out.append(t[i:])
                break
    return out


def split_eq(tok):
    """--opt=val -> ('--opt','val'); otherwise (tok, None)."""
    if tok.startswith('--') and '=' in tok:
        k, v = tok.split('=', 1)
        return k, v
    return tok, None


def expand_tilde(tok):
    """Expand a leading `~` or `~/…` to the home directory (bash does this
    deterministically).

    Returns the expanded absolute path, or the token unchanged when it can't be
    resolved here: a `~user`/`~+`/`~-` prefix (no plain `~` or `~/`) or no
    resolvable home. Callers still defer on a returned token that begins with
    `~` or contains an expanding `$` (see EXPANSION_RE), so only the
    deterministic, fully-resolvable cases are expanded
    — `~user`'s pwd lookup and `~+`/`~-`'s dir-stack state stay out of scope.

    The home comes from :func:`resolved_home`, not `$HOME`: on Windows the hook
    runs under cmd.exe with `HOME` unset, so reading the variable left `~/x`
    unexpanded and the hook deferred while bash expanded it anyway (Q43).
    """
    if tok == '~' or tok.startswith('~/'):
        home = resolved_home()
        if home:
            return home if tok == '~' else os.path.join(home, tok[2:])
    return tok


def classify_ln(tokens):
    """For an `ln ...` command, return `(target_token, link_token_or_None)`.

    Returns None when the command isn't `ln` or uses the multi-source form
    (3+ positionals — `ln a b destdir/`), which the staging logic deliberately
    doesn't track.

    Both the symbolic-link form (`ln -s`) and the hard-link form (`ln SRC LINK`
    without `-s`) are recognised — the threat model is identical: a later read
    through LINK reaches a file that may resolve outside the workspace, and the
    lexical `realpath` check would otherwise miss it because bash hasn't
    created LINK yet. Hard links can't cross filesystems, so the exposure is
    narrower in practice, but the bypass shape is the same on a single volume.

    Consumes the value-taking flags (`-t`/`--target-directory`, `-S`/`--suffix`,
    `--backup`) so they don't surface as positionals; other flags fall through
    harmlessly.
    """
    if not tokens or os.path.basename(tokens[0]) != 'ln':
        return None
    consume = {'-t': 1, '--target-directory': 1,
               '-S': 1, '--suffix': 1, '--backup': 1}
    positionals = []
    i, n, end_opts = 1, len(tokens), False
    while i < n:
        tok = tokens[i]
        if not end_opts and tok == '--':
            end_opts = True; i += 1; continue
        if not end_opts and tok.startswith('-') and tok != '-':
            key, inline = split_eq(tok)
            if key in consume:
                i += 1 + (0 if inline is not None else consume[key]); continue
            i += 1; continue
        positionals.append(tok); i += 1
    if len(positionals) == 1:
        return (positionals[0], None)
    if len(positionals) == 2:
        return (positionals[0], positionals[1])
    return None


def classify_dd(tokens):
    """For a `dd` command, return the list of file operands (`if=`/`of=` values).

    Returns None when the command isn't `dd`. Returns `[]` when `dd` is invoked
    with no `if=`/`of=` operands (still guarded, just no files to check).

    `dd` doesn't take POSIX-style flags — every argument is `KEY=VALUE`. Only
    `if=PATH` (read source) and `of=PATH` (write destination) name files; other
    operands (`bs=`, `count=`, `conv=`, `iflag=`, `oflag=`, `seek=`, `skip=`,
    `status=`) are values, not paths. The prefix check is strict: `iflag=` does
    not start with `if=`, and `oflag=` does not start with `of=`.
    """
    if not tokens or os.path.basename(tokens[0]) != 'dd':
        return None
    files = []
    for t in tokens[1:]:
        if t.startswith('if=') or t.startswith('of='):
            files.append(t.split('=', 1)[1])
    return files


# --- Process kills (issue 125) -----------------------------------------------
# `pkill`/`killall` address a process by *pattern*, not by path, so a pattern
# naming only the program ("make check", "ginkgo") matches the same process in
# every checkout on the host — including sibling worktrees of this repo running
# their own sessions. That is a write to another session's work, addressed the
# one way the path checks above cannot see.
#
# A kill passes only when some operand ANCHORS the pattern to this workspace:
# the project root's directory name as a whole path component with a path
# separator on at least one side. A bare word does not count however distinctive
# it looks — the pattern is a substring match against a command line, not a path,
# and the hook has no way to judge whether `api` excludes a sibling.

# Flags whose *value* is the following token, so it is not a pattern operand.
# Both misparse directions are safe: swallowing a real pattern leaves nothing
# anchored (deny), and mistaking a value for an operand yields an unanchored
# operand (deny). Precision here buys fewer false denies, never a hole. The
# union of the procps-ng and BSD/macOS option sets is used, since the hook
# can't tell which implementation is on PATH.
KILL_CONSUME = {
    'pkill': frozenset({
        '-F', '--pidfile', '-G', '--group', '-J', '-M', '-N', '-P', '--parent',
        '-T', '-U', '--uid', '-g', '--pgroup', '-j', '-s', '--session',
        '-t', '--terminal', '-u', '--euid', '--signal', '--ns', '--nslist'}),
    'killall': frozenset({
        '-c', '-n', '--ns', '-o', '--older-than', '-s', '--signal',
        '-t', '-u', '--user', '-y', '--younger-than', '-Z', '--context'}),
}

# What counts as a path-component name character. A root named `repo` must not
# anchor inside `repo-branch1`, so `-`, `.` and `_` bind rather than separate.
KILL_ANCHOR_NAME = 'A-Za-z0-9._-'


def classify_pkill(tokens):
    """For a `pkill`/`killall` command, return `(name, [pattern operands])`.

    Returns None when the command isn't one of them. The operand list is empty
    for an invocation selecting purely by uid/ppid/session (`pkill -u karl`,
    `pkill -P 1234`) — still a kill with nothing tying it to this workspace, so
    the caller denies that too.

    Value-taking flags come off via `KILL_CONSUME`; `--opt=val` splits; `--`
    ends options; a signal flag (`-9`, `-TERM`) falls through as an ordinary
    flag and is skipped.
    """
    if not tokens:
        return None
    name = os.path.basename(tokens[0])
    consume = KILL_CONSUME.get(name)
    if consume is None:
        return None
    operands = []
    i, n, end_opts = 1, len(tokens), False
    while i < n:
        tok = tokens[i]
        if not end_opts and tok == '--':
            end_opts = True; i += 1; continue
        if not end_opts and tok.startswith('-') and tok != '-':
            key, inline = split_eq(tok)
            if key in consume and inline is None:
                i += 2; continue
            i += 1; continue
        operands.append(tok); i += 1
    return (name, operands)


# --- Windows `taskkill` (Q58) ------------------------------------------------
# Windows' own host-wide kill, and reachable from BOTH frontends — Git Bash
# spawns it as a native exe, PowerShell as a native command. `taskkill /IM
# node.exe` is `killall node`, so it earns the same verdict; it needs its own
# classifier because neither `KILL_CONSUME` nor a PS_SPEC row describes its
# grammar. Flags take a `/` or a `-` prefix (taskkill accepts both), or `//` once
# Git Bash's MSYS path mangling has had its say, and their names are
# case-insensitive. Nothing is positional: every selector is flagged, so a bare
# word is a syntax error rather than a pattern.
TASKKILL_CMDS = frozenset({'taskkill'})

# Value-taking flags. `/S` `/U` `/P` address a remote host and are not selection;
# the other three are.
TASKKILL_SELECTORS = frozenset({'fi', 'im', 'pid'})
TASKKILL_CONSUME = TASKKILL_SELECTORS | frozenset({'s', 'u', 'p'})

# A flag rather than an operand. `[A-Za-z?]+` is deliberately tight: it leaves a
# path-shaped token like `/tmp/x` to be read as an operand, and matches `/?`.
TASKKILL_FLAG_RE = re.compile(r'^(?:/{1,2}|-)([A-Za-z?]+)$')


def native_cmd_name(tok):
    """Command word normalized the way Windows resolves one: basename,
    lowercased, `.exe` dropped. `TASKKILL.EXE` and `taskkill` are one command to
    cmd.exe and to PowerShell alike."""
    name = os.path.basename(tok).lower()
    return name[:-4] if name.endswith('.exe') else name


def classify_taskkill(words):
    """For a `taskkill` command, return `(mode, [operand indices])`; else None.

    `mode` is 'pid' when every selector is a literal `/PID <digits>` — the by-pid
    kill left alone exactly as `kill 1234` and `Stop-Process -Id 1234` are, since
    it is the rewrite the deny recommends — and 'other' for anything else: an
    `/IM` image name, an `/FI` filter, a `/PID` the shell expands, or no selector
    at all. The caller denies an 'other' unless one of the operands anchors.

    Operands come back as INDICES into `words` so each frontend keeps its own
    token representation: PowerShell's anchor check needs the tokenizer's
    `expandable` flag, which a list of plain strings would drop.

    Returns None for a help invocation (`taskkill /?`), which kills nothing —
    the same reason `classify_mktemp` returns None for `--version`.
    """
    if not words or native_cmd_name(words[0]) not in TASKKILL_CMDS:
        return None
    kinds, operands = set(), []
    i, n = 1, len(words)
    while i < n:
        m = TASKKILL_FLAG_RE.match(words[i])
        if m is None:
            kinds.add('other')
            operands.append(i)
            i += 1
            continue
        key = m.group(1).lower()
        if key == '?':
            return None
        if key not in TASKKILL_CONSUME:
            i += 1                            # a switch (`/T`, `/F`) or unknown
            continue
        if key in TASKKILL_SELECTORS and i + 1 < n:
            if key == 'pid' and words[i + 1].isdigit():
                kinds.add('pid')
            else:
                kinds.add('other')
                operands.append(i + 1)
        i += 2
    if 'other' in kinds or not kinds:
        return ('other', operands)
    return ('pid', [])


# --- Pattern-fed kills (issue 125 follow-up) ---------------------------------
# The deny above names the kill command. The same blind kill dodges it by
# deriving pids from a pattern instead: `pgrep -f ginkgo | xargs kill`,
# `kill $(pgrep -f ginkgo)`, `ps … | grep ginkgo | awk '{print $1}' | xargs kill`.
# Worse, `grep` and `awk` are clean guarded commands, so the whole string used to
# come back `allow` — the guard green-lit the laundered kill rather than merely
# missing it.
#
# Two rules close it. A signalling command anywhere in the string suppresses the
# blanket `allow` (a clean guarded command must never speak for a kill); and the
# pattern operands of the pid *sources* run through the same
# `kill_operand_anchored` check the kill command's own pattern does.
#
# Q60 widened both. The pid source is `ps` itself, not the filter reading it — so
# a pipeline is caught whatever it filters with, and one with no filter at all
# (`ps -eo pid= | xargs kill`) is caught too. And `sh -c '<body>'` joins the
# constructs the hook refuses to vouch for, since the body is one opaque token.

SIGNAL_CMDS = frozenset({'kill', 'pkill', 'killall', 'taskkill'})

# An operand that was demonstrably not derived from a pattern: a literal pid or a
# job spec (`%1`, `%+`, `%make`). A `kill` whose operands are all of this shape
# can't be laundering anything, which is what keeps the common safe forms —
# `kill 1234`, `kill -0 4321` — clear of the rule even when they share a command
# string with a `pgrep`.
LITERAL_PID_RE = re.compile(r'^(?:\d+|%[-+%A-Za-z0-9_]*)$')

# grep-family rows whose leading positional is a pattern (see SPEC). `awk` and
# the other filters are deliberately absent: none of them is the pid source. `ps`
# is (see below), and the filter only decides which of its rows survive — so a
# pipeline is caught whatever it filters with, and reading an awk program is
# never needed. (Q60)
GREP_LIKE = frozenset({'grep', 'rg'})

# Shells whose `-c` operand is a command string. The body is one token to the
# tokenizer, so a group carrying one clears `guarded` and the string defers
# instead of emitting the blanket `allow` — same principle as the
# signalling-command suppression above, applied to a second opaque construct
# (Q60). The body is additionally re-analyzed as its own command string when the
# group runs it on this host; see `shell_c_bodies` (Q61).
SHELL_C_CMDS = frozenset({'sh', 'bash', 'zsh', 'dash', 'ksh'})

# Command words that run a shell `-c` body in THIS filesystem. The body is only
# re-analyzed under one of these, because a path in it means nothing unless the
# shell it names is the host's: `docker exec c sh -c 'cat /var/lib/…'` and
# `kubectl exec p -- sh -c …` name paths inside a container, and `ssh h sh -c …`
# a path on another machine. Naming the local wrappers rather than the remote
# ones is what keeps a container runtime nobody has heard of yet from reading as
# local — an unlisted wrapper leaves the body unanalyzed, which is where it
# started. (Q61)
LOCAL_SHELL_WRAPPERS = frozenset({
    'env', 'find', 'ionice', 'nice', 'nohup', 'setsid', 'stdbuf', 'time',
    'timeout', 'xargs',
})

# Stand-in for a grep-family pattern the hook cannot read — `grep -f patterns.txt`
# takes its patterns from a file, and an invocation may carry no pattern operand
# at all. It can never anchor, which is the whole point: a grep filtering `ps`
# output whose pattern is invisible is precisely the case the hook cannot clear,
# and reporting nothing there would read as "not a pid source".
UNREADABLE_PATTERN = '(pattern the hook cannot read)'


def signals_zero(args):
    """True when a `kill`'s arguments `args` select signal 0 and nothing else.

    Signal 0 sends no signal — it is the liveness/permission probe behind
    `while kill -0 $(pgrep -f x)`, so however its pids were derived it can't be
    laundering a kill. Both spellings count: the bare `-0` and the POSIX
    `-s 0`/`-n 0`.

    A second signal selector of any kind forfeits the exemption, because which
    one bash honors depends on its spelling: a later `-s`/`-n` overrides an
    earlier bare spec (`kill -0 -s 9` really does SIGKILL, measured), while a
    later bare spec is read as a pid instead (`kill -0 -9 4321` signals nothing
    and reports `-9` as no such process). Rather than model that per shell, take
    the exemption only when there is nothing to arbitrate. (Q62)
    """
    selectors, i = [], 0
    while i < len(args):
        t = args[i]
        if not t.startswith('-') or t in ('-', '--'):
            break                             # first operand, or end of options
        if t in ('-s', '-n'):                 # value form: the next token names it
            i += 1
            if i < len(args):
                selectors.append(args[i])
        elif t not in ('-l', '-L'):           # listing signals, not selecting one
            selectors.append(t[1:])
        i += 1
    return bool(selectors) and all(s == '0' for s in selectors)


def signal_command(tokens):
    """For a group that signals a process, return `(name, launderable)`; else None.

    `launderable` marks a kill whose target could have come from a pattern —
    everything except a `kill` that sends no signal (see `signals_zero`) or whose
    operands are all literal pids or job specs.
    `pkill`/`killall`/`taskkill` are never launderable: they carry their own
    anchor rule (`classify_pkill`, `classify_taskkill`), and folding them in here
    would let an unrelated unanchored pattern elsewhere in the string deny a
    correctly anchored one.

    A signal-0 probe still returns a name, so it suppresses the blanket `allow`
    the way every other signalling command does. Only the deny is lifted.

    An `xargs` group is inspected for a signal command word among its tokens
    rather than parsed for it. Both misparse directions of a real xargs option
    table are holes (over- and under-consuming can each hide the command word),
    whereas a plain scan can only over-report — which costs a defer, never a
    silent allow. A kill hidden inside a quoted `sh -c 'kill …'` is one token and
    is missed either way; see README Limitations.
    """
    if not tokens:
        return None
    head = native_cmd_name(tokens[0])
    if head in SIGNAL_CMDS:
        if head != 'kill':
            return (head, False)
        if signals_zero(tokens[1:]):
            return (head, False)
        operands = [t for t in tokens[1:] if not t.startswith('-')]
        launderable = bool(operands) and not all(
            LITERAL_PID_RE.match(t) for t in operands)
        return (head, launderable)
    if head == 'xargs':
        for i, t in enumerate(tokens[1:], 1):
            name = native_cmd_name(t)
            if name in SIGNAL_CMDS:
                # Only the arguments AFTER the kill are its own: `xargs -0 kill`
                # is a real kill reading NUL-delimited pids, not a probe.
                return (name, not (name == 'kill' and signals_zero(tokens[i+1:])))
    return None


def shell_c_group(tokens):
    """True when the group runs a shell `-c` command string (see SHELL_C_CMDS).

    Every token is scanned rather than just the command word, because the shell
    is usually not it: `timeout 5 bash -c …`, `xargs -I{} sh -c …` and
    `find . -exec sh -c … \\;` all carry a body the hook cannot read. A plain
    scan can only over-report, which costs a defer rather than a silent allow.

    Only a short-option cluster counts as the flag, so `-c`, `-lc` and `-euc`
    fire while `bash --version` and a long `--config=…` do not.
    """
    for i, t in enumerate(tokens):
        if os.path.basename(t) not in SHELL_C_CMDS:
            continue
        if any(u.startswith('-') and not u.startswith('--') and 'c' in u[1:]
               for u in tokens[i+1:]):
            return True
    return False


def shell_c_bodies(tokens):
    """Bodies of the shell `-c` commands this group runs on this host.

    The caller re-analyzes each as its own command string, so extraction is far
    stricter than `shell_c_group`'s scan: that one only has to suppress `allow`,
    and over-reporting costs it a defer, whereas a wrongly picked body is fed to
    the tokenizer and can invent offenders out of text that is not a command at
    all (`bash x.sh | grep -c FAIL` has both a shell and a `-c` in it). So the
    `-c` must be an option of the shell ITSELF — found in the unbroken option run
    that follows the shell word, ending at the first operand — and the body is
    the token after it.

    The group must also run the shell locally: its command word is a shell or one
    of LOCAL_SHELL_WRAPPERS. Anything else (a container runtime, `ssh`, `sudo`)
    yields nothing and the body stays unanalyzed.
    """
    if not tokens:
        return []
    head = os.path.basename(tokens[0])
    if head not in SHELL_C_CMDS and head not in LOCAL_SHELL_WRAPPERS:
        return []
    out, n = [], len(tokens)
    for i, t in enumerate(tokens):
        if os.path.basename(t) not in SHELL_C_CMDS:
            continue
        j = i + 1
        while j < n and tokens[j].startswith('-') and tokens[j] != '-':
            if not tokens[j].startswith('--') and 'c' in tokens[j][1:]:
                if j + 1 < n:
                    out.append(tokens[j+1])
                break
            j += 1
    return out


def pgrep_operands(tokens):
    """Pattern operands of a `pgrep`, or None when the command isn't one.

    `pgrep` and `pkill` share procps-ng's option set — they are one program and
    one man page — so the extraction is `classify_pkill`'s with `pkill`'s table.
    """
    if not tokens or os.path.basename(tokens[0]) != 'pgrep':
        return None
    return classify_pkill(['pkill'] + list(tokens[1:]))[1]


def grep_pattern_operands(tokens):
    """Pattern operands of a grep-family command, or None when it isn't one.

    Flags come off the same `SPEC` row `files_in_command` uses, so the two agree
    about which tokens are values. `-e`/`--regexp` values are collected, and the
    leading positional counts as a pattern only when no `prog_suppressed_by`
    flag fired — with `-e` or `-f` present that positional is a FILE, and
    mistaking a file for a pattern is the unsafe direction: it would let
    `grep foo wt-a/list.txt` anchor a pipeline it has nothing to do with.

    An **inverting** grep (`-v`) contributes no pattern of its own, only the
    stand-in: `-v` excludes rather than selects, so its pattern is not what the
    kill will receive. Counting it would be a hole — `ps … | grep ginkgo |
    grep -v wt-a/skip | xargs kill` would read as anchored by the exclusion
    while killing every OTHER checkout's ginkgo.

    Never returns an empty list for a grep-family command: an invocation whose
    patterns live in a `-f` file (or that carries none at all) yields the
    `UNREADABLE_PATTERN` stand-in, which can never anchor.
    """
    name = ALIASES.get(os.path.basename(tokens[0]), os.path.basename(tokens[0]))
    if name not in GREP_LIKE:
        return None
    spec = SPEC[name]
    pats, positionals, flags_seen, invert = [], [], set(), False
    i, n, end_opts = 1, len(tokens), False
    while i < n:
        tok = tokens[i]
        if not end_opts and tok == '--':
            end_opts = True; i += 1; continue
        if not end_opts and tok.startswith('-') and tok != '-':
            key, inline = split_eq(tok)
            flags_seen.add(key)
            # `-v` anywhere in a short cluster (`-iv`, `-rv`), and any long form
            # long enough to be unambiguous (`--inv…`, which GNU grep accepts as
            # an abbreviation of `--invert-match`).
            if key.startswith('--'):
                invert = invert or key.startswith('--inv')
            else:
                invert = invert or 'v' in key[1:]
            if key in ('-e', '--regexp'):
                if inline is not None:
                    pats.append(inline); i += 1
                else:
                    pats += tokens[i+1:i+2]; i += 2
                continue
            if key in spec['file_flags']:
                cnt = spec['file_flags'][key][0]
                i += 1 + (0 if inline is not None else cnt); continue
            if key in spec['consume']:
                i += 1 + (0 if inline is not None else spec['consume'][key]); continue
            i += 1; continue
        positionals.append(tok); i += 1
    if positionals and not any(f in flags_seen
                               for f in spec.get('prog_suppressed_by', [])):
        pats.append(positionals[0])
    if invert:
        return [UNREADABLE_PATTERN]
    return pats or [UNREADABLE_PATTERN]


def workspace_anchor_re(proj):
    """Regex for a path fragment that pins a kill pattern to workspace `proj`.

    Matches the project root's directory name as a whole path component with a
    path separator on at least one side — `<root>/…`, `…/<root>`, `…/<root>/…`.
    Returns None when the root has no basename (a filesystem root), which leaves
    every pattern unanchored.
    """
    base = os.path.basename(proj.rstrip('/\\' + os.sep))
    if not base:
        return None
    b = re.escape(base)
    sep = '[/\\\\]'
    return re.compile('(?:%s%s(?![%s]))|(?:(?<![%s])%s%s)'
                      % (sep, b, KILL_ANCHOR_NAME, KILL_ANCHOR_NAME, b, sep))


def kill_operand_anchored(tok, anchor, group_cwd, group_cwd_unknown):
    """True when kill-pattern operand `tok` anchors to this workspace.

    `~`/`~/…` and a leading `$(pwd)` / `$(git rev-parse --show-toplevel)` resolve
    first — the same forms the file checks resolve — so a pattern written that
    way anchors like the literal it expands to. A token still carrying an
    expansion afterwards (`$VAR`, `~user`) can never anchor, however much
    workspace-shaped text surrounds it: bash decides at runtime where
    `$HOME/repo/bin` lands, so the `/repo/` in it proves nothing. This is the
    same unresolvable-means-outside rule `resolve_token` applies to file args.
    """
    if anchor is None:
        return False
    tok = expand_tilde(tok)
    if not group_cwd_unknown:
        tok = resolve_subst_prefix(tok, group_cwd)
    if tok.startswith('~') or EXPANSION_RE.search(tok):
        return False
    return anchor.search(tok) is not None


def default_temp_dir():
    """Directory ``mktemp`` (and bash) fall back to when none is given
    explicitly: ``$TMPDIR`` if set, else ``/tmp``. Both are among
    ``host_temp_roots()``, so returning a path here lets the normal host-temp
    check classify it — and correctly *allow* the rare in-workspace ``$TMPDIR``
    (``classify_outside`` returns in-workspace before it reaches the host-temp
    tier)."""
    return os.environ.get('TMPDIR') or '/tmp'


def inline_tmpdir(tokens):
    """Return the literal value of a leading ``TMPDIR=`` command-prefix
    assignment (the last one wins, mirroring bash), or None when there is none
    — or when the value isn't a trustworthy literal.

    ``TMPDIR=./scratch mktemp`` sets mktemp's default location to ``./scratch``
    for that one command; :func:`strip_env_prefix` drops the assignment before
    :func:`classify_mktemp` sees it, so without capturing it the default
    resolves to the ambient host temp and false-denies an in-workspace target
    (Q34). A value carrying an unexpanded ``$`` / backtick can't be resolved
    lexically (bash would expand it), so it degrades to None -> the host-temp
    default (deny direction), never a trusted allow. `tokens` is the
    keyword-stripped group (assignments still at the front)."""
    value = None
    for tok in tokens:
        if not ASSIGNMENT_RE.match(tok):
            break                                  # first real word ends the prefix
        name, val = tok.split('=', 1)
        if name == 'TMPDIR':
            value = val
    if not value or '$' in value or '`' in value:
        return None
    return value


def classify_mktemp(tokens, default_dir=None):
    """For a ``mktemp ...`` command, return the list of path tokens it will
    create (files or directories) so ``check_file`` can classify them, or None
    when the command isn't ``mktemp`` (or is a pure informational invocation
    that creates nothing).

    mktemp's whole hazard is that its *default* location is host temp — a bare
    ``mktemp``, ``mktemp -d``, ``mktemp foo.XXXX`` (no directory component) all
    write under ``$TMPDIR``/``/tmp``. We surface that default as a concrete path
    (``default_temp_dir()``) so the same host-temp ``deny`` / repo-local steering
    fires as for ``cat /tmp/x``, while an explicit in-workspace directory
    (``-p ./scratch``) or a slashed template (``mktemp ./x.XXXX``) is allowed
    like any other in-root write.

    Flag handling is explicit (never inferred at runtime) and spans GNU + BSD.
    Short flags may be clustered (``-dp DIR`` == ``-d -p DIR``): each character
    is decoded in turn, with ``-p`` taking the rest of the cluster (or the next
    token) as its directory and terminating the run.
      * ``-p DIR`` / ``-pDIR`` / ``--tmpdir=DIR``: DIR is the target directory
        (GNU ``-p`` requires the value; ``--tmpdir`` takes it only glued with
        ``=`` — a bare ``--tmpdir`` uses the default location per GNU's
        optional-argument rule, leaving a following token as a template).
      * ``-t``: GNU (no argument) and BSD (``-t prefix``, one argument) both
        resolve to the default host-temp location, so its presence alone marks
        the target host temp regardless of any following token.
      * ``-V`` / ``--version`` / ``--help``: informational, create nothing ->
        return None (defer to normal permissions rather than deny an info call).
      * ``-d``/``--directory``, ``-u``/``--dry-run``, ``-q``/``--quiet``,
        ``--suffix=S`` and unknown flags take no path argument. ``-u`` still
        yields a classified path (its intent is a host-temp path even though no
        file is written) — the secure-by-default direction.
      * a template with a ``/`` names its own location; a bare-name template (or
        none at all) uses the default location.

    ``default_dir`` overrides the fallback location for the default-location
    branch (bare / ``-t`` / bare-name template) — it carries a literal inline
    ``TMPDIR=`` prefix captured by :func:`inline_tmpdir`. An explicit ``-p`` /
    ``--tmpdir=`` still wins over it, mirroring real mktemp's precedence.
    """
    if not tokens or os.path.basename(tokens[0]) != 'mktemp':
        return None
    tmpdir = None            # explicit directory from -p / --tmpdir=
    force_default = False    # -t / bare --tmpdir -> default host-temp location
    templates = []
    i, n, end_opts = 1, len(tokens), False
    while i < n:
        tok = tokens[i]
        if not end_opts and tok == '--':
            end_opts = True; i += 1; continue
        if not end_opts and tok.startswith('-') and tok != '-':
            key, inline = split_eq(tok)
            if key in ('-V', '--version', '--help'):
                return None                        # informational -> defer
            if tok.startswith('--'):               # long options
                if key == '--tmpdir':
                    if inline is not None:
                        tmpdir = inline
                    else:
                        force_default = True       # bare --tmpdir (optional arg)
                i += 1; continue                   # --directory/--suffix=/unknown
            # Short-option cluster: decode each character so combined flags like
            # -dp DIR / -dpDIR / -du behave as -d -p DIR / -d -u. -p takes a
            # directory value (rest of the cluster if glued, else the next
            # token) and terminates the cluster; -t marks the default host-temp
            # location; -d/-u/-q and any other short flag are boolean.
            j = 1
            consumed_next = False
            while j < len(tok):
                ch = tok[j]
                if ch == 'p':                      # -p DIR: value-taking, ends cluster
                    rest = tok[j + 1:]
                    if rest:                       # glued: -pDIR / -dpDIR
                        tmpdir = rest
                    elif i + 1 < n:                # separate: -p DIR / -dp DIR
                        tmpdir = tokens[i + 1]; consumed_next = True
                    break
                if ch == 't':                      # -t -> default host-temp location
                    force_default = True
                j += 1                             # -d/-u/-q/other: no value
            if consumed_next:
                i += 1
            i += 1; continue
        templates.append(tok); i += 1

    targets = []
    if tmpdir is not None:
        targets.append(tmpdir)                     # DIR is the target location;
                                                   # any template lands inside it
    else:
        default_needed = force_default or not templates
        if not force_default:
            for t in templates:
                if '/' in t:
                    targets.append(t)              # slashed template names its dir
                else:
                    default_needed = True          # bare name -> default location
        if default_needed:
            targets.append(default_dir if default_dir is not None
                           else default_temp_dir())
    return targets


# Closed whitelist of pure, deterministic command substitutions accepted as a
# cd/pushd target (issue 59). Keys are the canonical whitespace-normalized
# token after shlex quote-stripping; values name the resolution strategy
# applied in main() against the tracked group cwd. The hook computes the same
# value bash will — it NEVER executes the substitution text — so anything not
# matching a key exactly keeps the untracked-cd behavior (secure default).
CD_SUBST = {
    '$(git rev-parse --show-toplevel)': 'toplevel',
    '$(pwd)': 'pwd',
}


def normalize_subst(tok):
    """Whitespace-normalize a candidate substitution token for CD_SUBST lookup:
    collapse internal whitespace runs and drop the optional spaces bash allows
    just inside `$( ... )`. Pure canonicalization for a closed-list match —
    a token that still doesn't match a key is simply not whitelisted."""
    t = ' '.join(tok.split())
    if t.startswith('$( '):
        t = '$(' + t[3:]
    if t.endswith(' )'):
        t = t[:-2] + ')'
    return t


def git_toplevel(start):
    """Nearest ancestor of `start` (inclusive) containing a `.git` entry — the
    value `git rev-parse --show-toplevel` prints from `start`. The `.git` entry
    may be a directory (normal repo) or a file (worktree/submodule gitdir
    pointer); both mark the toplevel.

    Returns None — leaving the cd untracked — when no `.git` boundary is found,
    or when a git-discovery env var (GIT_DIR, GIT_WORK_TREE,
    GIT_CEILING_DIRECTORIES) is set, since those can change git's answer away
    from the plain walk-up. Purely a filesystem walk: git is never executed.
    """
    if any(os.environ.get(v) for v in
           ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_CEILING_DIRECTORIES')):
        return None
    d = start
    while True:
        try:
            if os.path.exists(os.path.join(d, '.git')):
                return d
        except OSError:
            return None
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def classify_cd(tokens):
    """Classify a command group as a cwd-shifting builtin.

    Returns:
      ('arg', path)      — cd/pushd with a resolvable positional path
      ('subst', kind)    — cd/pushd whose target is a whitelisted pure
                           substitution (see CD_SUBST); resolved in main()
                           against the tracked group cwd
      ('unknown', None)  — cd/pushd/popd whose effect we can't track precisely
                           (no arg, `cd -`, `pushd +N`, popd, `~`/`$` arg, etc.)
      (None, None)       — not a cd-family command
    """
    if not tokens:
        return (None, None)
    name = os.path.basename(tokens[0])
    if name not in ('cd', 'pushd', 'popd'):
        return (None, None)
    if name == 'popd':
        return ('unknown', None)                  # stack not tracked
    for t in tokens[1:]:
        if t.startswith('-'):
            continue                              # option flag, keep looking
        sub = CD_SUBST.get(normalize_subst(t))
        if sub is not None:
            return ('subst', sub)
        arg = expand_tilde(t)                     # `cd ~/proj` tracks via home
        if arg.startswith('+') or arg.startswith('~') or '$' in arg:
            return ('unknown', None)
        return ('arg', msys_to_native(arg))       # `cd /c/proj` tracks the drive
    return ('unknown', None)                      # bare `cd` -> $HOME


def resolve_subst_prefix(tok, group_cwd):
    """Replace a leading whitelisted pure command substitution (CD_SUBST) with
    the concrete path bash would produce, resolved against ``group_cwd``; return
    ``tok`` unchanged when it doesn't start with a resolvable whitelisted
    substitution (issue 84).

    Extends the issue-59 cd-target resolution to file operands and redirect
    targets: ``cp x "$(git rev-parse --show-toplevel)/backup/"`` — the prefix is
    the same deterministic value bash computes, so the hook substitutes it and
    lets the concatenated remainder classify normally. Only a substitution at
    the START of the token is handled. The resolved value is STRING-concatenated
    with the remainder (matching bash — no path separator is inserted, so
    ``$(pwd)x`` becomes ``<pwd>x`` not ``<pwd>/x``), which means any ``$``/``~``
    or further substitution left in the remainder still trips the caller's
    ``EXPANSION_RE`` / tilde checks and keeps the runtime-expanded ``ask``. An
    unresolvable substitution (no ``.git`` boundary, git-discovery env vars set)
    returns ``tok`` unchanged, keeping the secure default. The caller gates this
    on a known ``group_cwd`` — ``pwd``/toplevel both depend on it.
    """
    if not tok.startswith('$('):
        return tok
    body, end = _scan_dollar_paren(tok, 2)
    if body is None:
        return tok                                # unterminated -> leave as-is
    kind = CD_SUBST.get(normalize_subst('$(' + body + ')'))
    if kind is None:
        return tok                                # not whitelisted -> leave as-is
    base = group_cwd if kind == 'pwd' else git_toplevel(group_cwd)
    if base is None:
        return tok                                # no .git boundary -> leave as-is
    return base + tok[end:]


def files_in_command(tokens):
    """Return list of file-arg tokens for a simple command, or None if unguarded."""
    name = ALIASES.get(os.path.basename(tokens[0]), os.path.basename(tokens[0]))
    spec = SPEC.get(name)
    if spec is None:
        return None

    files, flags_seen, positionals = [], set(), []
    i, n, end_opts = 1, len(tokens), False
    while i < n:
        tok = tokens[i]
        if not end_opts and tok == '--':
            end_opts = True; i += 1; continue
        if not end_opts and tok.startswith('-') and tok != '-':
            key, inlineval = split_eq(tok)
            flags_seen.add(key)
            if key in spec['file_flags']:
                cnt, fidx = spec['file_flags'][key]
                if inlineval is not None:
                    if 0 in fidx: files.append(inlineval)
                    i += 1; continue
                args = tokens[i+1:i+1+cnt]
                files += [a for j, a in enumerate(args) if j in fidx]
                i += 1 + cnt; continue
            if key in spec['consume']:
                i += 1 + (0 if inlineval is not None else spec['consume'][key]); continue
            i += 1; continue                      # unknown flag -> assume no arg
        positionals.append(tok); i += 1

    prog = 0 if any(f in flags_seen for f in spec.get('prog_suppressed_by', [])) \
             else spec.get('prog', 0)
    file_positionals = positionals[prog:]
    if spec.get('skip_assignments'):              # awk: drop var=val operands
        file_positionals = [p for p in file_positionals
                            if '=' not in p.split('/')[0]]
    files += file_positionals
    return files


def build_sibling_hint(siblings, override=None):
    """One-line guidance for writes into a sibling checkout of the same repo.

    `siblings` is a list of `(token, detail)` where `detail` carries the
    offending checkout `root`, its `branch`, and the `corrected` path under the
    session's own checkout (same relative path). When `override` is set the
    write is downgraded to a prompt rather than blocked, so the wording adjusts.
    """
    seen, parts = set(), []
    for tok, d in siblings:
        key = (tok, d.get('root'), d.get('corrected'))
        if key in seen:
            continue
        seen.add(key)
        parts.append(
            "`%s` is inside another checkout of this repo (%s, on branch %s) — "
            "write to `%s` under this session's checkout instead"
            % (tok, d.get('root'), d.get('branch') or '(unknown)',
               d.get('corrected')))
    body = "; ".join(parts) + "."
    if override:
        lead = ("Sibling-checkout write(s) — prompting because "
                "WORKSPACE_GUARD_OVERRIDE is set (%s): " % override)
        tail = ""
    else:
        lead = ("Sibling-checkout write(s) blocked: writing into a different "
                "checkout of this repo lands your change on the wrong branch. ")
        tail = (" For deliberate cross-checkout work set "
                "WORKSPACE_GUARD_OVERRIDE=<reason> to downgrade this to a prompt.")
    return lead + body + tail


def build_kill_hint(kills, override=None):
    """One-line guidance for a process kill with no workspace anchor.

    `kills` is a list of `(token, detail)` where `detail` carries the kill
    command `cmd`, the offending `pattern` (None when the invocation selects by
    uid/ppid/session — or over a pipeline — with no pattern at all), the
    workspace `root` to anchor to, and the `shell` whose rewrites to name. A
    `taskkill` names its own rewrite whichever shell ran it. When
    `override` is set the kill is downgraded to a prompt rather than blocked, so
    the wording adjusts.
    """
    seen, parts, root, shell = set(), [], '', 'bash'
    for _, d in kills:
        root = root or d.get('root') or ''
        shell = d.get('shell') or shell
        key = (d.get('cmd'), d.get('pattern'))
        if key in seen:
            continue
        seen.add(key)
        if d.get('pattern') is None:
            parts.append("`%s` selects processes with no pattern at all"
                         % d.get('cmd'))
        else:
            parts.append("`%s` pattern `%s` names no path in this workspace"
                         % (d.get('cmd'), d.get('pattern')))
    body = "; ".join(parts) + "."
    if override:
        lead = ("Unanchored process kill(s) — prompting because "
                "WORKSPACE_GUARD_OVERRIDE is set (%s): " % override)
        tail = ""
    else:
        lead = ("Unanchored process kill(s) blocked: a pattern that names no "
                "path in this workspace matches the same process in every "
                "checkout on this host, so it can kill another session's work. ")
        if any(d.get('cmd') == 'taskkill' for _, d in kills):
            # Neither of the other two rewrites is `taskkill`'s: it has no
            # `pgrep`, and `Stop-Process` is a different command.
            fix = ('Fix: run `tasklist /FI "IMAGENAME eq <name>"` and '
                   "`taskkill /PID <pid>` for the one(s) you meant — `/IM` and "
                   "`/FI` reach that image in every checkout on this host.")
        elif shell == 'powershell':
            fix = ("Fix: run `Get-Process <name> | Select-Object Id, Path` and "
                   "`Stop-Process -Id <pid>` for the one(s) you meant, or filter "
                   "the pipeline to this workspace first (`Get-Process | "
                   "Where-Object { $_.Path -like '%s' } | Stop-Process`)."
                   % os.path.join(root, '*'))
        else:
            fix = ("Fix: run `pgrep -fl <pattern>` and kill the pid(s) you "
                   "meant, or put this workspace's path in the pattern "
                   "(`%s/…`)." % root)
        tail = (" " + fix + " For a deliberate cross-workspace kill set "
                "WORKSPACE_GUARD_OVERRIDE=<reason> to downgrade this to a "
                "prompt.")
    return lead + body + tail


def offender_display(tok, rp):
    """Display form of an offending file token for the decision reason.

    A relative token is suffixed with the absolute path it resolved to
    (``notes.txt -> /outside/dir/notes.txt``) so a prompt issued after an
    in-chain ``cd`` names where the path actually lands (issue 85). An
    absolute token already says so and is shown as-is.
    """
    if os.path.isabs(tok) or rp is None:
        return tok
    return '%s -> %s' % (tok, rp)


def build_reason(offenders, scratch_hint='', override=None):
    """Build the permissionDecisionReason for a blocked command.

    `offenders` is a list of `(token, category[, detail])` items from
    `check_file` / `handle_edit`. The message names the offending token(s) AND
    tells the agent how to avoid the prompt, tailored per category:

      * 'sibling'   — a WRITE into a sibling checkout of the same repo (primary
                      checkout or another worktree). `detail` carries the
                      checkout root, branch, and corrected in-session path.
      * 'kill'      — a `pkill`/`killall`, or a PowerShell `Stop-Process`, with
                      nothing naming a path in this workspace. `detail` carries
                      the command, the pattern, the workspace root to anchor to,
                      and the shell whose rewrites to name.
      * 'hosttemp'  — a path under a host-wide temp root (`/tmp`, `/var/tmp`,
                      `$TMPDIR`). Steered to a repo-local gitignored scratch dir
                      via `scratch_hint` (see build_scratch_hint).
      * 'outside'   — a path that genuinely resolves outside the project root.
      * 'expand'    — a `~`/`$VAR`/`$(...)` token bash expands at runtime; the
                      hook can't see where it lands, so it may in fact be
                      in-root and fixable by writing a literal path.
      * 'untracked' — a relative path after a `cd` the hook couldn't follow.

    Categories are emitted in a stable order; tokens within each are sorted and
    de-duplicated.
    """
    buckets = {'hosttemp': [], 'outside': [], 'expand': [], 'untracked': []}
    siblings, kills = [], []
    for item in offenders:
        tok, cat = item[0], item[1]
        detail = item[2] if len(item) > 2 else None
        if cat == 'sibling':
            siblings.append((tok, detail or {}))
        elif cat == 'kill':
            kills.append((tok, detail or {}))
        else:
            buckets[cat].append(tok)

    hints = []
    if siblings:
        hints.append(build_sibling_hint(siblings, override))
    if kills:
        hints.append(build_kill_hint(kills, override))
    if buckets['hosttemp']:
        hints.append(
            "Host-wide temp path(s): "
            + ", ".join(sorted(set(buckets['hosttemp'])))
            + ". " + scratch_hint)
    if buckets['outside']:
        hints.append(
            "Outside-workspace path(s): "
            + ", ".join(sorted(set(buckets['outside'])))
            + ". Fix: use a path inside the project root. If you genuinely "
            "need a file outside the root, approve this prompt — the native "
            "Read/Grep/Glob tools run the same check, so switching tools "
            "won't avoid it.")
    if buckets['expand']:
        hints.append(
            "Runtime-expanded arg(s) bash resolves but the hook can't: "
            + ", ".join(sorted(set(buckets['expand'])))
            + ". Fix: if this lands inside the project root, write the literal "
            "path (drop the $VAR / $(...) / leading ~), or assign the variable "
            "a plain literal earlier in the same command (VAR=./path; ...) so "
            "the hook can resolve it; otherwise use the Read/Grep tools.")
    if buckets['untracked']:
        hints.append(
            "Relative path(s) after an untracked cd: "
            + ", ".join(sorted(set(buckets['untracked'])))
            + ". Fix: give cd a literal target — bare cd, cd -, cd $HOME, and "
            "unrecognized $(...) targets drop tracking; pass an absolute path "
            "or use the Read/Grep tools.")
    return " ".join(hints)


def emit(decision, reason):
    """Print a PreToolUse decision as the hook's stdout JSON."""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason}}))


# --- Shared decision core ----------------------------------------------------
# The per-invocation config and the resolved-path classification live here, at
# module level, so every tool handler (Bash, the Read/Grep/Glob readers, and the
# Edit/Write writers) reaches an identical verdict for the same path. The Bash
# handler additionally owns the shell-specific machinery (tokenizer, cd/var
# tracking, ln-staging, the 'expand'/'untracked' categories); the native-tool
# handlers get a concrete path straight from `tool_input` and skip all of it.

# Bundle of everything a path check depends on, resolved once per invocation.
Ctx = collections.namedtuple('Ctx', [
    'proj', 'cwd', 'session_id', 'session_tmp_root', 'session_proj_dir',
    'tmp_roots', 'tmp_allow', 'tmp_action', 'read_prefixes', 'session_wt',
    'override', 'kill_anchor'])


def build_context(data):
    """Resolve the shared per-invocation context from the hook payload.

    Fields (all resolved once so the handlers can't drift):
      * ``proj`` / ``cwd`` — project root and the tool's working directory.
      * ``session_id`` — this session's UUID; scopes the Claude-managed-temp
        allow to THIS session's own task output (empty on older CLIs -> allow off).
      * ``session_tmp_root`` — ``/tmp/claude-<uid>`` realpath.
      * ``session_proj_dir`` — ``<tmp_root>/<slug>`` holding this session, for the
        sibling-session read exemption (#61) and for naming this session's
        ``scratchpad/`` in the host-temp deny (Q56); None when not locatable.
      * ``tmp_roots`` / ``tmp_allow`` / ``tmp_action`` — host-temp config.
      * ``read_prefixes`` — prefixes always allowed for READS (never writes).
      * ``session_wt`` — the session's own checkout, for the sibling-checkout
        deny; a no-op unless the session is itself a linked worktree.
      * ``override`` — WORKSPACE_GUARD_OVERRIDE reason, or None.
      * ``kill_anchor`` — compiled regex a ``pkill``/``killall`` pattern must
        match to count as scoped to this workspace (issue 125).
    """
    cwd = data.get('cwd') or os.getcwd()
    proj = os.path.realpath(os.environ.get('CLAUDE_PROJECT_DIR') or cwd)
    session_id = data.get('session_id') or ''
    session_tmp_root = claude_tmp_root()
    return Ctx(
        proj=proj, cwd=cwd, session_id=session_id,
        session_tmp_root=session_tmp_root,
        session_proj_dir=claude_session_project_dir(session_id, session_tmp_root),
        tmp_roots=host_temp_roots(cwd),
        tmp_allow=host_temp_allowlist(),
        tmp_action=host_temp_action(),
        read_prefixes=allowed_read_prefixes(cwd),
        session_wt=resolve_session_worktree(proj),
        override=guard_override(),
        kill_anchor=workspace_anchor_re(proj))


def path_is_outside(rp, proj):
    """True when resolved path ``rp`` is neither ``proj`` nor below it. Uses the
    os.sep boundary so `/projfoo` is NOT considered under `/proj`."""
    return rp != proj and not rp.startswith(proj + os.sep)


def classify_outside(rp, ctx, is_read):
    """Classify a RESOLVED realpath ``rp`` against the workspace boundary.

    Returns ``(category, detail)`` — category one of 'sibling' (carrying a detail
    dict of ``{root, branch, corrected}``), 'hosttemp', or 'outside' — or ``None``
    when the path is in-workspace or covered by an exemption. This is the shared
    core: ``handle_bash`` (after its ln-staging / expand tracking),
    ``handle_edit``, and ``handle_read_tool`` all route resolved paths through
    here, so a native ``Read`` and ``cat`` reach the identical verdict. The
    ordering mirrors the original in-``check_file`` logic exactly.
    """
    # Claude Code's own per-session task-output/scratch — the agent reading back
    # its own background output, not the boundary we guard. (Q21)
    if is_session_tmp_path(rp, ctx.session_id, ctx.session_tmp_root):
        return None
    if not path_is_outside(rp, ctx.proj):
        return None
    # Read-only: well-known Claude-owned paths (~/.claude/projects/ + configured
    # extras). Write access is never exempt here.
    if is_read and any(path_at_or_under(rp, p) for p in ctx.read_prefixes):
        return None
    # Read-only: a sibling session of the SAME project sharing the scratch parent
    # (dispatcher tails workers). (#61)
    if is_read and ctx.session_proj_dir \
            and path_at_or_under(rp, ctx.session_proj_dir):
        return None
    # Write into a sibling checkout of the same repo -> wrong-branch deny.
    if not is_read:
        sib = sibling_checkout_for(rp, ctx.session_wt)
        if sib is not None:
            root, branch = sib
            corrected = os.path.join(
                ctx.session_wt['root'], os.path.relpath(rp, root))
            return ('sibling', {'root': root, 'branch': branch,
                                'corrected': corrected})
    # Host-wide temp (/tmp, /var/tmp, $TMPDIR) -> steered deny — unless under the
    # Claude-managed temp root (keeps cross-session ask) or explicitly allowed.
    if is_host_temp(rp, ctx.tmp_roots) \
            and not path_at_or_under(rp, ctx.session_tmp_root):
        if matches_allowlist(rp, ctx.tmp_allow, ctx.cwd):
            return None
        return ('hosttemp', None)
    return ('outside', None)


def decide(offenders, ctx, bypass):
    """Map a non-empty ``offenders`` list to a ``(decision, reason)`` pair.

    Shared final step for every handler. ``deny`` when running under
    ``bypassPermissions`` (no human to answer an ask), when a host-temp path is
    hit and the configured action is ``deny``, or when a sibling-checkout write
    or an unanchored process kill is hit without an override; otherwise ``ask``.
    Both decisions block equally — this is a recoverability/steering choice, not
    a weakening of the boundary."""
    host_temp_hit = any(cat == 'hosttemp' for _, cat, _ in offenders)
    cross_hit = any(cat in ('sibling', 'kill') for _, cat, _ in offenders)
    cross_deny = cross_hit and ctx.override is None
    deny_now = bypass or (host_temp_hit and ctx.tmp_action == 'deny') \
        or cross_deny
    decision = "deny" if deny_now else "ask"
    reason = build_reason(offenders,
                          build_scratch_hint(
                              ctx.proj, scratch_dir_name(),
                              session_scratchpad(ctx.session_id,
                                                 ctx.session_proj_dir)),
                          override=ctx.override)
    return decision, reason


def resolve_native_path(raw, cwd):
    """Resolve a native tool's path field to a realpath, or None to defer.

    Native tools pass literal paths (no shell expansion), so beyond the
    deterministic ``~``/``~/…`` that ``expand_tilde`` handles, a leftover ``~``
    or any ``$`` is treated as unresolvable and the caller defers to builtin
    permissions — the posture the edit handler has used since the sibling deny.

    No :func:`msys_to_native` pass: these tools are not the shell, so on Windows
    they read a leading slash as drive-relative themselves, and the guard has to
    agree with the tool whose call it is judging."""
    if not raw or not isinstance(raw, str):
        return None
    p = expand_tilde(raw)
    if p.startswith('~') or '$' in p:
        return None
    return os.path.realpath(p if os.path.isabs(p) else os.path.join(cwd, p))


# What one command string said about process signalling, merged across its
# command-substitution bodies (issue 125 follow-up):
#   signal    — name of a signalling command seen anywhere, else None. Its mere
#               presence suppresses the blanket `allow`. Also set to `'sh -c'`
#               by an unreadable shell `-c` body, which the hook must not speak
#               for either (Q60).
#   launder   — name of a signalling command whose target could have come from a
#               pattern (see signal_command), else None.
#   patterns  — (text, anchored) for every pid-source pattern collected.
KillFacts = collections.namedtuple('KillFacts', ['signal', 'launder', 'patterns'])


def analyze_command(cmd, ctx, base_cwd, depth=0):
    """Analyze one command string against the workspace boundary.

    Returns ``(offenders, guarded)``: the list of ``check_file`` offender tuples
    and whether any guarded command was seen (so the caller can emit ``allow``
    for a guarded-but-clean command). ``base_cwd`` is the cwd file arguments
    resolve against (the tool's cwd at top level).

    This is the public entry point; :func:`_analyze_command` does the work and
    additionally reports what the string said about process signalling. Two
    things happen with that here, both of them once for the whole string:

    * ``guarded`` is cleared when anything in the string signals a process, or
      hides one behind a shell ``-c`` body. A clean guarded command must never
      speak for either — ``allow`` speaks for the WHOLE string and
      short-circuits the user's own permission settings, so ``grep x f | xargs
      kill`` and ``grep x f && sh -c '…'`` defer instead.
    * A launderable kill whose pid-source patterns ALL fail the workspace anchor
      test becomes one ``'kill'`` offender, the same category and message
      ``pkill`` produces. "Any pattern anchors ⇒ no offender" mirrors the
      ``pkill`` rule, and is what keeps the bare-word pattern of a
      ``grep -v grep`` stage from denying an otherwise anchored pipeline.
    """
    offenders, guarded, kf = _analyze_command(cmd, ctx, base_cwd, depth)
    if kf.launder and kf.patterns and not any(a for _, a in kf.patterns):
        texts = list(dict.fromkeys(t for t, _ in kf.patterns))
        named = [t for t in texts if t != UNREADABLE_PATTERN]
        offenders.append((kf.launder, 'kill', {
            'cmd': kf.launder, 'root': ctx.proj,
            'pattern': ', '.join(named or texts)}))
    return offenders, guarded and not kf.signal


def _analyze_command(cmd, ctx, base_cwd, depth=0, in_subst=False):
    """Analyze one command string; returns ``(offenders, guarded, KillFacts)``.

    Command-substitution bodies (``"$(…)"`` and backtick ``` `…` ```, plus the
    bare ``$(…)`` the group loop also splits out) and shell ``-c`` bodies are
    recursively analyzed and their offenders folded in — but their ``guarded``
    flag is DISCARDED, so a clean guarded command inside one never flips a
    deferring outer command into an ``allow``. Both recursions are strictly
    friction-adding.

    Their KillFacts DO fold in, because the two halves of a laundered kill can
    sit on opposite sides of the recursion: in ``kill "$(pgrep -f ginkgo)"`` the
    quoting hides the source from the outer tokenizer, and neither half is an
    offender on its own.

    ``in_subst`` says the string being analyzed is a command-substitution body,
    whose output the enclosing command consumes by definition. Only that gives a
    bare ``ps`` its provenance; a shell ``-c`` body inherits the flag rather than
    setting it, since running a command string is not piping its output anywhere.
    """
    proj, cwd = ctx.proj, base_cwd
    # Alias for readability at the two use sites far below; the group loop's own
    # nesting counter is `paren` so that neither can shadow the other (Q63).
    subst_depth = depth
    if not cmd.strip():
        return [], False, KillFacts(None, None, [])

    try:
        # `\n` is a punctuation char so a newline command boundary surfaces as
        # a token (it is otherwise eaten as whitespace, merging the commands on
        # either side). Removing it from `whitespace` stops shlex re-swallowing
        # it; quoted newlines stay inside their word token regardless. The runs
        # this produces (`;\n`, `|\n`, ...) are split back apart below.
        # Heredoc bodies are stripped from the raw string BEFORE shlex (see
        # strip_heredoc_bodies) so body text — which is arbitrary data, possibly
        # with unbalanced quotes — never reaches the tokenizer. Comments are then
        # stripped (see strip_comments) and shlex's own comment handling is
        # disabled — it would swallow the newline after a comment and merge the
        # next line into the commented command's group. Heredoc stripping runs
        # first so an unbalanced quote in a body can't throw off strip_comments'
        # own quote tracking for the rest of the command.
        cleaned = strip_comments(strip_heredoc_bodies(cmd))
        lex = shlex.shlex(cleaned, posix=True, punctuation_chars=';()<>|&\n')
        lex.whitespace_split = True
        lex.whitespace = lex.whitespace.replace('\n', '')
        lex.commenters = ''
        tokens = glue_dollar_paren(split_operator_runs(list(lex)))
    except ValueError:
        return [], False, KillFacts(None, None, [])   # unbalanced quotes -> defer

    # Each group is a `(cmd_tokens, redir_targets, persists, pipe)` tuple: a
    # redirect target is collected into the group it textually appears in, so
    # it later resolves against THAT group's cwd rather than the chain's
    # original cwd — this is what lets `cd /tmp && cat /dev/null > evil` flag
    # `/tmp/evil` (Q16). `persists` is True only when a variable assignment in
    # the group survives into later commands of the same string: at paren
    # depth 0 (not a subshell — `(f=x); cat $f` doesn't set f), not a pipeline
    # segment (each side of `|` runs in a subshell), and not backgrounded
    # (`f=x & …` assigns in the background copy only). `pipe` numbers the
    # pipeline the group belongs to, which is what tells a `grep` filtering `ps`
    # output apart from a `grep` reading ordinary files.
    groups, cur, cur_redir, i = [], [], [], 0
    paren, prev_sep, pipe = 0, '', 0
    while i < len(tokens):
        t = tokens[i]
        if t in SEPARATORS:
            if cur or cur_redir:
                persists = (paren == 0 and prev_sep != '|'
                            and t in (';', '\n', '&&', '||'))
                groups.append((cur, cur_redir, persists, pipe))
                cur, cur_redir = [], []
            if t == '(':
                paren += 1
            elif t == ')':
                paren = max(0, paren - 1)
            if t != '|':
                pipe += 1
            prev_sep = t
            i += 1; continue
        if t in REDIR or t in DUP:
            # An fd number written immediately before a redirect/dup operator
            # (`2>file`, `2>&1`) tokenizes as a bare digit token glued to the
            # operator. shlex drops the adjacency, so it lands as the previous
            # `cur` token; pop it so it doesn't leak as a positional file arg.
            # (A literal file *named* `2` right before a redirect is
            # indistinguishable post-tokenization — see Limitations.)
            if cur and cur[-1].isdigit():
                cur.pop()
            if t in DUP:
                # `2>&1`, `2>&-`, `<&3`: the target is a bare fd number or `-`
                # (a duplication/close target, not a path) — skip it. But
                # `>&file` (target isn't a bare fd) redirects to a file, so
                # treat that target like any other redirect target.
                if i + 1 < len(tokens):
                    nxt = tokens[i+1]
                    if not nxt.isdigit() and nxt != '-':
                        cur_redir.append(nxt)
                    i += 2; continue
                i += 1; continue
            if i + 1 < len(tokens):
                # `<<TAG` heredoc delimiter and `<<<STR` here-string content
                # are not file paths — skip without recording a redirect target.
                if t in ('<<', '<<<'):
                    i += 2; continue
                cur_redir.append(tokens[i+1]); i += 2; continue
            i += 1; continue
        cur.append(t); i += 1
    if cur or cur_redir:
        groups.append((cur, cur_redir, paren == 0 and prev_sep != '|', pipe))

    def is_outside(rp):
        return path_is_outside(rp, proj)

    def resolve_token(f, group_cwd, group_cwd_unknown):
        """Resolve a file token. Returns one of:
          ('skip', None)         — '-', flag, or allowlisted device
          ('expand', None)       — runtime-expanded (`~`/`$`); shlex can't
                                   resolve it, so secure-by-default outside.
                                   Distinct from 'untracked' so the decision
                                   reason can tailor the fix (the path may in
                                   fact land inside the root).
          ('untracked', None)    — relative path with an unknown cwd (after a
                                   `cd` we couldn't follow); secure-by-default
                                   outside.
          ('path', abspath)      — caller compares against the workspace and
                                   the staged-outside set
        Both 'expand' and 'untracked' are treated identically to a resolved
        outside path by the decision logic — they only differ in the advice
        the reason string surfaces.
        """
        if not f or f == '-' or f.startswith('-'):
            return ('skip', None)
        if is_allowed_device(f):
            return ('skip', None)
        # Bash expands `~`/`~/…` to the home dir deterministically — resolve it
        # here so an in-workspace home path isn't needlessly flagged.
        # `~user`/`~+`/`~-`, an unresolvable home, and any `$VAR`/`$(...)` stay
        # 'expand' (unresolvable).
        # A `$` bash keeps literal (trailing, or before e.g. `.`/`/` — see
        # EXPANSION_RE) is part of the filename and falls through to realpath.
        f = expand_tilde(f)
        # A leading whitelisted pure substitution (`$(git rev-parse
        # --show-toplevel)/…`, `$(pwd)/…`) is the same deterministic value bash
        # computes, so resolve it against the tracked cwd and let the remainder
        # classify normally (issue 84). Gated on a known cwd — both kinds depend
        # on it. Non-whitelisted `$(...)` is returned unchanged and still hits
        # EXPANSION_RE below.
        if not group_cwd_unknown:
            f = resolve_subst_prefix(f, group_cwd)
        if f.startswith('~') or EXPANSION_RE.search(f):
            return ('expand', None)
        # Last, so a `$VAR` is never rewritten before it's recognised as one:
        # read a leading-slash path the way Git Bash will (a no-op elsewhere).
        f = msys_to_native(f)
        if os.path.isabs(f):
            return ('path', os.path.realpath(f))
        if group_cwd_unknown:
            return ('untracked', None)
        return ('path', os.path.realpath(os.path.join(group_cwd, f)))

    # Symlinks and hard links staged by an earlier `ln OUTSIDE LINK` in the
    # same chain (with or without `-s`). Tracks the resolved abspath of each
    # `LINK` whose target is outside the workspace, so a later `cat link` can
    # be flagged before bash materialises the link and breaks the
    # lexical-realpath check (Q8 + Q17).
    staged_outside_paths = set()

    def check_file(f, group_cwd, group_cwd_unknown, is_read=False):
        """Return `(token, category, detail)` if the file resolves outside the
        workspace (directly, or via a link staged by an earlier `ln` —
        symbolic or hard — in this chain), else None.

        `category` is one of 'sibling' (a WRITE into a sibling checkout of the
        same repo), 'outside' (a resolved path outside the root), 'expand' (a
        runtime-expanded `~`/`$` token), or 'untracked' (a relative path after a
        `cd` we couldn't follow). All block identically; the category steers the
        advice, and 'sibling' additionally carries a `detail` dict (else None).

        `is_read=True` enables the ALLOWED_READ_PREFIXES exemption for
        commands that only read files (see WRITE_COMMANDS). Redirect
        targets and write commands pass is_read=False.

        A `$VAR` bound to a `for VAR in <literal list>` loop expands to one
        concrete path per candidate (issue 70); every candidate is checked and
        the first that lands outside is returned (naming that resolved
        candidate, not the `$VAR` token). Tokens with no loop variable are a
        single candidate — the original single-path check. A token whose
        candidate set is over the cap is reported as 'expand' — it IS a
        runtime-expanded token, and enumerating it is what hung the hook (Q46).
        """
        cands = expand_loop_candidates(f, loopmap)
        if cands is None:
            return (f, 'expand', None)
        for cand in cands:
            kind, rp = resolve_token(cand, group_cwd, group_cwd_unknown)
            if kind == 'skip':
                continue
            if kind in ('expand', 'untracked'):
                return (cand, kind, None)
            # A path staged outside by an earlier `ln` (symbolic or hard) is
            # flagged even when it physically lives under the session temp dir —
            # checked BEFORE the session-tmp allow so the ln-staging defense
            # (Q8/Q17) can't be bypassed by pointing a link inside the allowed
            # scratch dir.
            if rp in staged_outside_paths:
                return (offender_display(cand, rp), 'outside', None)
            # Everything past resolution — the session-tmp allow, read-prefix
            # and sibling-session exemptions, sibling-checkout / host-temp /
            # outside tiers — is the shared core (classify_outside), so a `cat`
            # and a native `Read` of the same path can't diverge. The ln-staging
            # defense above runs FIRST so a link planted inside an allowed
            # scratch dir can't launder an outside target.
            res = classify_outside(rp, ctx, is_read)
            if res is not None:
                return (offender_display(cand, rp), res[0], res[1])
        return None

    def stage_ln(target, link, group_cwd, group_cwd_unknown):
        """If `ln TARGET LINK` (symbolic or hard) points outside, record
        LINK's resolved path. LINK may be None (omitted) — then the link name
        is `basename(TARGET)` in the current group cwd, matching POSIX `ln`
        semantics."""
        tkind, trp = resolve_token(target, group_cwd, group_cwd_unknown)
        if tkind == 'skip':
            return
        if tkind == 'path' and not is_outside(trp):
            return                                # target is inside workspace
        link_tok = link if link is not None else os.path.basename(target.rstrip('/'))
        if not link_tok:
            return
        lkind, lrp = resolve_token(link_tok, group_cwd, group_cwd_unknown)
        if lkind != 'path':
            return                                # link itself unresolvable;
                                                  # later check_file catches it
                                                  # via $/~/unknown rule
        staged_outside_paths.add(lrp)

    # Per-group cwd tracking. A `cd`/`pushd` in an earlier group of the same
    # chain shifts the runtime cwd for later guarded groups; `popd` or an
    # unresolvable `cd` arg (`cd -`, `$HOME`, etc.) loses tracking.
    outside, guarded = [], False
    group_cwd, group_cwd_unknown = cwd, False
    # Literal variable propagation (issue 58): values of `NAME=literal`
    # assignments seen so far in this command string. Heredocs disable the
    # whole feature — their body lines tokenize as commands, so a body line
    # shaped like an assignment could otherwise pollute the map with values
    # bash never assigns.
    varmap, propagate = {}, '<<' not in tokens
    # Loop-variable propagation (issue 70): a `for NAME in <all-literal list>`
    # records NAME's candidate value set here instead of poisoning it, so a
    # later `$NAME` in a file arg is checked against every value bash iterates.
    # Poisoned by the same reassignment/`read`/`eval` rules as varmap (below).
    loopmap = {}
    # Process-signalling facts for this string (issue 125 follow-up). Grep
    # patterns are held with their pipeline number and only promoted to pid
    # sources after the loop, so a `ps` segment counts wherever in its pipeline
    # it sits rather than only before the grep.
    signal, launder, patterns = None, None, []
    # `kill_pipes` numbers the pipelines holding a launderable kill, so a `ps`
    # only counts as that kill's pid source when pids can actually reach it.
    ps_pipes, grep_pats, kill_pipes = set(), [], set()
    # `(body, cwd)` for every shell `-c` body this string runs locally, recursed
    # into after the loop so each resolves against the cwd of its own group.
    shell_bodies = []
    for g, g_redir, persists, pipe in groups:
        # Substitute known literals for path checking. The pre-substitution
        # tokens are kept for assignment parsing below — bash decides what is
        # an assignment before expansion.
        if varmap:
            sub_g = [substitute_vars(t, varmap) for t in g]
            g_redir = [substitute_vars(t, varmap) for t in g_redir]
        else:
            sub_g = g
        # Redirect targets attach to this group, so resolve them against the
        # group's cwd *before* any `cd` this group performs takes effect — bash
        # opens a redirect relative to the cwd in force when the redirection is
        # set up. A group never contains a `cd` plus another command (cd is its
        # own group, split by `&&`/`;`/`|`), so for the common case the group
        # cwd is simply the cwd a preceding `cd` left us in. (Q16)
        for f in g_redir:
            o = check_file(f, group_cwd, group_cwd_unknown)
            if o is not None:
                outside.append(o)
        # A nested loop's header shares its group with the enclosing `do`, so
        # the reserved words come off before the for-header check too.
        kw_g = strip_sh_keywords(sub_g)
        if propagate:
            assigned = apply_assignment_group(g, varmap, persists)
            if assigned is not None:
                # A name set as a scalar literal is no longer a loop variable.
                for nm in assigned:
                    loopmap.pop(nm, None)
                if 'IFS' in assigned:
                    # See clobbers_ifs: a changed IFS re-splits every later
                    # expansion, so stop propagating for the rest of the string.
                    varmap.clear()
                    loopmap.clear()
                    propagate = False
                continue                          # assignment-only group
            forbind = for_loop_binding(kw_g, loopmap)
            if forbind is not None:
                name, values = forbind
                varmap.pop(name, None)            # a loop var isn't a scalar
                if values is None:
                    loopmap.pop(name, None)       # unresolvable list -> poison
                else:
                    loopmap[name] = values
                continue                          # for-header: nothing to check
            if clobbers_ifs(sub_g):
                varmap.clear()
                loopmap.clear()
                propagate = False
            else:
                poison_vars(sub_g, varmap)
                poison_vars(sub_g, loopmap)       # same rules invalidate loops
        g = strip_env_prefix(kw_g)
        if not g: continue                        # keyword/env-only or redirect-only group
        # Signalling and pid-source classification runs before every `continue`
        # below, so no command shape can skip past it.
        sig = signal_command(g)
        if sig is not None:
            signal = signal or sig[0]
            if sig[1]:
                launder = launder or sig[0]
                kill_pipes.add(pipe)
        elif shell_c_group(g):
            # An unreadable command string: suppress `allow` the same way a
            # signalling command does. Checked in the `elif` so a group that is
            # BOTH (`xargs sh -c 'kill …'`) keeps its signal classification.
            signal = signal or 'sh -c'
        # The body is readable after all when it is a command string this host
        # runs — queued whatever the classification above landed on, since the
        # two answer different questions. Skipped once the cwd is untracked: the
        # body's relative paths would resolve against a stale directory and read
        # as in-workspace, and a wrong clean answer is worse than no answer.
        if not group_cwd_unknown:
            shell_bodies.extend((b, group_cwd) for b in shell_c_bodies(g))
        if os.path.basename(g[0]) == 'ps':
            ps_pipes.add(pipe)
        pg = pgrep_operands(g)
        gp = grep_pattern_operands(g) if pg is None else None
        for p in (pg or []):
            patterns.append((p, kill_operand_anchored(
                p, ctx.kill_anchor, group_cwd, group_cwd_unknown)))
        for p in (gp or []):
            grep_pats.append((pipe, p, kill_operand_anchored(
                p, ctx.kill_anchor, group_cwd, group_cwd_unknown)))
        kind, arg = classify_cd(g)
        if kind is not None:
            if kind == 'arg':
                new_cwd = arg if os.path.isabs(arg) else os.path.join(group_cwd, arg)
                group_cwd = os.path.realpath(new_cwd)
                group_cwd_unknown = False
            elif kind == 'subst' and not group_cwd_unknown:
                # Whitelisted pure substitution: compute the same value bash
                # will, from the tracked cwd. `$(pwd)` is the identity;
                # `$(git rev-parse --show-toplevel)` is the nearest `.git`
                # ancestor. Unresolvable (no `.git` boundary, git-discovery
                # env vars set) -> cd stays untracked.
                new_cwd = group_cwd if arg == 'pwd' else git_toplevel(group_cwd)
                if new_cwd is not None:
                    group_cwd = new_cwd
                else:
                    group_cwd_unknown = True
            else:
                group_cwd_unknown = True
            continue
        ln = classify_ln(g)
        if ln is not None:
            stage_ln(ln[0], ln[1], group_cwd, group_cwd_unknown)
            continue
        dd = classify_dd(g)
        if dd is not None:
            guarded = True
            for f in dd:
                o = check_file(f, group_cwd, group_cwd_unknown)
                if o is not None:
                    outside.append(o)
            continue
        mk = classify_mktemp(g, inline_tmpdir(kw_g))
        if mk is not None:
            # mktemp creates a file/dir -> write context (is_read default False).
            # An empty list (e.g. `mktemp -p ./scratch`, all-in-workspace) still
            # marks the command guarded so a clean invocation emits `allow`.
            guarded = True
            for f in mk:
                o = check_file(f, group_cwd, group_cwd_unknown)
                if o is not None:
                    outside.append(o)
            continue
        kl = classify_pkill(g)
        if kl is not None:
            # A kill is not a file op, so it never sets `guarded`: an anchored
            # pattern DEFERS rather than emitting `allow`, leaving the user's own
            # permission settings to have their say on a destructive command.
            # An unanchored one is an offender the decision layer denies.
            name, operands = kl
            if not any(kill_operand_anchored(o, ctx.kill_anchor, group_cwd,
                                             group_cwd_unknown)
                       for o in operands):
                outside.append((g[0], 'kill', {
                    'cmd': name, 'root': proj,
                    'pattern': ' '.join(operands) if operands else None}))
            continue
        tk = classify_taskkill(g)
        if tk is not None:
            # Same category and the same reason `guarded` stays clear as the
            # `pkill` branch above. The anchor scan is this command's own tokens:
            # `taskkill` reads no pipeline, so an anchor written upstream of it
            # cannot be what selects the processes.
            mode, idx = tk
            operands = [g[i] for i in idx]
            if mode != 'pid' and not any(
                    kill_operand_anchored(o, ctx.kill_anchor, group_cwd,
                                          group_cwd_unknown)
                    for o in operands):
                outside.append((g[0], 'kill', {
                    'cmd': 'taskkill', 'root': proj,
                    'pattern': ' '.join(operands) if operands else None}))
            continue
        fs = files_in_command(g)
        if fs is None: continue
        guarded = True
        cmd_name = ALIASES.get(os.path.basename(g[0]), os.path.basename(g[0]))
        is_read = cmd_name not in WRITE_COMMANDS \
            and not has_write_mode_flag(cmd_name, g)
        # Q37: for OUTPUT_POSITIONALS commands (`uniq IN OUT`, `xxd IN OUT`)
        # the operands at index >= out_from are output files — write context.
        # Indexing into `fs` is positional order because those SPEC rows have
        # no file_flags and prog 0 (files_in_command appends flag-files first,
        # which would otherwise shift indices).
        out_from = OUTPUT_POSITIONALS.get(cmd_name)
        for i, f in enumerate(fs):
            f_is_read = is_read and (out_from is None or i < out_from)
            o = check_file(f, group_cwd, group_cwd_unknown, is_read=f_is_read)
            if o is not None:
                outside.append(o)

    # A grep's pattern is a pid source only when it filters `ps` output, which is
    # a property of the whole pipeline — resolved here so a `ps` segment counts
    # wherever in its pipeline it sits, not only before the grep.
    for grep_pipe, text, anchored in grep_pats:
        if grep_pipe in ps_pipes:
            patterns.append((text, anchored))

    # `ps` is itself a pid source, and an unreadable one — the hook is not going
    # to parse `-eo` format strings. Contributing the stand-in is what catches a
    # pipeline whose filter is not a grep (`awk '/x/ {print $1}'`, `sed -n`,
    # `cut -f1`) and one with no filter at all (`ps -eo pid= | xargs kill`),
    # without reading any filter program. A readable grep pattern in the same
    # pipeline still ANCHORS it, under the unchanged any-pattern-anchors rule.
    #
    # Unlike a grep pattern, which is promoted string-wide, this needs the pids
    # to be able to REACH the kill: a grep pattern is readable, so an unanchored
    # one is evidence of intent to select processes by name wherever it sits,
    # whereas a bare `ps` says nothing until it is wired to a kill. So it counts
    # in the launderable kill's own pipeline, or anywhere inside a substitution
    # body (whose output the enclosing command consumes by definition, which is
    # what catches `kill $(ps -eo pid= | head -1)`).
    #
    # That requirement is load-bearing: without it the rule denies the commonest
    # debugging idiom there is — `./run.sh & p=$!; kill $p; ps -p $p` — where the
    # `ps` is a CONSUMER of an already-known pid, in its own group. (Q60)
    if (ps_pipes & kill_pipes) or (in_subst and ps_pipes):
        patterns.append((UNREADABLE_PATTERN, False))

    # Recurse into command-substitution bodies — `"$(mktemp)"`, backtick
    # `` `cat /outside` ``, and the bare `$(…)` the group loop also split out
    # (harmless double-analysis, deduped by the reason builder). A guarded
    # command hidden in a quoted/backtick substitution isn't tokenized as its
    # own command by shlex (the metacharacters are inside quotes), so its file
    # ops would otherwise be invisible. Each body resolves against the same
    # `base_cwd`; only its OFFENDERS bubble up — its `guarded` is dropped, so a
    # clean substitution never produces an `allow`. (Q33)
    #
    # Heredoc bodies come out of the command line first: a `cat <<'EOF'` body is
    # literal data to bash, so a `$(…)` written there never runs, while a
    # `<<EOF` body is expanded and does need scanning (Q35). The expanded ones
    # are scanned as their own units, with `quotes=False` — inside a heredoc
    # body bash applies no quoting, so an apostrophe there is text, not the
    # start of a quoted run that would swallow a later `$(…)`. (Q50)
    if subst_depth < MAX_SUBST_DEPTH:
        heredocs = []
        subs = command_substitutions(strip_heredoc_bodies(cmd, expanded=heredocs))
        for hd in heredocs:
            subs.extend(command_substitutions(hd, quotes=False))
        for body in subs:
            sub_off, _, sub_kf = _analyze_command(body, ctx, base_cwd,
                                                  subst_depth + 1, in_subst=True)
            outside.extend(sub_off)
            signal = signal or sub_kf.signal
            launder = launder or sub_kf.launder
            patterns.extend(sub_kf.patterns)

    # Shell `-c` bodies (Q61). A body is an ordinary command string that happens
    # to have arrived inside one token, so it gets the same treatment as a
    # substitution body: offenders and KillFacts fold in, `guarded` is dropped.
    # Dropping it is what keeps Q60 intact — a body reading only in-workspace
    # files still leaves the string deferring rather than earning it an `allow`,
    # so the hook never vouches for a string on the strength of a construct it
    # reads at one remove. What carries in is the group's cwd, and whatever
    # `substitute_vars` already put in the body token — over-substituting a
    # single-quoted body errs toward FINDING a path, which is the safe way to be
    # wrong; the body's own assignments are then the recursion's business.
    if subst_depth < MAX_SUBST_DEPTH:
        for body, body_cwd in shell_bodies:
            b_off, _, b_kf = _analyze_command(body, ctx, body_cwd,
                                              subst_depth + 1, in_subst)
            outside.extend(b_off)
            signal = signal or b_kf.signal
            launder = launder or b_kf.launder
            patterns.extend(b_kf.patterns)
    return outside, guarded, KillFacts(signal, launder, patterns)


def handle_bash(data):
    cmd = (data.get('tool_input') or {}).get('command', '') or ''
    ctx = build_context(data)
    if not cmd.strip():
        return
    outside, guarded = analyze_command(cmd, ctx, ctx.cwd)
    # Emit only when there's something to say. A real offender (`outside`
    # non-empty) blocks; a guarded command whose targets are all clean emits an
    # explicit `allow`; a string with neither — an unguarded command and no
    # offending redirect — defers so normal permissions apply. `outside` can be
    # non-empty even when `guarded` is False: a redirect target is a shell-level
    # write the hook resolves regardless of the command word, so `echo secret >
    # /tmp/x` (host-temp) and `ls > /outside` are honored here rather than
    # discarded by an earlier guarded-only gate (Q26). An unanchored `pkill`
    # arrives the same way — it has no file argument to guard, and an anchored
    # one deliberately leaves `guarded` False so it defers instead of allowing.
    # For the same reason `analyze_command` clears `guarded` whenever anything in
    # the string signals a process: `allow` speaks for the WHOLE string, so a
    # clean `grep` must never carry an `xargs kill` past the user's own
    # permission settings.
    if not outside and not guarded:
        return

    if outside:
        # Two reasons to block with `deny` rather than `ask`:
        #
        #  1. Host-temp paths (/tmp, /var/tmp, $TMPDIR). These get a constructive
        #     `deny` by default (WORKSPACE_GUARD_TMP_ACTION) that steers the agent
        #     to a repo-local gitignored scratch dir — host temp collides across
        #     sessions/worktrees and lives outside the root, so prompting to
        #     approve it is the wrong nudge.
        #  2. `bypassPermissions` / full-auto runs, where there is no human to
        #     answer an `ask`. Verified behavior (CLI 2.1.159): `ask` still
        #     *blocks* there, but only feeds the model an unanswerable approval
        #     prompt it stalls on. `deny` blocks identically *and* feeds the
        #     reason back, so the model can route around the path instead of
        #     stalling. (Q17)
        #
        # Interactive/headless `default` mode keeps `ask` for plain outside
        # paths so a human still gets the approve/reject prompt. Both decisions
        # are equally blocking — this is a recoverability/steering choice, not a
        # weakening of the boundary.
        #
        # Two more deny drivers reach past this session's own checkout: a WRITE
        # into a sibling checkout of the same repo (the 'sibling' category), and
        # a `pkill`/`killall` whose pattern names no path in this workspace (the
        # 'kill' category). Both deny by default — they self-heal in one agent
        # round trip — unless WORKSPACE_GUARD_OVERRIDE is set, which downgrades
        # them to `ask` for deliberate cross-workspace work.
        bypass = data.get("permission_mode") == "bypassPermissions"
        decision, reason = decide(outside, ctx, bypass)
    else:
        decision, reason = "allow", "Guarded commands target workspace/pipe only"
    emit(decision, reason)


def handle_edit(data):
    """Guard the file-writing tools (Edit, Write, MultiEdit, NotebookEdit).

    These tools always WRITE, so the file arg is checked with is_read=False: it
    gets the full outside/host-temp/sibling-checkout treatment, identical to a
    bash write of the same path. An in-workspace target, an unresolvable
    ``~``/``$`` path, or a path covered by an exemption defers (emits nothing) so
    builtin permissions apply.
    """
    ti = data.get('tool_input') or {}
    raw = ti.get('file_path') or ti.get('notebook_path') or ''
    rp = resolve_native_path(raw, data.get('cwd') or os.getcwd())
    if rp is None:
        return                                    # unresolved / absent -> defer
    ctx = build_context(data)
    res = classify_outside(rp, ctx, is_read=False)
    if res is None:
        return                                    # in-workspace / exempt -> defer
    bypass = data.get("permission_mode") == "bypassPermissions"
    decision, reason = decide([(raw, res[0], res[1])], ctx, bypass)
    emit(decision, reason)


def handle_read_tool(data):
    """Guard the native read/search tools (Read, Grep, Glob).

    These only READ, so the target is checked with is_read=True: an outside path
    prompts (`ask`), but the read-prefix (~/.claude/projects/) and
    session-tmp / sibling-session-scratch exemptions apply — so the agent reading
    back its own or a sibling worker's output is not prompted, exactly as for a
    bash `cat`/`grep`. Read's path is `file_path`; Grep/Glob use `path`.
    """
    ti = data.get('tool_input') or {}
    raw = ti.get('file_path') or ti.get('path') or ''
    rp = resolve_native_path(raw, data.get('cwd') or os.getcwd())
    if rp is None:
        return                                    # unresolved / absent -> defer
    ctx = build_context(data)
    res = classify_outside(rp, ctx, is_read=True)
    if res is None:
        return                                    # in-workspace / exempt -> defer
    bypass = data.get("permission_mode") == "bypassPermissions"
    decision, reason = decide([(raw, res[0], res[1])], ctx, bypass)
    emit(decision, reason)


# --- PowerShell tool --------------------------------------------------------
# Claude Code ships two shell tools. A Windows session without Git for Windows
# — or any session with CLAUDE_CODE_USE_POWERSHELL_TOOL=1 — gets `PowerShell`
# instead of `Bash`, and until Q51 nothing below matched it: the plugin loaded,
# reported itself active, and checked no shell command at all. The native file
# tools stayed guarded throughout, which is what made it easy to miss.
#
# This is a separate frontend, not a SPEC row. PowerShell is not a POSIX shell
# and every difference falls in the unsafe direction — most of all the escape
# character, which is a backtick. `shlex` reads `C:\Users\x` as escapes and
# yields `C:Usersx`, a path that resolves INSIDE the project root. That is a
# silent allow on the commonest Windows path form, so the tokenizer, the cmdlet
# table, and the parameter grammar are all its own. Only `classify_outside` /
# `decide` are shared, so a PowerShell `Get-Content` and a bash `cat` of the
# same path can't reach different verdicts.
#
# Posture: parse a known subset, defer on the rest. An `ask` on everything
# unparsed is the stricter reading, but the table covers little enough that
# nearly every command would prompt, and a guard that noisy gets switched off.
# The honest cost is in README's Limitations: the unparsed tail is unguarded and
# the gap is invisible to the user.

# CommonParameters. Present on every cmdlet, so they belong in every row's name
# space or prefix resolution mis-resolves against an incomplete set.
_PS_COMMON_CONSUME = frozenset({
    'erroraction', 'warningaction', 'informationaction', 'progressaction',
    'errorvariable', 'warningvariable', 'informationvariable',
    'outvariable', 'outbuffer', 'pipelinevariable'})
_PS_COMMON_SWITCHES = frozenset({'verbose', 'debug', 'whatif', 'confirm'})

# Every guarded row names its file parameters, its value-taking parameters, and
# its switches. The asymmetry that matters: failing to declare a value-taking
# parameter leaks its value into the positional list, which for a row with
# role-differentiated positionals SHIFTS every later operand — `Set-Content
# -Encoding UTF8 C:\out\x` would bind `UTF8` as the target and `C:\out\x` as the
# value, a silent allow. Wrongly declaring one only swallows a token that then
# resolves cwd-relative, a harmless prompt. So `consume` is enumerated in full
# from the cmdlet signature; guessing is what breaks it, in the bad direction.
def _ps_row(files, consume=(), switches=(), positional=()):
    consume = frozenset(consume) | _PS_COMMON_CONSUME
    switches = frozenset(switches) | _PS_COMMON_SWITCHES
    return {'files': files, 'consume': consume,
            'positional': list(positional),
            'names': frozenset(files) | consume | switches}


# `-Path`/`-LiteralPath` name the operand on nearly every provider cmdlet; the
# role differs per row, so the dict is spelled out rather than shared.
PS_SPEC = {
    # Reads. `positional` gives the role of each positional operand in order;
    # operands past the end repeat the last entry, which is what makes
    # `-Path`'s array binding (`Remove-Item a b c`) come out right.
    'get-content': _ps_row(
        {'path': 'read', 'literalpath': 'read'},
        consume=('readcount', 'totalcount', 'head', 'tail', 'filter',
                 'include', 'exclude', 'credential', 'delimiter', 'encoding',
                 'stream'),
        switches=('force', 'wait', 'raw', 'asbytestream'),
        positional=('path',)),
    # PowerShell's grep. Position 0 is -Pattern, not a file — binding it as one
    # would resolve the regex cwd-relative and mask the real operand at 1.
    'select-string': _ps_row(
        {'path': 'read', 'literalpath': 'read'},
        consume=('pattern', 'inputobject', 'include', 'exclude', 'encoding',
                 'context', 'culture'),
        switches=('simplematch', 'casesensitive', 'quiet', 'list', 'raw',
                  'notmatch', 'allmatches', 'noemphasis'),
        positional=('pattern', 'path')),
    'import-csv': _ps_row(
        {'path': 'read', 'literalpath': 'read'},
        consume=('delimiter', 'header', 'encoding'),
        switches=('useculture',),
        positional=('path',)),
    'import-clixml': _ps_row(
        {'path': 'read', 'literalpath': 'read'},
        consume=('skip', 'first'),
        switches=('includetotalcount',),
        positional=('path',)),
    # Writes. Position 1 of Set-Content/Add-Content is -Value, not a path.
    'set-content': _ps_row(
        {'path': 'write', 'literalpath': 'write'},
        consume=('value', 'filter', 'include', 'exclude', 'credential',
                 'encoding', 'stream'),
        switches=('passthru', 'force', 'nonewline', 'asbytestream'),
        positional=('path', 'value')),
    'add-content': _ps_row(
        {'path': 'write', 'literalpath': 'write'},
        consume=('value', 'filter', 'include', 'exclude', 'credential',
                 'encoding', 'stream'),
        switches=('passthru', 'force', 'nonewline', 'asbytestream'),
        positional=('path', 'value')),
    'out-file': _ps_row(
        {'filepath': 'write', 'literalpath': 'write'},
        consume=('encoding', 'width', 'inputobject'),
        switches=('append', 'force', 'noclobber', 'nonewline'),
        positional=('filepath',)),
    'tee-object': _ps_row(
        {'filepath': 'write', 'literalpath': 'write'},
        consume=('inputobject', 'variable'),
        switches=('append',),
        positional=('filepath',)),
    'export-csv': _ps_row(
        {'path': 'write', 'literalpath': 'write'},
        consume=('inputobject', 'delimiter', 'encoding', 'quotefields',
                 'usequotes'),
        switches=('append', 'force', 'noclobber', 'notypeinformation',
                  'includetypeinformation', 'useculture', 'noheader'),
        positional=('path',)),
    'export-clixml': _ps_row(
        {'path': 'write', 'literalpath': 'write'},
        consume=('inputobject', 'depth', 'encoding'),
        switches=('force', 'noclobber'),
        positional=('path',)),
    # Mutations. Source and destination are both checked; the boundary doesn't
    # care which is which, but the read/write split does — a read of the source
    # keeps the read-prefix exemption, a write of the destination doesn't.
    'copy-item': _ps_row(
        {'path': 'read', 'literalpath': 'read', 'destination': 'write'},
        consume=('filter', 'include', 'exclude', 'credential', 'fromsession',
                 'tosession'),
        switches=('container', 'force', 'recurse', 'passthru'),
        positional=('path', 'destination')),
    'move-item': _ps_row(
        {'path': 'read', 'literalpath': 'read', 'destination': 'write'},
        consume=('filter', 'include', 'exclude', 'credential'),
        switches=('force', 'passthru'),
        positional=('path', 'destination')),
    'remove-item': _ps_row(
        {'path': 'write', 'literalpath': 'write'},
        consume=('filter', 'include', 'exclude', 'credential', 'stream'),
        switches=('recurse', 'force'),
        positional=('path',)),
    # -NewName is a name, not a path: it can't carry the operand out of root.
    'rename-item': _ps_row(
        {'path': 'write', 'literalpath': 'write'},
        consume=('newname', 'credential'),
        switches=('force', 'passthru'),
        positional=('path', 'newname')),
}

# Not guarded — `Set-Location` reads no file — but tracked, because without it
# `Set-Location C:\out; Get-Content secrets.txt` resolves the relative operand
# against the session cwd and silently allows it. Mirrors the bash cd tracking.
PS_LOCATION_SPEC = _ps_row(
    {'path': 'read', 'literalpath': 'read'},
    consume=('stackname',), switches=('passthru',), positional=('path',))
PS_LOCATION_CMDS = frozenset({'set-location', 'push-location', 'pop-location'})

# PowerShell resolves aliases before parameters, and several collide with the
# POSIX names in SPEC while meaning something with a different flag set — `cat`
# is Get-Content, `sc` is Set-Content, `rm` is Remove-Item. Routing those to the
# SPEC row of the same name is exactly the aliasing mistake Q3 recorded.
PS_ALIASES = {
    'gc': 'get-content', 'cat': 'get-content', 'type': 'get-content',
    'sls': 'select-string',
    'ipcsv': 'import-csv', 'epcsv': 'export-csv',
    'sc': 'set-content', 'set': 'set-content',
    'ac': 'add-content',
    'tee': 'tee-object',
    'cpi': 'copy-item', 'copy': 'copy-item', 'cp': 'copy-item',
    'mi': 'move-item', 'move': 'move-item', 'mv': 'move-item',
    'ri': 'remove-item', 'rm': 'remove-item', 'del': 'remove-item',
    'erase': 'remove-item', 'rd': 'remove-item', 'rmdir': 'remove-item',
    'rni': 'rename-item', 'ren': 'rename-item',
    'cd': 'set-location', 'sl': 'set-location', 'chdir': 'set-location',
    'pushd': 'push-location', 'popd': 'pop-location',
    'spps': 'stop-process', 'kill': 'stop-process',
}

# --- PowerShell process kills (Q57) ------------------------------------------
# `Stop-Process` is this shell's `pkill`, and the bash section on issue 125 gives
# the reasoning: a kill selected by process name, or fed by an unfiltered
# pipeline, reaches that process in every checkout on the host. Its own rule
# rather than a PS_SPEC row, because nothing here is a file path — selection is
# by name, by pid, or by piped object.
PS_KILL_CMDS = frozenset({'stop-process'})

# The three selectors, and the verdict each earns:
#   -Id           kill by pid. Untouched, exactly as the bash side leaves
#                 `kill <pid>` alone — it is the rewrite the deny recommends.
#   -Name         `killall`. Denied outright: a process name carries no path, so
#                 nothing in the statement can scope it to this workspace.
#   -InputObject  the processes came from somewhere the hook can see, so the
#                 rest of the statement is where an anchor may appear. Bare
#                 pipeline input (no selector at all) reads the same way.
PS_KILL_SELECTORS = frozenset({'id', 'name', 'inputobject'})
PS_KILL_SPEC = _ps_row(
    {},                                   # nothing here names a file
    consume=tuple(PS_KILL_SELECTORS),
    switches=('force', 'passthru'))

# A kill is judged over the whole STATEMENT, not one pipeline segment: the
# anchored rewrite is `Get-Process | Where-Object { $_.Path -like '<root>\*' } |
# Stop-Process`, where the anchor sits two segments upstream of the kill. So `|`,
# `(`/`)` and `{`/`}` all stay inside the scope and only these end it.
PS_STATEMENT_OPS = frozenset({';', '\n', '&&', '||', '&'})


# `@"` / `@'` opens a here-string; the body ends at a line whose first
# non-blank characters are the closing delimiter.
PS_HERE_OPEN_RE = re.compile(r"@([\"'])[ \t]*\r?\n")


def ps_strip_here_strings(text, literal_only=False):
    """Replace here-string bodies with an empty literal, or None if one is open.

    Body text is arbitrary data — it may hold unbalanced quotes, `#`, or
    anything else that would derail the tokenizer — so it never reaches it.
    `literal_only` keeps the expandable (`@"`) form intact for the subexpression
    scan, since PowerShell *does* run a `$(…)` written there; the literal (`@'`)
    form is inert and drops either way. Same split as the bash heredoc handling
    in Q35.
    """
    out, i = [], 0
    while True:
        m = PS_HERE_OPEN_RE.search(text, i)
        if not m:
            out.append(text[i:])
            return ''.join(out)
        close = re.compile(r"\r?\n[ \t]*" + re.escape(m.group(1)) + r"@")
        e = close.search(text, m.end() - 1)
        if not e:
            return None                       # unterminated -> caller defers
        if literal_only and m.group(1) == '"':
            out.append(text[i:e.end()])
        else:
            out.append(text[i:m.start()])
            out.append("''")
        i = e.end()


def _ps_scan_paren(text, start):
    """Index just past the `)` balancing the `(` at `start`, or None.

    Single-quoted runs are opaque. Double-quoted runs are not: PowerShell
    expands `$(…)` inside them, so a nested subexpression is scanned rather than
    skipped — otherwise a `)` in ordinary quoted prose would close the wrong
    paren and truncate the body.
    """
    depth, i, n = 0, start, len(text)
    while i < n:
        c = text[i]
        if c == '`':
            i += 2
            continue
        if c == "'":
            i += 1
            while i < n and text[i] != "'":
                i += 1
            i += 1
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                if text[i] == '`':
                    i += 2
                    continue
                if text[i] == '$' and i + 1 < n and text[i + 1] == '(':
                    j = _ps_scan_paren(text, i + 1)
                    if j is None:
                        return None
                    i = j
                    continue
                i += 1
            i += 1
            continue
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def ps_subexpressions(text, depth=0):
    """Split `$(…)` / `@(…)` out of `text`.

    Returns `(masked, bodies)`. Each subexpression is replaced by a bare `$` so
    the token that contained it still reads as runtime-expanded, and its body is
    returned for analysis in its own right — the same strictly-friction-adding
    treatment bash command substitutions get, and for the same reason: a guarded
    cmdlet written inside one is invisible to the outer tokenizer.
    """
    out, bodies, i, n = [], [], 0, len(text)
    while i < n:
        c = text[i]
        if c == '`':
            out.append(text[i:i + 2])
            i += 2
            continue
        if c == "'":
            j = i + 1
            while j < n and text[j] != "'":
                j += 1
            out.append(text[i:j + 1])
            i = j + 1
            continue
        if c in '$@' and i + 1 < n and text[i + 1] == '(':
            j = _ps_scan_paren(text, i + 1)
            if j is None:
                out.append(c)
                i += 1
                continue
            body = text[i + 2:j - 1]
            bodies.append(body)
            if depth < MAX_SUBST_DEPTH:
                bodies.extend(ps_subexpressions(body, depth + 1)[1])
            out.append('$')
            i = j
            continue
        if c == '"':
            j, seg, closed = i + 1, ['"'], False
            while j < n:
                if text[j] == '"':
                    closed = True
                    break
                if text[j] == '`':
                    seg.append(text[j:j + 2])
                    j += 2
                    continue
                if text[j] == '$' and j + 1 < n and text[j + 1] == '(':
                    k = _ps_scan_paren(text, j + 1)
                    if k is None:
                        break
                    body = text[j + 2:k - 1]
                    bodies.append(body)
                    if depth < MAX_SUBST_DEPTH:
                        bodies.extend(ps_subexpressions(body, depth + 1)[1])
                    seg.append('$')
                    j = k
                    continue
                seg.append(text[j])
                j += 1
            # Never close the run that the input left open: fabricating the
            # quote here would hand the tokenizer a balanced string, and an
            # unbalanced command has to defer rather than half-parse.
            if closed:
                seg.append('"')
                j += 1
            out.append(''.join(seg))
            i = j
            continue
        out.append(c)
        i += 1
    return ''.join(out), bodies


def ps_tokenize(text):
    """Tokenize a PowerShell command line in argument mode.

    Returns `(kind, value, expandable, quoted)` tuples — kind is 'word', 'op'
    (a command separator) or 'redir' — or None when a quote is left open, which
    defers the whole command.

    `expandable` records a `$` seen outside single quotes: the value is decided
    at runtime, so the caller reports the token rather than resolving it.
    `quoted` records that some part of the value came from quotes, which is what
    stops an array operand (`-Path a,b`) from being split inside a quoted name.
    """
    toks = []
    buf, expandable, quoted, started = [], False, False, False

    def flush():
        if started:
            toks.append(('word', ''.join(buf), expandable, quoted))
        del buf[:]
        return False, False, False

    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in ' \t\r':
            expandable, quoted, started = flush()
            i += 1
            continue
        if c == '\n':
            expandable, quoted, started = flush()
            toks.append(('op', '\n', False, False))
            i += 1
            continue
        if c == '#' and not started:                     # line comment
            while i < n and text[i] != '\n':
                i += 1
            continue
        if not started and text.startswith('<#', i):     # block comment
            end = text.find('#>', i + 2)
            i = n if end < 0 else end + 2
            continue
        if c == '`':
            if i + 1 >= n:
                i += 1
                continue
            if text[i + 1] == '\n':                      # line continuation
                i += 2
                continue
            buf.append(text[i + 1])
            started = True
            i += 2
            continue
        if c == "'":
            started, quoted = True, True
            i += 1
            while True:
                if i >= n:
                    return None
                if text[i] == "'":
                    if i + 1 < n and text[i + 1] == "'":
                        buf.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(text[i])
                i += 1
            continue
        if c == '"':
            started, quoted = True, True
            i += 1
            while True:
                if i >= n:
                    return None
                ch = text[i]
                if ch == '`' and i + 1 < n:
                    buf.append(text[i + 1])
                    i += 2
                    continue
                if ch == '"':
                    if i + 1 < n and text[i + 1] == '"':
                        buf.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                if ch == '$':
                    expandable = True
                buf.append(ch)
                i += 1
            continue
        if c == '>':
            # `2>` / `*>` glue the stream selector onto the operator; without
            # this the selector flushes as a word and becomes a file operand.
            pending = ''.join(buf)
            if started and not quoted and (pending.isdigit() or pending == '*'):
                op = pending + c
                del buf[:]
                expandable, quoted, started = False, False, False
            else:
                op = c
                expandable, quoted, started = flush()
            i += 1
            if i < n and text[i] == '>':
                op += '>'
                i += 1
            toks.append(('redir', op, False, False))
            continue
        if c in '|&':
            expandable, quoted, started = flush()
            op = c
            if i + 1 < n and text[i + 1] == c:
                op += c
                i += 1
            toks.append(('op', op, False, False))
            i += 1
            continue
        if c in ';(){}':
            # Script-block braces and grouping parens are separators, so a
            # `ForEach-Object { Get-Content … }` body is analyzed as its own
            # command instead of vanishing into the caller's operands.
            expandable, quoted, started = flush()
            toks.append(('op', c, False, False))
            i += 1
            continue
        if c == '$':
            expandable = True
        buf.append(c)
        started = True
        i += 1
    flush()
    return toks


def ps_expand_tilde(tok):
    """Expand a leading `~`, `~/` or `~\\` — PowerShell accepts both separators."""
    if tok == '~' or tok[:2] in ('~/', '~\\'):
        home = resolved_home()
        if home:
            return home if tok == '~' else os.path.join(home, tok[2:])
    return tok


def ps_is_absolute(p):
    """True for the path forms PowerShell resolves without the current directory:
    drive-qualified (`C:\\x`), UNC (`\\\\host\\share`), and root-relative."""
    return bool(DRIVE_PREFIX_RE.match(p)) or p.startswith('\\') \
        or p.startswith('/')


def ps_realpath(p, cwd):
    """Resolve a PowerShell path token to a comparable absolute path.

    No :func:`msys_to_native` pass, for the reason ``resolve_native_path`` gives:
    the guard has to agree with the tool whose call it is judging. That mount
    table is Git Bash's, and PowerShell has never heard of it — a leading slash
    there is the root of the current drive, not `/c/…` or `%TMP%`.
    """
    full = p if ps_is_absolute(p) else os.path.join(cwd, p)
    if DRIVE_PATHS:
        return os.path.realpath(full)
    # The PowerShell tool only exists on Windows, so a non-drive host here means
    # the test suite. `os.path` does not consider `C:\x` absolute on POSIX, and
    # handing it to realpath would resolve it against the process directory and
    # land it INSIDE the project root — the fixture would then assert a silent
    # allow and call it a pass. Normalize the native forms lexically instead; no
    # POSIX root can contain a drive-qualified or UNC path. The test runs after
    # the join, so a relative operand read under a tracked `Set-Location C:\…`
    # is caught too.
    if DRIVE_PREFIX_RE.match(full) or full.startswith('\\'):
        return os.path.normpath(full.replace('\\', '/'))
    return os.path.realpath(full)


def ps_resolve_param(name, spec):
    """Resolve a parameter name the way PowerShell does — exact match, else an
    unambiguous prefix. An ambiguous or unknown name falls back to itself, where
    it reads as a switch: the value behind it stays a positional operand and
    still gets checked, which is the safe direction to be wrong in."""
    if name in spec['names']:
        return name
    hits = [k for k in spec['names'] if k.startswith(name)]
    return hits[0] if len(hits) == 1 else name


def ps_bind_args(args, spec):
    """Bind a segment's argument tokens to file roles.

    Returns `(token, expandable, quoted, role)` for each operand that names a
    file, role being 'read' or 'write'.

    Two passes, because PowerShell binds by name first and only then fills the
    positional slots that are still free. `Select-String -Pattern foo <file>`
    puts the file in slot 1 (-Path) however the two are ordered; a single
    left-to-right pass would hand it slot 0 (-Pattern), classify a real read as
    a non-file operand, and allow it silently.
    """
    named, positionals, bound, i, n = [], [], set(), 0, len(args)
    while i < n:
        _, val, exp, quoted = args[i]
        if val == '--%':
            break                       # stop-parsing: the rest is verbatim
        if len(val) > 1 and val[0] == '-' and not exp \
                and not val[1].isdigit() and val[1] != '.':
            name, attached = val[1:], None
            if ':' in name:             # `-Path:C:\x` binds without a space
                name, attached = name.split(':', 1)
            key = ps_resolve_param(name.lower(), spec)
            role = spec['files'].get(key)
            if role:
                # -Path and -LiteralPath are alternates for one slot, so
                # binding either has to close it.
                bound.update(k for k, v in spec['files'].items() if v == role)
                if attached is not None:
                    named.append((attached, exp, quoted, role))
                elif i + 1 < n:
                    _, nval, nexp, nquoted = args[i + 1]
                    named.append((nval, nexp, nquoted, role))
                    i += 1
            else:
                bound.add(key)
                if key in spec['consume'] and attached is None:
                    i += 1
            i += 1
            continue
        positionals.append((val, exp, quoted))
        i += 1

    slots, pos = spec['positional'], 0
    for val, exp, quoted in positionals:
        while pos < len(slots) and slots[pos] in bound:
            pos += 1
        # Operands past the last slot repeat it — that is what makes -Path's
        # array binding (`Remove-Item a b c`) come out right.
        slot = slots[pos] if pos < len(slots) else (slots[-1] if slots else None)
        pos += 1
        role = spec['files'].get(slot)
        if role:
            named.append((val, exp, quoted, role))
    return named


def ps_path_parts(tok, quoted):
    """Split an unquoted comma-joined array operand (`-Path a,b`) into paths.

    Left whole when quoted, so a filename that genuinely contains a comma keeps
    its name. Splitting an unquoted one is the safe direction: `-Path a,C:\\out`
    checked as a single token would resolve cwd-relative and allow.
    """
    if not quoted and ',' in tok:
        return [p for p in tok.split(',') if p]
    return [tok]


def ps_apply_location(words, cwd, cwd_unknown):
    """Follow a Set-Location/Push-Location so later relative operands resolve
    against the right directory. Anything the hook can't follow — a bare `cd`,
    `cd -`, a `$var` target, `Pop-Location` — drops tracking, which turns later
    relative operands into 'untracked' offenders rather than silent allows."""
    name = PS_ALIASES.get(words[0][1].lower(), words[0][1].lower())
    if name == 'pop-location':
        return cwd, True
    targets = [b for b in ps_bind_args(words[1:], PS_LOCATION_SPEC)
               if b[3] == 'read']
    if len(targets) != 1:
        return cwd, True
    tok, exp, _, _ = targets[0]
    if exp or tok in ('-', '+'):
        return cwd, True
    p = ps_expand_tilde(tok)
    if p.startswith('~'):
        return cwd, True
    if not ps_is_absolute(p) and cwd_unknown:
        return cwd, True
    return ps_realpath(p, cwd), False


def ps_strip_head(words):
    """Drop an assignment prefix or the dot-source operator, so the command word
    is at index 0 — `$out = Get-Content …` heads on Get-Content."""
    if words and words[0][1].startswith('$'):
        if len(words) > 1 and words[1][1] == '=':
            words = words[2:]
        elif words[0][1].endswith('='):
            words = words[1:]
    if words and words[0][1] == '.':        # dot-source operator
        words = words[1:]
    return words


def ps_pid_list(tok):
    """True when `tok` is a literal pid or a comma-joined list of them."""
    return all(p.isdigit() for p in tok.split(','))


def ps_classify_kill(words):
    """Classify a `Stop-Process` segment, or None when it isn't one.

    Returns `(mode, selectors)` with mode 'pid' (every selector is a literal
    process id — nothing to guard), 'name' (`-Name` was used, which no anchor can
    rescue), or 'other' (anchorable by the rest of the statement).

    An `-Id` value carrying a `$` is NOT the pid case: after `$p = Get-Process
    -Name node`, `Stop-Process -Id $p.Id` is host-wide and the hook can't see the
    difference. Only literal digits count.
    """
    if not words:
        return None
    name = PS_ALIASES.get(words[0][1].lower(), words[0][1].lower())
    if name not in PS_KILL_CMDS:
        return None
    kinds, selectors = set(), []

    def take(key, val, exp):
        if key == 'id' and not exp and ps_pid_list(val):
            kinds.add('pid')
            return
        kinds.add('name' if key == 'name' else 'other')
        selectors.append(val)

    i, n, verbatim = 1, len(words), False
    while i < n:
        _, val, exp, _ = words[i]
        if val == '--%':
            # Stop-parsing: the tail goes to the target verbatim, so the hook
            # reads it as selection it cannot vouch for rather than as flags.
            verbatim = True
            i += 1
            continue
        if not verbatim and len(val) > 1 and val[0] == '-' and not exp \
                and not val[1].isdigit() and val[1] != '.':
            pname, attached = val[1:], None
            if ':' in pname:              # `-Name:node` binds without a space
                pname, attached = pname.split(':', 1)
            key = ps_resolve_param(pname.lower(), PS_KILL_SPEC)
            if key in PS_KILL_SELECTORS:
                if attached is not None:
                    take(key, attached, exp)
                elif i + 1 < n:
                    take(key, words[i + 1][1], words[i + 1][2])
                    i += 1
            elif key in PS_KILL_SPEC['consume'] and attached is None:
                i += 1                    # an ordinary value-taking parameter
            i += 1
            continue
        take('id', val, exp)              # positional 0 is -Id, else -InputObject
        i += 1

    if 'name' in kinds:
        return 'name', selectors
    if 'other' in kinds or not kinds:     # no selector at all -> pipeline input
        return 'other', selectors
    return 'pid', selectors


def ps_kill_operand_anchored(tok, expandable, anchor):
    """True when statement token `tok` pins a `Stop-Process` to this workspace.

    Same anchor as the bash side — the project root's directory name as a whole
    path component — with PowerShell's resolution rules in front of it. The
    tokenizer's `expandable` flag stands in for `EXPANSION_RE`: `ps_subexpressions`
    has already reduced a `$(…)` to a bare `$`, and PowerShell decides at runtime
    where `$env:USERPROFILE\\repo\\bin` lands, so the `\\repo\\` in it proves
    nothing.
    """
    if anchor is None or expandable:
        return False
    p = ps_expand_tilde(tok)
    if p.startswith('~'):
        return False
    return anchor.search(p) is not None


def ps_statement_kills(segments, ctx):
    """Offenders for the unanchored `Stop-Process` calls in one statement, and
    whether the statement signalled a process at all.

    `segments` is the statement's pipeline segments, each a token list. The
    anchor is looked for across all of them because the anchored rewrite is a
    pipeline (see PS_STATEMENT_OPS). A `-Name` kill is exempt from that scan —
    no amount of surrounding text scopes a bare process name to this workspace.

    The signal flag counts a kill that earns no offender — one by literal pid,
    or one the anchor cleared — because suppressing the caller's blanket `allow`
    is exactly what those cases need (Q59).

    A `taskkill` segment is judged here too, but on its OWN tokens: it reads no
    pipeline, so an anchor upstream of it selects nothing it kills. It shares
    this function only so one place answers "did this statement signal" (Q58).
    """
    kills, signal, offenders = [], False, []
    for seg in segments:
        words = ps_strip_head([t for t in seg if t[0] == 'word'])
        tk = classify_taskkill([w[1] for w in words])
        if tk is not None:
            signal = True
            offenders.extend(ps_taskkill_offenders(words, tk, ctx))
            continue
        kl = ps_classify_kill(words)
        if kl is None:
            continue
        signal = True
        if kl[0] != 'pid':
            kills.append(kl)
    if not kills:
        return offenders, signal
    anchored = any(ps_kill_operand_anchored(t[1], t[2], ctx.kill_anchor)
                   for seg in segments for t in seg if t[0] == 'word')
    offenders += [('Stop-Process', 'kill',
                   {'cmd': 'Stop-Process', 'root': ctx.proj, 'shell': 'powershell',
                    'pattern': ' '.join(selectors) or None})
                  for mode, selectors in kills if mode == 'name' or not anchored]
    return offenders, signal


def ps_taskkill_offenders(words, tk, ctx):
    """Offenders for a `taskkill` segment already classified as `tk`.

    The bash frontend runs the same rule over the same operands; only the
    resolution differs, so the anchor check is PowerShell's and the verdict is
    identical for a given command.
    """
    mode, idx = tk
    if mode == 'pid' or any(
            ps_kill_operand_anchored(words[i][1], words[i][2], ctx.kill_anchor)
            for i in idx):
        return []
    return [(words[0][1], 'kill',
             {'cmd': 'taskkill', 'root': ctx.proj, 'shell': 'powershell',
              'pattern': ' '.join(words[i][1] for i in idx) or None})]


def ps_analyze_segment(tokens, ctx, cwd, cwd_unknown):
    """Analyze one pipeline segment. Returns `(offenders, guarded, cwd,
    cwd_unknown)` — the trailing pair carries location tracking to the next
    segment in the chain."""
    files, words, i, n = [], [], 0, len(tokens)
    while i < n:
        kind = tokens[i][0]
        if kind == 'redir':
            # A redirect target is a shell-level write, honored whatever the
            # command word is — same as the bash side (Q26).
            if i + 1 < n and tokens[i + 1][0] == 'word':
                _, val, exp, quoted = tokens[i + 1]
                files.append((val, exp, quoted, 'write'))
                i += 2
                continue
            i += 1
            continue
        words.append(tokens[i])
        i += 1

    words = ps_strip_head(words)
    guarded = False
    if words:
        name = PS_ALIASES.get(words[0][1].lower(), words[0][1].lower())
        if name in PS_LOCATION_CMDS:
            cwd, cwd_unknown = ps_apply_location(words, cwd, cwd_unknown)
        else:
            spec = PS_SPEC.get(name)
            if spec is not None:
                guarded = True
                files.extend(ps_bind_args(words[1:], spec))

    offenders = []
    for tok, exp, quoted, role in files:
        if exp:
            offenders.append((tok, 'expand', None))
            continue
        for part in ps_path_parts(tok, quoted):
            p = ps_expand_tilde(part)
            if p.startswith('~'):
                offenders.append((part, 'expand', None))
                continue
            if not ps_is_absolute(p) and cwd_unknown:
                offenders.append((part, 'untracked', None))
                continue
            rp = ps_realpath(p, cwd)
            res = classify_outside(rp, ctx, is_read=(role == 'read'))
            if res is not None:
                disp = part if ps_is_absolute(part) \
                    else offender_display(part, rp)
                offenders.append((disp, res[0], res[1]))
    return offenders, guarded, cwd, cwd_unknown


def ps_analyze_command(cmd, ctx, base_cwd, depth=0):
    """Analyze a PowerShell command string. Returns `(offenders, guarded)`,
    matching `analyze_command`'s contract so the two frontends share the
    emit logic below.

    `guarded` is cleared when anything in the string signals a process, for the
    reason the bash side clears it: `allow` speaks for the WHOLE string and
    short-circuits the user's own permission settings, so a clean `Get-Content`
    must never speak for a `Stop-Process` sharing the string with it — including
    the kills the decision layer had no cause to deny, by literal pid or
    anchored. Those defer instead, which is the posture an anchored kill on its
    own already gets. (Q59)
    """
    offenders, guarded, signal = _ps_analyze_command(cmd, ctx, base_cwd, depth)
    return offenders, guarded and not signal


def _ps_analyze_command(cmd, ctx, base_cwd, depth=0):
    """Analyze one PowerShell string; returns `(offenders, guarded, signal)`."""
    if not cmd.strip():
        return [], False, False
    expandable_text = ps_strip_here_strings(cmd, literal_only=True)
    stripped = ps_strip_here_strings(cmd)
    if expandable_text is None or stripped is None:
        return [], False, False               # open here-string -> defer
    bodies = ps_subexpressions(expandable_text)[1]
    toks = ps_tokenize(ps_subexpressions(stripped)[0])
    if toks is None:
        return [], False, False               # open quote -> defer

    offenders, guarded, signal = [], False, False
    cwd, cwd_unknown, seg, stmt = base_cwd, False, [], []
    for tok in toks + [('op', ';', False, False)]:
        if tok[0] == 'op':
            if seg:
                off, g, cwd, cwd_unknown = ps_analyze_segment(
                    seg, ctx, cwd, cwd_unknown)
                offenders.extend(off)
                guarded = guarded or g
                stmt.append(seg)
            seg = []
            # A kill never sets `guarded` itself, and clears anyone else's: an
            # anchored one defers rather than emitting `allow`, leaving the
            # user's own permission settings to have their say on a destructive
            # command.
            if tok[1] in PS_STATEMENT_OPS:
                off, sig = ps_statement_kills(stmt, ctx)
                offenders.extend(off)
                signal = signal or sig
                stmt = []
        else:
            seg.append(tok)

    # Subexpression bodies contribute offenders only — a clean guarded cmdlet
    # inside one never flips a deferring outer command into an `allow`. Their
    # signal DOES fold up, because `ps_subexpressions` masks the body out of the
    # outer text entirely: in `Get-Content .\in.txt; $(Stop-Process -Id 1234)`
    # the two halves sit on opposite sides of this recursion, and neither is an
    # offender on its own.
    if depth < MAX_SUBST_DEPTH:
        for body in bodies:
            sub_off, _, sub_sig = _ps_analyze_command(body, ctx, base_cwd,
                                                      depth + 1)
            offenders.extend(sub_off)
            signal = signal or sub_sig
    return offenders, guarded, signal


def handle_powershell(data):
    """Guard the PowerShell shell tool.

    A missing command field is NOT treated as ordinary uncertainty. The field
    name comes from the installed binary rather than from documented schema
    (see docs/plan/q51-powershell-tool.md), so deferring on its absence would be
    indistinguishable from the wiring bug it would be hiding — a guard that
    reports itself active and enforces nothing, the exact failure
    run-python-hook.cmd exists to prevent. That case asks, and says why.
    """
    ti = data.get('tool_input')
    if not isinstance(ti, dict) or not isinstance(ti.get('command'), str):
        emit("ask", "workspace-guard could not read the PowerShell tool's "
                    "command (tool_input.command), so it checked nothing about "
                    "this command's file access. Approve only if you have read "
                    "the command yourself, and please report this — the guard "
                    "is meant to check every shell command.")
        return
    cmd = ti['command']
    if not cmd.strip():
        return
    ctx = build_context(data)
    outside, guarded = ps_analyze_command(cmd, ctx, ctx.cwd)
    if not outside and not guarded:
        return
    if outside:
        bypass = data.get("permission_mode") == "bypassPermissions"
        decision, reason = decide(outside, ctx, bypass)
    else:
        decision, reason = "allow", "Guarded cmdlets target workspace only"
    emit(decision, reason)


def main():
    data = json.load(sys.stdin)
    tool = data.get('tool_name') or ''
    # PowerShell is checked FIRST and never falls through. The default branch is
    # Bash handling, and routing a PowerShell command into the POSIX tokenizer
    # is the silent-allow the section above exists to avoid.
    if tool == 'PowerShell':
        handle_powershell(data)
    elif tool in ('Edit', 'Write', 'MultiEdit', 'NotebookEdit'):
        handle_edit(data)
    elif tool in ('Read', 'Grep', 'Glob'):
        handle_read_tool(data)
    else:
        # Absent tool_name (older CLIs) -> Bash handling, preserving the
        # original behavior.
        handle_bash(data)


if __name__ == "__main__":
    main()
