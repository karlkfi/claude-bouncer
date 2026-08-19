#!/usr/bin/env python3
"""PreToolUse hook: deny when a command whose exit status IS the answer loses it.

Reads the hook JSON on stdin, emits a PreToolUse decision on stdout. Three ways
a status goes missing, all of which turn a failure into a green:

  1. Piped into a filter. A pipeline reports its LAST stage's status, so
     `make check 2>&1 | tail -30` reports `tail`'s 0. zsh -- the shell the Bash
     tool runs -- has no `PIPESTATUS` to recover it.
  2. Backgrounded with something else running last. A `;`-list yields its last
     statement's status, so a backgrounded `cmd > log 2>&1; echo "EXIT=$?"`
     notifies `completed (exit code 0)` for a failed command.
  3. Sequenced before a state-changing command with `;`. The status is read
     correctly and then ignored: `make check; git push` pushes either way.

Every verdict is a `deny`, never an `ask`. A deny's reason is shown to the
model, so the fix lands where the command gets rewritten; an ask goes to the
user and the model never sees the hint. The break-glass is a command prefix,
`EXIT_STATUS_GUARD_OVERRIDE=<reason> <command>` -- an environment assignment, because
that is the only form a PreToolUse hook can see (it reads the command string,
and the session cannot set a variable in the hook's own environment).

Gate patterns are matched against the HEAD of a shell segment, after leading
`VAR=val` assignments and `bash`/`sudo`/`time`-style wrappers are peeled --
never against the raw command string. A raw-string match also fires on every
`git show`, `grep`, and commit message that merely NAMES the command.

The shell analysis here -- heredoc bodies, comments, quote state, command
substitution, operator runs -- is ported from claude-workspace-guard's
bash-workspace-guard.py rather than written fresh. Hand-rolling a shell-grammar
scanner is the documented way this class of tool fails: silently, in both
directions.
"""
import sys, os, json, re, shlex, collections

# --- Ported from claude-workspace-guard (scripts/bash-workspace-guard.py) ----
# These carry the quote-state tracking and bracket counting. Kept structurally
# identical to their source so a fix there transfers by inspection.

# POSIX command-prefix assignment: NAME starts with letter/underscore, then
# letters/digits/underscores, then `=`. Bash treats one or more of these at the
# start of a simple command as inline env exports; they do not change the
# command name lookup.
ASSIGNMENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')

# Reserved words that may prefix the real command in a segment (`while read f`,
# `time make check`, `! grep …`). Left in place they become tokens[0] and the
# registry lookup misses, so the gate is never recognised.
SH_KEYWORDS = frozenset({
    'while', 'until', 'if', 'then', 'elif', 'else', 'do', 'done', 'fi',
    'case', 'esac', 'in', 'time', 'function', '!', '{', '}', '[[', ']]',
})

# Command separators and redirect operators (after shlex punctuation grouping).
# `|&` is bash's pipe-stderr-too spelling and is a pipe for every purpose here;
# without it in the vocabulary the run splits into `|` + `&` and the `&` reads
# as a background fork.
SEPARATORS = {'|', '|&', '||', '&&', '&', ';', '\n', '(', ')'}
REDIR = {'>', '>>', '<', '<<', '<<<', '>|', '&>', '&>>'}
# fd-duplication operators: `2>&1`, `2>&-`, `0<&3`. The token after one is a
# duplication/close target (a bare fd or `-`), not a path.
DUP = {'>&', '<&'}

# Operators that chain commands within one statement, as opposed to ending it.
PIPE_OPS = {'|', '|&'}
CHAIN_OPS = PIPE_OPS | {'&&', '||'}
# Operators that end a statement, so the next segment's status is independent.
END_OPS = {';', '\n', '&'}

# Every char shlex treats as punctuation (see `punctuation_chars` below). A
# token built only from these is an operator run; anything else is a word, so a
# quoted filename containing one survives intact.
PUNCT_CHARS = frozenset(';()<>|&\n')

# Longest-match vocabulary for splitting glued operator runs. Built from the
# operator sets so it cannot drift from what the segment loop understands.
_OPERATORS = tuple(sorted(SEPARATORS | REDIR | DUP, key=len, reverse=True))

# Chars that may precede an unquoted `#` for it to start a comment: bash only
# comments at the start of a word. `$#`, `${#x}`, and `file#1` are not comments.
COMMENT_PRECEDERS = frozenset(' \t\n;|&()<>')

# Backstop on command-substitution nesting. Recursion already terminates (each
# body is a strictly shorter substring); beyond this, deeper bodies are not
# analyzed -- a possible missed deny, never a fabricated one.
MAX_SUBST_DEPTH = 25


def strip_comments(cmd):
    """Remove unquoted `#` comments and fold backslash-newline continuations.

    shlex's built-in comment handling swallows the comment AND its trailing
    newline, merging the next line into the commented line's segment; it also
    starts a comment at a mid-word `#` (`file#1`), which bash does not. So
    comments are stripped here with bash's actual rule and shlex's own comment
    processing is disabled. The newline that ends a comment is kept.

    A continuation is dropped outright, the way POSIX joins a continued line
    before tokenizing. `tokenize` makes a newline a command boundary, so one
    left in place splits an `&&` chain written across lines into two statements
    and the chain reads as a `;` sequence (#8). This branch is the one place
    this function diverges from workspace-guard's copy, which has no equivalent.
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
            if cmd[i+1] == '\n':                   # continuation -> one logical line
                i += 2
                continue
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
    """Skip a heredoc body starting at ``i`` up to and including its terminator.

    Body lines are compared RAW -- no quote or expansion parsing -- so an
    apostrophe, an unbalanced quote, or a `func(` in the body can never affect
    the scan. Returns the index just past the terminator's newline, or
    ``len(text)`` on an unterminated body (matching bash).
    """
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

    A body can hold anything -- apostrophes, `func(`, an odd number of quotes --
    none of it shell syntax. Left in place, shlex either mis-tokenizes it (body
    text becomes phantom commands) or, on an unbalanced quote, aborts the entire
    parse. This is what makes a piped gate quoted in a heredoc body text rather
    than a command, with no special case for it anywhere in the rules.

    Pass a list as ``expanded`` to also collect the bodies whose delimiter
    carries no quote and no backslash (`` <<EOF ``, not `` <<'EOF' ``). That is
    bash's own expansion rule -- an unquoted delimiter leaves `$(…)` and
    `${PIPESTATUS[0]}` live -- so the callers see exactly the bodies bash would
    evaluate. They come back separately rather than inline because a body is
    data: the apostrophe in a `don't` would otherwise open a quote for the rest
    of the scan.
    """
    out = []
    i, n = 0, len(cmd)
    in_single = in_double = False
    last = ''                                     # last emitted char (word start)
    pending = []                                  # (delim, strip_tabs, quoted)
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


def _skip_balanced_parens(text, start):
    """Step over a run of balanced parens beginning at ``start`` (a ``(``).

    Returns the index just past the matching close, or end-of-string on
    imbalance. Used to skip `$((…))` arithmetic, whose `<<` is a shift rather
    than a heredoc and which contains no command.
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
    """Scan a ``$(`` body from ``start`` to its matching ``)``.

    Returns ``(body, end)``, or ``(None, start)`` when no balanced terminator is
    found. Paren nesting, quotes, and backslash escapes inside the body are
    tracked so a ``)`` in a quoted string does not close early. Quote tracking
    is flat; on input where that mis-locates the close, the body handed to shlex
    is unbalanced and analysis defers for it -- fail-safe.
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
    """Scan a backtick body to the next unescaped backtick.

    Returns ``(body, end)``, or ``(None, start)`` when unterminated.
    """
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

    Returns the inner command string of each `$(…)` and backtick substitution in
    an UNQUOTED or DOUBLE-QUOTED context -- the two contexts where bash performs
    command substitution. One inside single quotes is a literal and is skipped;
    `$((…))` arithmetic holds no command and is skipped too.

    Scans the RAW string, never the post-shlex tokens: shlex strips the quotes,
    losing the single-vs-double distinction that decides whether a `$(…)` even
    substitutes. Only the outermost substitutions are returned -- the caller
    recurses to find nested ones.

    With ``quotes=False`` a quote character is ordinary text and every
    substitution is live. That is how bash reads an unquoted heredoc body, so
    the apostrophe in a `don't` must not switch the scanner off for the rest.
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
                break
            bodies.append(body)
            i = end
            continue
        i += 1
    return bodies


def glue_dollar_paren(tokens):
    """Re-attach a `(` to a preceding word ending in `$`.

    `(` is a punctuation char, so `$(cmd)` tokenizes as `$` + `(` + …. The `(`
    is kept in the stream so the segment loop still opens a nesting level for
    the substitution body, which is what lets a gate inside `$(…)` be found by
    the ordinary rules.
    """
    out = []
    for t in tokens:
        if t == '(' and out and out[-1].endswith('$'):
            out[-1] += '('
        out.append(t)
    return out


def split_operator_runs(tokens):
    """Split a glued operator-run token into its individual operators.

    shlex returns a run of adjacent operator characters as ONE token: `(cd x); …`
    tokenizes `);`, a newline boundary glues as `;\\n`. None of those compound
    runs match the operator vocabulary the segment loop keys on, so the command
    boundary is missed and two commands merge into one segment.

    Applied ONLY to pure operator runs; a quoted filename containing an operator
    char is a word token and is left intact. Each run is consumed greedily
    longest-first, so `&>>` wins over `&>` over `&`, and `|&` over `|`.
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
                # Unreachable while every punctuation char is itself an
                # operator. Kept as a total-function guard: emit the remainder
                # as one token rather than spin. A merged segment can only miss
                # a deny, never fabricate one.
                out.append(t[i:])
                break
    return out


def strip_env_prefix(tokens):
    """Drop leading POSIX `NAME=VALUE` command-prefix assignments.

    `GOFLAGS=-mod=mod go build ./…` tokenizes with the assignment at index 0;
    without stripping, the registry lookup misses and the gate goes unrecognised.
    """
    i = 0
    while i < len(tokens) and ASSIGNMENT_RE.match(tokens[i]):
        i += 1
    return tokens[i:]


def strip_sh_keywords(tokens):
    """Drop leading shell reserved words that may prefix the real command.

    Stripped BEFORE strip_env_prefix because bash's order in a simple command is
    reserved word(s), then inline assignments, then the command name
    (`time LC_ALL=C make check`).
    """
    i = 0
    while i < len(tokens) and tokens[i] in SH_KEYWORDS:
        i += 1
    return tokens[i:]


# --- Segmentation -----------------------------------------------------------
# Everything below is this guard's own, and works on the token stream the ported
# layer produces -- flat list bookkeeping, no raw-text scanning.

# Words that precede a real command without changing whose status is at stake.
WRAPPERS = frozenset({'sudo', 'nohup', 'command', 'exec', 'bash', 'sh', 'zsh',
                      'env', 'stdbuf', 'setsid'})

# A segment: the tokens of one simple command, the operator run that follows it,
# and its paren-nesting depth. `post_ops` is a tuple rather than a single token
# because a group can close between a command and the operator that decides its
# fate: in `(cd x && go test ./...) | tail` the ops after `go test` are `)` then
# `|`, and it is the `|` that eats the status.
Seg = collections.namedtuple('Seg', 'tokens post_ops depth')


def next_op(ops):
    """The operator that decides what becomes of a segment's exit status.

    Parens are transparent: closing a subshell hands its last statement's status
    outward unchanged, so whatever follows the `)` is what actually consumes it.
    """
    for op in ops:
        if op not in ('(', ')'):
            return op
    return ''


def tokenize(cmd):
    """(tokens, cleaned, heredoc_bodies); tokens is None when unparseable.

    ``cleaned`` is the command with comments and heredoc bodies removed -- the
    string the raw-text scans should read, so a `$PIPESTATUS` or a `$(…)` quoted
    inside a heredoc body is not mistaken for one the shell would evaluate.
    """
    expanded = []
    # Heredoc bodies go first so an unbalanced quote inside one cannot throw off
    # strip_comments' own quote tracking for the rest of the command.
    cleaned = strip_comments(strip_heredoc_bodies(cmd, expanded))
    try:
        # `\n` is made a punctuation char so a newline command boundary surfaces
        # as a token; it is otherwise eaten as whitespace, merging the commands
        # on either side. Quoted newlines stay inside their word token.
        lex = shlex.shlex(cleaned, posix=True, punctuation_chars=';()<>|&\n')
        lex.whitespace_split = True
        lex.whitespace = lex.whitespace.replace('\n', '')
        lex.commenters = ''
        tokens = list(lex)
    except ValueError:
        return None, cleaned, expanded            # unbalanced quotes -> defer
    return glue_dollar_paren(split_operator_runs(tokens)), cleaned, expanded


def split_segments(tokens):
    """Split a token stream into Segs, or return None when it will not parse.

    Redirect operators and their targets are dropped rather than collected: this
    guard checks no paths, and a target left in place would land in the head and
    stop `make check > log` from matching `^make`.
    """
    items = []                                    # ('cmd', tokens) | ('op', text)
    depths = []
    cur, depth, i = [], 0, 0
    while i < len(tokens):
        t = tokens[i]
        if t in SEPARATORS:
            if cur:
                items.append(('cmd', cur)); depths.append(depth)
                cur = []
            if t == ')':
                depth = max(0, depth - 1)
            items.append(('op', t)); depths.append(depth)
            if t == '(':
                depth += 1
            i += 1
            continue
        if t in REDIR or t in DUP:
            # An fd number written immediately before a redirect operator
            # (`2>file`, `2>&1`) tokenizes as a bare digit that lands in `cur`;
            # pop it so it does not leak into the head.
            if cur and cur[-1].isdigit():
                cur.pop()
            if t in DUP:
                if i + 1 < len(tokens):
                    i += 2
                    continue
                i += 1
                continue
            if i + 1 < len(tokens):
                i += 2                            # skip the target / delimiter
                continue
            i += 1
            continue
        cur.append(t); i += 1
    if cur:
        items.append(('cmd', cur)); depths.append(depth)

    # Two chain operators with no command between them is not something bash
    # would run. A hook that cannot parse a command has nothing to say about it.
    prev_op = None
    for kind, text in items:
        if kind == 'cmd':
            prev_op = None
            continue
        if text in CHAIN_OPS and prev_op in CHAIN_OPS:
            return None
        prev_op = text if text in CHAIN_OPS else None

    segs = []
    for idx, (kind, payload) in enumerate(items):
        if kind != 'cmd':
            continue
        post = []
        for j in range(idx + 1, len(items)):
            if items[j][0] != 'op':
                break
            post.append(items[j][1])
        segs.append(Seg(payload, tuple(post), depths[idx]))
    return segs


def peel_wrappers(tokens):
    """Strip reserved words, inline assignments, and wrapper commands, in the
    order bash resolves them, until the real command word is first."""
    while True:
        tokens = strip_env_prefix(strip_sh_keywords(tokens))
        if tokens and os.path.basename(tokens[0]) in WRAPPERS:
            tokens = tokens[1:]
            continue
        return tokens


def head_words(seg):
    """The words a registry pattern is matched against, real command word first."""
    return peel_wrappers(seg.tokens)


# --- Registry ---------------------------------------------------------------

# Flags that make an invocation a capability probe rather than a run: the tool
# prints and exits without producing a result, so there is no status to lose and
# nothing for a pipe to swallow. Recognised structurally for every gate rather
# than as per-tool exempt patterns -- one of those would fix the tool it was
# reported against and leave the whole class.
#
# `-v` is deliberately absent: it is --version to `make` but verbose to
# `go test`, so exempting it would exempt `go test -v ./... | tail`, which is
# the bug this guard exists to catch. `make -v | head` stays denied, and
# `--version` is the way out of it.
PROBE_FLAGS = frozenset({'--version', '--help', '-V', '-h', 'version', '--usage'})

# Sub-subcommands that report state without changing any: their output is the
# point, so there is no status for a pipe to swallow and nothing for `&&` to
# gate. Recognised structurally for every gate, the same way probes are --
# `git stash list`, `git worktree list`, `gh release list`, and `kubectl rollout
# history` are one shape, and a per-tool exempt row fixes one and leaves the
# class (#19).
#
# `status` is deliberately absent: `kubectl rollout status` waits for a
# condition and reports it as an exit code, so a pipe really does swallow the
# answer. It stays a gate, and stays out of `mutators` by not being named there.
READ_WORDS = frozenset({'list', 'ls', 'show', 'view', 'history'})


def is_read(words):
    """Whether the subcommand path names a read verb.

    Only from the third word on: at the second, a read verb is as likely to be
    a `make` target or a branch name as a subcommand, and `make list` belongs in
    the registry where its Makefile can be read. The scan stops at the first
    flag, so an operand is never mistaken for a subcommand -- `git commit -m
    show` is a commit.
    """
    for i, word in enumerate(words):
        if i < 2:
            continue
        if word.startswith('-'):
            break
        if word in READ_WORDS:
            return True
    return False


# POSIX bracket classes, so a pattern copied from an ERE-based registry works
# here. Python's `re` has no `[[:space:]]`; left untranslated it silently becomes
# a character set of `:aceps` and the pattern matches the wrong things.
POSIX_CLASSES = {
    '[:space:]': r'\s', '[:blank:]': r' \t', '[:alnum:]': 'a-zA-Z0-9',
    '[:alpha:]': 'a-zA-Z', '[:digit:]': '0-9', '[:upper:]': 'A-Z',
    '[:lower:]': 'a-z', '[:xdigit:]': '0-9A-Fa-f', '[:word:]': r'\w',
}

OVERRIDE_VAR = 'EXIT_STATUS_GUARD_OVERRIDE'
# The 1.x spelling still works, undocumented. It is named in downstream repo
# docs and CLAUDE.md files that the rename does not reach.
OVERRIDE_VARS = (OVERRIDE_VAR, 'PIPE_GUARD_OVERRIDE')


def translate_posix(pattern):
    for name, repl in POSIX_CLASSES.items():
        pattern = pattern.replace(name, repl)
    return pattern


class Registry(object):
    """Compiled gate / exempt / mutator patterns.

    A pattern that does not compile is dropped rather than fatal: this runs on
    every Bash call, so a bad edit to the registry must degrade the guard, never
    break the tool. `errors` reports the rejects so the test suite can fail
    on them.
    """

    def __init__(self, data):
        self.errors = []
        self.gates = self._compile(data.get('gates') or [])
        self.exempt = self._compile(data.get('exempt') or [])
        self.mutators = self._compile(data.get('mutators') or [])

    def _compile(self, patterns):
        out = []
        for p in patterns:
            try:
                out.append(re.compile(translate_posix(p)))
            except re.error:
                self.errors.append(p)
        return out

    @staticmethod
    def _any(compiled, text):
        return any(r.search(text) for r in compiled)

    def _matches(self, patterns, words):
        """Whether ``words`` are a registered, non-exempt, non-probe invocation.

        All three screens apply to both lists. A probe prints and exits, and a
        read reports, so neither is a run whose status is at stake nor a state
        change. Where the read verb is not in the subcommand path -- `git tag
        --sort` lists as surely as `git stash list` does -- the split is made in
        the registry instead, by naming the write forms and leaving the rest
        unregistered (#11, #19).
        """
        if not words:
            return False
        head = ' '.join(words)
        if self._any(self.exempt, head) or not self._any(patterns, head):
            return False
        # Read from the parsed words rather than the joined head, so a flag
        # spelled inside a quoted argument stays one word: `git commit -m "bump
        # --version output"` is a commit, and still a gate.
        if any(w in PROBE_FLAGS for w in words[1:]):
            return False
        return not is_read(words)

    def is_gate(self, words):
        """Whether these words are a registered gate whose status is the answer:
        registered, not exempted, and not a capability probe."""
        return self._matches(self.gates, words)

    def is_mutator(self, words):
        """Whether these words are a registered state change, screened the same
        way -- a read is not a state change, and neither is a probe."""
        return self._matches(self.mutators, words)


DEFAULT_REGISTRY = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'exit-status-guard.json')

# Project registry names, newest first. A repo that still carries the 1.x name
# keeps its patterns; naming both is cheaper than a migration nobody runs.
PROJECT_REGISTRY_NAMES = ('exit-status-guard.json', 'pipe-guard.json')

REGISTRY_ENV_VARS = ('EXIT_STATUS_GUARD_REGISTRY', 'PIPE_GUARD_REGISTRY')


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def project_registry_path(cwd):
    """`.claude/exit-status-guard.json` under the project root, or None.

    CLAUDE_PROJECT_DIR is the root Claude Code resolved; the payload's cwd is
    the fallback for older CLIs. The 1.x filename is read when the current one
    is absent, so both are never merged and the new name wins outright.
    """
    root = os.environ.get('CLAUDE_PROJECT_DIR') or cwd
    if not root:
        return None
    for name in PROJECT_REGISTRY_NAMES:
        path = os.path.join(root, '.claude', name)
        if os.path.isfile(path):
            return path
    return None


def load_registry(cwd=''):
    """Shipped defaults, extended by the project's own file when present.

    A project file adds patterns rather than replacing them, so a repo that
    names one extra gate does not silently drop every default. `"replace": true`
    in that file opts into full control.
    """
    override = next((os.environ[v] for v in REGISTRY_ENV_VARS
                     if os.environ.get(v)), '')
    base = _read_json(override or DEFAULT_REGISTRY) or {}
    local = _read_json(project_registry_path(cwd) or '') or {}
    if local.get('replace'):
        merged = local
    else:
        merged = dict(base)
        for key in ('gates', 'exempt', 'mutators'):
            merged[key] = list(base.get(key) or []) + list(local.get(key) or [])
    return Registry(merged)


# --- Rules ------------------------------------------------------------------

def truncate(s, n=70):
    return s if len(s) <= n else s[:n]


def reads_var(text, name, quotes=True):
    """Whether ``text`` reads `$NAME` or `${NAME…}` somewhere bash would expand it.

    Scans the raw string with quote state, because the distinction that matters
    is exactly the one shlex destroys: `'$PIPESTATUS'` in single quotes is
    literal text, `"${PIPESTATUS[0]}"` in double quotes is a real read.
    """
    i, n = 0, len(text)
    in_single = in_double = False
    while i < n:
        c = text[i]
        if in_single:
            if c == "'":
                in_single = False
            i += 1
            continue
        if c == '\\':
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
        if c == '$':
            rest = text[i+1:]
            if rest.startswith('{'):
                rest = rest[1:]
            if rest.startswith(name) and not re.match(
                    r'[A-Za-z0-9_]', rest[len(name):len(name)+1] or ' '):
                return True
        i += 1
    return False


def sets_pipefail(segs):
    """Whether any segment is `set -o pipefail`, in any of its spellings
    (including a combined `set -euo pipefail`)."""
    for seg in segs:
        words = head_words(seg)
        if len(words) >= 2 and words[0] == 'set' and 'pipefail' in words[1:]:
            return True
    return False


def has_override(segs):
    """Whether a segment carries the break-glass assignment with a reason.

    An empty value does not count: the point of the prefix is that the caller
    has to say why, and a bare `EXIT_STATUS_GUARD_OVERRIDE= <command>` would be
    the switch-it-off form.

    Only the LEADING assignment run of a segment is read, which is what makes
    the name asymmetric: a real assignment sits in command position, while the
    name quoted in a commit message or echoed into a pipe is an argument and
    disables nothing.
    """
    prefixes = tuple(name + '=' for name in OVERRIDE_VARS)
    for seg in segs:
        for tok in strip_sh_keywords(seg.tokens):
            if not ASSIGNMENT_RE.match(tok):
                break                             # past the assignment run
            for prefix in prefixes:
                if tok.startswith(prefix) and tok[len(prefix):].strip():
                    return True
    return False


def status_source(segs, idx):
    """The segment whose exit status the operator after ``segs[idx]`` consumes.

    Walks back over segments with no command word of their own -- a `}` closing
    a brace group, a `fi`, a `done`. A `)` is an operator rather than a word, so
    a subshell needs no special case: the segment before the `)` is already the
    last statement inside it, which is the status a pipe on the outside reads.
    """
    while idx >= 0:
        if head_words(segs[idx]):
            return segs[idx]
        idx -= 1
    return None


def piped_gate(segs, reg):
    """The head of the first gate whose status a pipe swallows, or ''."""
    for i, seg in enumerate(segs):
        if next_op(seg.post_ops) not in PIPE_OPS:
            continue
        src = status_source(segs, i)
        if src is None:
            continue
        words = head_words(src)
        if reg.is_gate(words):
            return ' '.join(words)
    return ''


def carries_status(chain, reg):
    """Whether a failing gate could still surface as this statement's status.

    ``chain`` alternates segments and the operators between them. Anything
    unrecognised counts as carrying, so an unfamiliar shape gets silence rather
    than a guess.

    Evaluated left to right, which is the association bash uses: in
    `a && b || c` the `||` sees `(a && b)` as its left side. `&&` lets either
    side be the last to run, so either can carry a failure out; `||` and `|`
    yield only their right side's status.
    """
    if not chain:
        return True
    acc = _seg_carries(chain[0], reg)
    for i in range(1, len(chain) - 1, 2):
        rhs = _seg_carries(chain[i + 1], reg)
        acc = (acc or rhs) if chain[i] == '&&' else rhs
    return acc


def _seg_carries(seg, reg):
    words = head_words(seg)
    if not words:
        return True                               # a bare assignment or `}`
    # `exit $rc` is the documented fix, and a literal `exit 0` is a deliberate
    # discard -- the escape hatch for a background call whose status genuinely
    # does not matter.
    if words[0] == 'exit':
        return True
    return reg.is_gate(words)


def last_statement(segs):
    """(chain, backgrounded) for the statement whose status the call reports.

    The statement is everything after the last `;`, newline, or `&` at any
    depth: a `;` inside a trailing subshell ends the sub-statement whose status
    the subshell itself yields, which is the same question one level down.
    """
    start, backgrounded = 0, False
    for i, seg in enumerate(segs):
        op = next_op(seg.post_ops)
        if op not in END_OPS:
            continue
        if i + 1 < len(segs):
            start, backgrounded = i + 1, False
        else:
            backgrounded = op == '&'              # trailing `&` forks the tail
    chain = []
    for i in range(start, len(segs)):
        if chain:
            chain.append(next_op(segs[i - 1].post_ops))
        chain.append(segs[i])
    return chain, backgrounded


def first_gate(segs, reg):
    for seg in segs:
        words = head_words(seg)
        if reg.is_gate(words):
            return ' '.join(words)
    return ''


def lost_background_status(segs, background, reg):
    """The head of a gate whose status never reaches the caller, or ''.

    A `;`-list yields its last statement's status, so a backgrounded
    `make check > log 2>&1; echo "EXIT=$?"` notifies exit 0 for a failed gate --
    the same false green as the pipe, arriving by a different route. A trailing
    `&` is the other spelling and loses the status even in the foreground.

    Foreground calls are otherwise silent by construction: there the trailing
    echo prints the real status where it can be read, which is the documented
    correct form.
    """
    if not segs:
        return ''
    chain, trailing_amp = last_statement(segs)
    if not background and not trailing_amp:
        return ''
    if not trailing_amp and carries_status(chain, reg):
        return ''
    return first_gate(segs, reg)


def sequenced_mutation(segs, reg):
    """(gate, mutator) when a gate is sequenced before a state-changing command
    with `;` rather than `&&`, or ('', '').

    `make check; git push` reads the gate's status correctly and then ignores
    it: the push runs whatever the check did. Only top-level segments count --
    inside a subshell the sequence is that subshell's own business -- and only a
    `;`/newline separator, since `&&` is the form that already gates.
    """
    gate, gate_at = '', -1
    for i, seg in enumerate(segs):
        if seg.depth != 0:
            continue
        words = head_words(seg)
        if gate_at >= 0 and i > gate_at and reg.is_mutator(words):
            return gate, ' '.join(words)
        if next_op(seg.post_ops) in (';', '\n') and reg.is_gate(words):
            gate, gate_at = ' '.join(words), i
    return '', ''


# --- Reasons ----------------------------------------------------------------

# Each suggested rewrite names a log file, and a suggestion that does not run is
# worse than none: the session reading it has just been denied and is copying
# verbatim. `tmp/` is a build-output name -- commonly gitignored, so absent from
# a fresh checkout or a new worktree -- and the redirect then fails before the
# gate ever runs. So the path is resolved per call: the session's scratchpad
# when it is really there, and otherwise a form that creates its own directory.
LOG_PLACEHOLDER = '<LOG>'
MKDIR_PLACEHOLDER = '<MKDIR>'

SCRATCH_ROOT = '/tmp/claude-%d'
SESSION_ID_RE = re.compile(r'[A-Za-z0-9_-]+')


def scratch_root():
    """The per-user root Claude Code puts session directories under, or ''.

    POSIX only. Windows has no `os.getuid()` and puts the root under the
    per-user temp dir, whose backslashes would need converting before they could
    go in a bash redirect -- so the mkdir form is the answer there instead.
    `hasattr` is the discriminator rather than `os.name` because the missing
    call is the actual condition.
    """
    return SCRATCH_ROOT % os.getuid() if hasattr(os, 'getuid') else ''


def scratch_dir(session_id, root=None):
    """This session's scratchpad directory, or '' when it is not found.

    Claude Code lays it out as `<root>/<project-slug>/<session id>/scratchpad`.
    The slug encoding is undocumented and differs between a worktree and the
    main checkout, so the project dir is located by scanning ``root`` for the
    child that already holds this session -- ground truth from the filesystem,
    the way workspace-guard's `claude_session_project_dir` does it. A listdir
    and isdir scan only; nothing is read, and a layout that moves underneath
    this returns '' rather than a path that is not there.
    """
    if root is None:
        root = scratch_root()
    if not root or not SESSION_ID_RE.fullmatch(session_id or ''):
        return ''
    try:
        slugs = os.listdir(root)
    except OSError:
        return ''
    for slug in slugs:
        path = os.path.join(root, slug, session_id, 'scratchpad')
        try:
            if os.path.isdir(path):
                return path
        except OSError:
            continue
    return ''


def with_log_path(reason, scratch):
    """Fill a reason's log placeholders with a path the redirect can write.

    Joined with `/` rather than `os.path.join`, because the result is a bash
    command line: a backslash separator there is an escape character, not a
    path.
    """
    if scratch:
        return reason.replace(MKDIR_PLACEHOLDER, '').replace(
            LOG_PLACEHOLDER, scratch + '/out.log')
    return reason.replace(MKDIR_PLACEHOLDER, 'mkdir -p tmp && ').replace(
        LOG_PLACEHOLDER, 'tmp/out.log')


# The tail every reason ends with. Every rule here is about a status nobody
# will read, so the sentence before it is the same one throughout.
OVERRIDE_TAIL = (
    " If this call is not the mistake the rule describes, that is a defect in "
    "the rule: report it at https://github.com/karlkfi/claude-pipe-guard/issues "
    "rather than overriding it every time.")

OVERRIDE_HINT = (
    " If the status genuinely does not matter here, re-run prefixed with "
    + OVERRIDE_VAR + "=<reason>." + OVERRIDE_TAIL)

PIPESTATUS_REASON = (
    "This reads $PIPESTATUS, which does not exist in zsh -- the shell the Bash "
    "tool runs. It expands to empty, so the test against it reads as success "
    "whatever the pipeline did. zsh spells it $pipestatus (lowercase, "
    "1-indexed); better still, redirect and read the status directly: "
    '<MKDIR>cmd > <LOG> 2>&1; echo "EXIT=$?".' + OVERRIDE_HINT)

PIPED_REASON = (
    " is piped into a filter, so this call's exit status is the filter's, not "
    "the gate's -- a failure reads exactly like a pass, and zsh (the shell the "
    "Bash tool runs) has no PIPESTATUS to recover it. Redirect instead, then "
    "reconcile status against output: "
    "<MKDIR>cmd > <LOG> 2>&1; echo \"EXIT=$?\"; "
    "grep -E 'FAILED|Error [0-9]|^make:' <LOG>." + OVERRIDE_HINT)

LOST_STATUS_REASON = (
    " runs in the background, but this call's exit status is its LAST "
    "statement's -- an echo exits 0 whatever the gate did, so the task "
    "notification reports success for a failed gate. Capture the status and "
    're-raise it: <MKDIR>cmd > <LOG> 2>&1; rc=$?; echo "EXIT=$rc"; exit $rc.'
    + OVERRIDE_HINT)

SEQUENCED_REASON_HEAD = "` is sequenced before `"
SEQUENCED_REASON = (
    "` with `;`, which runs the second whatever the first returned -- the "
    "status is read correctly and then ignored, so a failed check still "
    "publishes. Join them with `&&` so the state change is conditional on the "
    "check passing." + OVERRIDE_HINT)


def decide(cmd, background, reg, scratch='', depth=0):
    """The deny reason for a Bash command, or '' to stay silent.

    ``scratch`` is the session scratchpad the suggested rewrites should redirect
    into; empty means they carry their own `mkdir` instead.

    Every failure path returns '': a hook that cannot parse a command has
    nothing to say about it.
    """
    tokens, cleaned, heredocs = tokenize(cmd)
    if tokens is None:
        return ''
    segs = split_segments(tokens)
    if segs is None:
        return ''
    if has_override(segs):
        return ''

    # $PIPESTATUS is wrong wherever it appears, gate or no gate. Read from the
    # cleaned string plus any heredoc body whose delimiter was unquoted, which
    # is exactly what bash expands: a quoted delimiter makes the body literal,
    # so the same text there is a note about the bug rather than the bug.
    if reads_var(cleaned, 'PIPESTATUS') or any(
            reads_var(body, 'PIPESTATUS', quotes=False) for body in heredocs):
        return with_log_path(PIPESTATUS_REASON, scratch)

    # `pipefail` propagates the failure and zsh's `$pipestatus` recovers each
    # stage's status. Neither mitigates a status the last statement discarded,
    # so the suppression is scoped to the pipe verdict.
    if not sets_pipefail(segs) and not reads_var(cleaned, 'pipestatus'):
        gate = piped_gate(segs, reg)
        if gate:
            return with_log_path(
                '`' + truncate(gate) + '`' + PIPED_REASON, scratch)

    gate = lost_background_status(segs, background, reg)
    if gate:
        return with_log_path(
            '`' + truncate(gate) + '`' + LOST_STATUS_REASON, scratch)

    gate, mutator = sequenced_mutation(segs, reg)
    if gate:
        return ('`' + truncate(gate) + SEQUENCED_REASON_HEAD
                + truncate(mutator) + SEQUENCED_REASON)

    # A gate inside a backtick substitution never reaches the segment loop as a
    # command (backticks are ordinary word characters to shlex), so the bodies
    # are analyzed on their own. `$(…)` bodies mostly arrive through the `(`
    # already in the stream; recursing costs little and covers the rest.
    if depth < MAX_SUBST_DEPTH:
        bodies = command_substitutions(cleaned)
        for body in heredocs:
            bodies.extend(command_substitutions(body, quotes=False))
        for body in bodies:
            reason = decide(body, False, reg, scratch, depth + 1)
            if reason:
                return reason

    return ''


# --- Hook I/O ---------------------------------------------------------------

def emit(decision, reason):
    """Print a PreToolUse decision as the hook's stdout JSON."""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason}}))


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return
    if (data.get('tool_name') or 'Bash') != 'Bash':
        return
    ti = data.get('tool_input') or {}
    cmd = ti.get('command') or ''
    if not cmd.strip():
        return
    cwd = data.get('cwd') or ''
    reg = load_registry(cwd)
    reason = decide(cmd, bool(ti.get('run_in_background')), reg,
                    scratch_dir(data.get('session_id') or ''))
    if reason:
        # Always `deny`, never `ask`. The reason reaches the model, so the fix
        # lands where the command is rewritten; an `ask` goes to the user and
        # the model never sees why.
        emit('deny', reason)


if __name__ == '__main__':
    main()
