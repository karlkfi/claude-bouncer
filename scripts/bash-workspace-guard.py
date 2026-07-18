#!/usr/bin/env python3
"""PreToolUse hook: prompt (ask) when a guarded command targets a file
outside the workspace; allow when it only touches workspace files or pipes.

Reads the hook JSON on stdin, emits a PreToolUse decision on stdout.
"""
import sys, os, json, re, shlex, fnmatch, collections

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
# split by an exotic inherited IFS into pieces the single-token check misses).
IMPURE_VALUE_CHARS = frozenset(' \t\n$`*?[:')

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
# is not exempt from the workspace check.
WRITE_COMMANDS = frozenset({'cp', 'mv', 'tee', 'rm'})

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


def claude_projects_dir():
    """Realpath of Claude Code's per-user project-data dir, ``~/.claude/projects/``.

    Claude Code writes session and sub-agent data (workflow journals, task
    output indices, etc.) under this directory. Reading these files back is
    not the boundary this hook guards: the data is written by the harness
    itself, not by external inputs. Returns None if $HOME is unset or the
    path cannot be resolved.
    """
    home = os.environ.get('HOME')
    if not home:
        return None
    try:
        return os.path.realpath(os.path.join(home, '.claude', 'projects'))
    except OSError:
        return None


def allowed_read_prefixes():
    """Resolved list of absolute path prefixes exempt from the workspace check
    for **read-only** guarded commands (see WRITE_COMMANDS for exclusions).

    Default: Claude Code's per-user project-data dir (~/.claude/projects/).
    Additive extension via WORKSPACE_GUARD_READ_ALLOW_PREFIXES (colon- or
    comma-separated). Each entry is run through realpath so platform symlinks
    (e.g. /tmp -> /private/tmp on macOS) resolve correctly. Entries that
    cannot be resolved are skipped (fail-open on config, fail-safe on the
    boundary: a bad entry just loses its exemption).
    """
    defaults = []
    cpd = claude_projects_dir()
    if cpd:
        defaults.append(cpd)
    extras = _split_pathlist(os.environ.get('WORKSPACE_GUARD_READ_ALLOW_PREFIXES', ''))
    out = []
    for p in defaults + extras:
        try:
            out.append(os.path.realpath(p))
        except OSError:
            continue
    return out


def claude_tmp_root():
    """Realpath of Claude Code's per-user temp root, ``/tmp/claude-<uid>``.

    Claude Code stores each session's background-task output under
    ``<root>/<encoded-project>/<session-uuid>/tasks/<id>.output`` (mode 0700,
    per-UID). This layout is an undocumented internal convention — there is no
    hook field that names it — so we infer it from ``os.getuid()``. If Claude
    Code ever relocates the dir, paths simply stop matching the allow below and
    revert to ``ask`` (fail-safe), so inferring it never weakens the boundary.
    """
    return os.path.realpath('/tmp/claude-%d' % os.getuid())


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
# outside-workspace `ask`. The list is extensible; see host_temp_roots().
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


def host_temp_roots():
    """Resolved set of host-temp roots: the defaults, any extra roots from
    ``WORKSPACE_GUARD_TMP_ROOTS`` (additive — never replaces the defaults, so the
    boundary can't be weakened by clearing it), and ``$TMPDIR`` if set.

    Each root is run through ``realpath`` so a path under macOS's
    ``/tmp -> /private/tmp`` symlink or a ``$TMPDIR`` under ``/var/folders/...``
    is matched after the file argument is itself resolved. A root that can't be
    resolved is skipped (fail-open)."""
    raw = list(HOST_TEMP_DEFAULT_ROOTS)
    raw += _split_pathlist(os.environ.get('WORKSPACE_GUARD_TMP_ROOTS', ''))
    tmpdir = os.environ.get('TMPDIR')
    if tmpdir:
        raw.append(tmpdir)
    out = set()
    for r in raw:
        if not r:
            continue
        try:
            out.add(os.path.realpath(r))
        except OSError:
            continue
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


def matches_allowlist(rp, patterns):
    """True when resolved path ``rp`` matches an allowlist entry. An entry with
    glob metacharacters is matched with ``fnmatch``; otherwise it's an exact or
    directory-prefix match, resolved with ``realpath`` first so a configured
    ``/tmp/ok`` matches the realpath ``/private/tmp/ok`` on macOS.

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
            if any(fnmatch.fnmatch(c, p) for c in cands):
                return True
            continue
        try:
            rp_pat = os.path.realpath(p)
        except OSError:
            rp_pat = p
        base = rp_pat.rstrip(os.sep)
        if any(c == rp_pat or path_at_or_under(c, base) for c in cands):
            return True
    return False


def scratch_dir_name():
    """Repo-local scratch dir named in the deny message (default ``tmp/``), from
    ``WORKSPACE_GUARD_SCRATCH_DIR``."""
    return (os.environ.get('WORKSPACE_GUARD_SCRATCH_DIR') or 'tmp/').strip() or 'tmp/'


def build_scratch_hint(proj, scratch):
    """One-line guidance steering off host temp toward a repo-local scratch dir.

    Names the dir concretely when it already exists under the project root
    (an ``os.path.isdir`` stat — no file contents are read), otherwise tells the
    user to create and gitignore it. Closes with the two config knobs."""
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


def sibling_override():
    """Reason string from ``WORKSPACE_GUARD_OVERRIDE`` (downgrades the sibling
    deny to ``ask`` for deliberate cross-checkout work), or None when unset."""
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
           'uniq':'cat','xxd':'cat','od':'cat',
           'strings':'cat','cmp':'cat',
           'zcat':'cat','gzcat':'cat','bzcat':'cat','xzcat':'cat'}


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


def strip_heredoc_bodies(tokens):
    """Drop heredoc body tokens so body text is never parsed as commands.

    Bash slurps everything between the newline after `<<TAG` and a line that
    is exactly TAG as literal stdin data. shlex has no heredoc concept, so
    those body lines otherwise tokenize as command groups: HTML like
    `</div>` becomes a `<` redirect with target `/div` (flagged as an
    outside-workspace path) and script text becomes phantom file args (Q15).

    On `<<`, the following word token is recorded as a pending delimiter —
    `<<-EOF` and `<< EOF` both arrive as `<<` + word, so a leading `-` on the
    word is accepted as the tab-strip modifier when matching the terminator.
    The rest of the command line is kept (a trailing `> out.txt` redirect
    still parses); at the next newline the body starts and tokens are dropped
    until a line consisting of exactly the delimiter. Multiple pending
    heredocs consume consecutive bodies in order. An unterminated body
    swallows to end-of-input, matching bash. `<<<` here-strings arrive as a
    distinct operator token and never match here. A `<<` from unquoted
    arithmetic (`$((x<<2))`) can arm a bogus delimiter; if a newline follows,
    later tokens may be skipped and the hook defers — fail-safe, never a
    silent allow of a checked path.
    """
    out, pending = [], []
    i, n = 0, len(tokens)
    while i < n:
        t = tokens[i]
        if t == '<<' and i + 1 < n \
                and not all(c in PUNCT_CHARS for c in tokens[i+1]):
            out.append(t); out.append(tokens[i+1])
            pending.append(tokens[i+1])
            i += 2
            continue
        out.append(t)
        i += 1
        if t == '\n' and pending:
            while pending and i < n:
                delim = pending.pop(0)
                cands = {delim, delim[1:]} if delim.startswith('-') else {delim}
                line_start = True
                while i < n:
                    tok = tokens[i]
                    if line_start and tok in cands \
                            and (i + 1 >= n or tokens[i+1] == '\n'):
                        i += 1                    # drop the terminator line
                        break
                    line_start = tok == '\n'
                    i += 1
    return out


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


def literal_assignment_value(raw):
    """Return the literal an assignment RHS resolves to, or None if bash
    might expand or word-split it into something the hook can't predict.

    ``raw`` is post-shlex (quotes removed). A leading ``~``/``~/…`` is
    expanded like bash expands it in assignments; ``~user``/unset-``$HOME``
    stay unresolvable. An empty value is rejected because ``f=(a b)``
    tokenizes as ``f=`` + a paren run — treating it as the scalar empty
    string would miss the array's real ``$f`` (its first element).
    """
    if not raw:
        return None
    if raw == '~' or raw.startswith('~/'):
        raw = expand_tilde(raw)
    if raw.startswith('~'):
        return None
    if any(c in IMPURE_VALUE_CHARS for c in raw):
        return None
    return raw


def literal_for_item(raw):
    """Return the literal a `for VAR in <list>` item resolves to, or None when
    bash would expand it into paths the hook can't predict (issue 70).

    Reuses the assignment-RHS purity test (``literal_assignment_value``) — same
    tilde handling, same rejection of ``$``/backtick/glob/whitespace — then
    ADDITIONALLY rejects brace items (``{a,b}``, ``a{1..3}``): unlike an
    assignment RHS, a for-list item IS brace-expanded by bash, so treating
    ``{a,b}`` as the literal string would miss the real ``a``/``b`` paths. A
    rejected item poisons the loop variable (today's runtime-expanded ``ask``).
    """
    val = literal_assignment_value(raw)
    if val is None:
        return None
    if '{' in val or '}' in val:
        return None
    return val


def for_loop_binding(g):
    """Classify a command group as a `for NAME in <list>; …` loop header.

    Returns:
      * ``(name, [values])`` — a fully-literal list: record the candidate set so
        ``$NAME`` in a later file arg is validated against every value (issue 70).
      * ``(name, None)`` — a `for NAME in` whose list has any non-literal/glob/
        brace item, an empty list, or a `for NAME` with no `in` (iterates
        ``"$@"``): the caller drops NAME from the maps, keeping today's poison.
      * ``None`` — not a `for NAME in` header at all (the `for ((…))` arithmetic
        form tokenizes with ``(`` at ``g[1]``), so the caller's existing
        ``poison_vars`` path runs unchanged.

    Must be called on the post-substitution tokens: a list item like ``$SP/a``
    with ``SP`` a known literal has already been resolved, so the bound value
    matches what bash iterates.
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
        val = literal_for_item(it)
        if val is None:
            return (name, None)                   # non-literal item -> poison
        values.append(val)
    return (name, values)


def expand_loop_candidates(tok, loopmap):
    """Expand every `$NAME`/`${NAME}` whose NAME is a for-loop variable into the
    full set of concrete tokens bash iterates over (issue 70).

    ``loopmap`` maps a loop variable to its literal candidate list (recorded by
    ``for_loop_binding`` from a fully-literal ``for NAME in …``). A token using
    such a variable stands for one path per candidate — and bash visits ALL of
    them — so the caller checks every expansion and prompts if ANY lands outside
    the workspace. Since the candidate set is exactly bash's iteration list, an
    outside path can never slip past; a stale/over-broad set only ever adds
    candidates, which can prompt but never wrongly allow.

    Returns ``[tok]`` unchanged when the token uses no loop variable (or holds a
    backtick the hook won't evaluate). Several distinct loop variables in one
    token expand as the cross product; order is deterministic (variable order,
    then candidate order) so reasons and tests stay stable.
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
    i = 0
    while i < len(g) and g[i] in SH_KEYWORDS:
        i += 1
    rest = g[i:]
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
    """Expand a leading `~` or `~/…` to `$HOME` (bash does this deterministically).

    Returns the expanded absolute path, or the token unchanged when it can't be
    resolved here: a `~user`/`~+`/`~-` prefix (no plain `~` or `~/`) or an unset
    `$HOME`. Callers still defer on a returned token that begins with `~` or
    contains an expanding `$` (see EXPANSION_RE), so only the deterministic,
    fully-resolvable cases are expanded
    — `~user`'s pwd lookup and `~+`/`~-`'s dir-stack state stay out of scope.
    """
    if tok == '~' or tok.startswith('~/'):
        home = os.environ.get('HOME')
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
        arg = expand_tilde(t)                     # `cd ~/proj` tracks via $HOME
        if arg.startswith('+') or arg.startswith('~') or '$' in arg:
            return ('unknown', None)
        return ('arg', arg)
    return ('unknown', None)                      # bare `cd` -> $HOME


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


def build_reason(offenders, scratch_hint='', override=None):
    """Build the permissionDecisionReason for a blocked command.

    `offenders` is a list of `(token, category[, detail])` items from
    `check_file` / `handle_edit`. The message names the offending token(s) AND
    tells the agent how to avoid the prompt, tailored per category:

      * 'sibling'   — a WRITE into a sibling checkout of the same repo (primary
                      checkout or another worktree). `detail` carries the
                      checkout root, branch, and corrected in-session path.
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
    siblings = []
    for item in offenders:
        tok, cat = item[0], item[1]
        detail = item[2] if len(item) > 2 else None
        if cat == 'sibling':
            siblings.append((tok, detail or {}))
        else:
            buckets[cat].append(tok)

    hints = []
    if siblings:
        hints.append(build_sibling_hint(siblings, override))
    if buckets['hosttemp']:
        hints.append(
            "Host-wide temp path(s): "
            + ", ".join(sorted(set(buckets['hosttemp'])))
            + ". " + scratch_hint)
    if buckets['outside']:
        hints.append(
            "Outside-workspace path(s): "
            + ", ".join(sorted(set(buckets['outside'])))
            + ". Fix: use a path inside the project root, or read the file "
            "with the Read/Grep/Glob tools instead of bash. If you genuinely "
            "need a file outside the root, approve this prompt.")
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
            + ". Fix: avoid cd outside the root and bare cd / cd - / cd $HOME; "
            "pass an in-root path or use the Read/Grep tools.")
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
    'sib_override'])


def build_context(data):
    """Resolve the shared per-invocation context from the hook payload.

    Fields (all resolved once so the handlers can't drift):
      * ``proj`` / ``cwd`` — project root and the tool's working directory.
      * ``session_id`` — this session's UUID; scopes the Claude-managed-temp
        allow to THIS session's own task output (empty on older CLIs -> allow off).
      * ``session_tmp_root`` — ``/tmp/claude-<uid>`` realpath.
      * ``session_proj_dir`` — ``<tmp_root>/<slug>`` holding this session, for the
        sibling-session read exemption (#61); None when not locatable.
      * ``tmp_roots`` / ``tmp_allow`` / ``tmp_action`` — host-temp config.
      * ``read_prefixes`` — prefixes always allowed for READS (never writes).
      * ``session_wt`` — the session's own checkout, for the sibling-checkout
        deny; a no-op unless the session is itself a linked worktree.
      * ``sib_override`` — WORKSPACE_GUARD_OVERRIDE reason, or None.
    """
    cwd = data.get('cwd') or os.getcwd()
    proj = os.path.realpath(os.environ.get('CLAUDE_PROJECT_DIR') or cwd)
    session_id = data.get('session_id') or ''
    session_tmp_root = claude_tmp_root()
    return Ctx(
        proj=proj, cwd=cwd, session_id=session_id,
        session_tmp_root=session_tmp_root,
        session_proj_dir=claude_session_project_dir(session_id, session_tmp_root),
        tmp_roots=host_temp_roots(),
        tmp_allow=host_temp_allowlist(),
        tmp_action=host_temp_action(),
        read_prefixes=allowed_read_prefixes(),
        session_wt=resolve_session_worktree(proj),
        sib_override=sibling_override())


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
        if matches_allowlist(rp, ctx.tmp_allow):
            return None
        return ('hosttemp', None)
    return ('outside', None)


def decide(offenders, ctx, bypass):
    """Map a non-empty ``offenders`` list to a ``(decision, reason)`` pair.

    Shared final step for every handler. ``deny`` when running under
    ``bypassPermissions`` (no human to answer an ask), when a host-temp path is
    hit and the configured action is ``deny``, or when a sibling-checkout write
    is hit without an override; otherwise ``ask``. Both decisions block equally —
    this is a recoverability/steering choice, not a weakening of the boundary."""
    host_temp_hit = any(cat == 'hosttemp' for _, cat, _ in offenders)
    sibling_hit = any(cat == 'sibling' for _, cat, _ in offenders)
    sibling_deny = sibling_hit and ctx.sib_override is None
    deny_now = bypass or (host_temp_hit and ctx.tmp_action == 'deny') \
        or sibling_deny
    decision = "deny" if deny_now else "ask"
    reason = build_reason(offenders,
                          build_scratch_hint(ctx.proj, scratch_dir_name()),
                          override=ctx.sib_override)
    return decision, reason


def resolve_native_path(raw, cwd):
    """Resolve a native tool's path field to a realpath, or None to defer.

    Native tools pass literal paths (no shell expansion), so beyond the
    deterministic ``~``/``~/…`` that ``expand_tilde`` handles, a leftover ``~``
    or any ``$`` is treated as unresolvable and the caller defers to builtin
    permissions — the posture the edit handler has used since the sibling deny."""
    if not raw or not isinstance(raw, str):
        return None
    p = expand_tilde(raw)
    if p.startswith('~') or '$' in p:
        return None
    return os.path.realpath(p if os.path.isabs(p) else os.path.join(cwd, p))


def handle_bash(data):
    cmd = (data.get('tool_input') or {}).get('command', '') or ''
    ctx = build_context(data)
    proj, cwd = ctx.proj, ctx.cwd
    if not cmd.strip():
        return

    try:
        # `\n` is a punctuation char so a newline command boundary surfaces as
        # a token (it is otherwise eaten as whitespace, merging the commands on
        # either side). Removing it from `whitespace` stops shlex re-swallowing
        # it; quoted newlines stay inside their word token regardless. The runs
        # this produces (`;\n`, `|\n`, ...) are split back apart below.
        # Comments are stripped up front (see strip_comments) and shlex's own
        # comment handling is disabled — it would swallow the newline after a
        # comment and merge the next line into the commented command's group.
        lex = shlex.shlex(strip_comments(cmd), posix=True,
                          punctuation_chars=';()<>|&\n')
        lex.whitespace_split = True
        lex.whitespace = lex.whitespace.replace('\n', '')
        lex.commenters = ''
        tokens = glue_dollar_paren(
            strip_heredoc_bodies(split_operator_runs(list(lex))))
    except ValueError:
        return                                    # unbalanced quotes -> defer

    # Each group is a `(cmd_tokens, redir_targets, persists)` triple: a
    # redirect target is collected into the group it textually appears in, so
    # it later resolves against THAT group's cwd rather than the chain's
    # original cwd — this is what lets `cd /tmp && cat /dev/null > evil` flag
    # `/tmp/evil` (Q16). `persists` is True only when a variable assignment in
    # the group survives into later commands of the same string: at paren
    # depth 0 (not a subshell — `(f=x); cat $f` doesn't set f), not a pipeline
    # segment (each side of `|` runs in a subshell), and not backgrounded
    # (`f=x & …` assigns in the background copy only).
    groups, cur, cur_redir, i = [], [], [], 0
    depth, prev_sep = 0, ''
    while i < len(tokens):
        t = tokens[i]
        if t in SEPARATORS:
            if cur or cur_redir:
                persists = (depth == 0 and prev_sep != '|'
                            and t in (';', '\n', '&&', '||'))
                groups.append((cur, cur_redir, persists))
                cur, cur_redir = [], []
            if t == '(':
                depth += 1
            elif t == ')':
                depth = max(0, depth - 1)
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
        groups.append((cur, cur_redir, depth == 0 and prev_sep != '|'))

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
        # Bash expands `~`/`~/…` to $HOME deterministically — resolve it here so
        # an in-workspace home path isn't needlessly flagged. `~user`/`~+`/`~-`,
        # an unset $HOME, and any `$VAR`/`$(...)` stay 'expand' (unresolvable).
        # A `$` bash keeps literal (trailing, or before e.g. `.`/`/` — see
        # EXPANSION_RE) is part of the filename and falls through to realpath.
        f = expand_tilde(f)
        if f.startswith('~') or EXPANSION_RE.search(f):
            return ('expand', None)
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
        single candidate — the original single-path check."""
        for cand in expand_loop_candidates(f, loopmap):
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
                return (cand, 'outside', None)
            # Everything past resolution — the session-tmp allow, read-prefix
            # and sibling-session exemptions, sibling-checkout / host-temp /
            # outside tiers — is the shared core (classify_outside), so a `cat`
            # and a native `Read` of the same path can't diverge. The ln-staging
            # defense above runs FIRST so a link planted inside an allowed
            # scratch dir can't launder an outside target.
            res = classify_outside(rp, ctx, is_read)
            if res is not None:
                return (cand, res[0], res[1])
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
    for g, g_redir, persists in groups:
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
        if propagate:
            assigned = apply_assignment_group(g, varmap, persists)
            if assigned is not None:
                # A name set as a scalar literal is no longer a loop variable.
                for nm in assigned:
                    loopmap.pop(nm, None)
                if 'IFS' in assigned:
                    # A changed IFS alters how bash word-splits every later
                    # expansion — stop propagating for the rest of the string.
                    varmap.clear()
                    loopmap.clear()
                    propagate = False
                continue                          # assignment-only group
            forbind = for_loop_binding(sub_g)
            if forbind is not None:
                name, values = forbind
                varmap.pop(name, None)            # a loop var isn't a scalar
                if values is None:
                    loopmap.pop(name, None)       # unresolvable list -> poison
                else:
                    loopmap[name] = values
                continue                          # for-header: nothing to check
            poison_vars(sub_g, varmap)
            poison_vars(sub_g, loopmap)           # same rules invalidate loops
        g = strip_env_prefix(sub_g)
        if not g: continue                        # env-only / redirect-only group
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
        fs = files_in_command(g)
        if fs is None: continue
        guarded = True
        cmd_name = ALIASES.get(os.path.basename(g[0]), os.path.basename(g[0]))
        is_read = cmd_name not in WRITE_COMMANDS
        for f in fs:
            o = check_file(f, group_cwd, group_cwd_unknown, is_read=is_read)
            if o is not None:
                outside.append(o)
    if not guarded:
        return                                    # no guarded command -> defer

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
        # A third deny driver: a WRITE into a sibling checkout of the same repo
        # (the 'sibling' category). It denies by default — self-heals in one
        # agent round trip — unless WORKSPACE_GUARD_OVERRIDE is set, which
        # downgrades it to `ask` for deliberate cross-checkout work.
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


def main():
    data = json.load(sys.stdin)
    tool = data.get('tool_name') or ''
    # Absent tool_name (older CLIs, or the Bash-only matcher) -> Bash handling,
    # preserving the original behavior.
    if tool in ('Edit', 'Write', 'MultiEdit', 'NotebookEdit'):
        handle_edit(data)
    elif tool in ('Read', 'Grep', 'Glob'):
        handle_read_tool(data)
    else:
        handle_bash(data)


if __name__ == "__main__":
    main()
