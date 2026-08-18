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

A fourth check is optional, and off unless a project's own registry carries a
`repo_state` key. It is not about exit status: it denies a `git push` onto a
base that has moved into this branch's own line ranges, and a `gh pr create`
where an open PR already changes them. Both shell out, and both fail silent --
offline, an old git, or a rate-limited token costs a missed catch and never a
blocked command.

Every verdict is a `deny`, never an `ask`. A deny's reason is shown to the
model, so the fix lands where the command gets rewritten; an ask goes to the
user and the model never sees the hint. The break-glass is a command prefix,
`PIPE_GUARD_OVERRIDE=<reason> <command>` -- an environment assignment, because
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
import sys, os, json, re, shlex, collections, fnmatch, subprocess

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

# POSIX bracket classes, so a pattern copied from an ERE-based registry works
# here. Python's `re` has no `[[:space:]]`; left untranslated it silently becomes
# a character set of `:aceps` and the pattern matches the wrong things.
POSIX_CLASSES = {
    '[:space:]': r'\s', '[:blank:]': r' \t', '[:alnum:]': 'a-zA-Z0-9',
    '[:alpha:]': 'a-zA-Z', '[:digit:]': '0-9', '[:upper:]': 'A-Z',
    '[:lower:]': 'a-z', '[:xdigit:]': '0-9A-Fa-f', '[:word:]': r'\w',
}

OVERRIDE_VAR = 'PIPE_GUARD_OVERRIDE'


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
        # Presence turns the repo-state checks on; an empty object is presence,
        # so `{}` runs them on the defaults.
        cfg = data.get('repo_state')
        self.repo_state = cfg if isinstance(cfg, dict) else None

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

        Both screens apply to both lists. A probe prints and exits, so it is
        neither a run whose status is at stake nor a state change. `exempt` is
        where a subcommand's read form is told apart from its write form:
        `git tag -l` and `git tag -a` match the same pattern otherwise, which had
        rule 3 casting the write as the gate and the read as the publish (#11).
        """
        if not words:
            return False
        head = ' '.join(words)
        if self._any(self.exempt, head) or not self._any(patterns, head):
            return False
        # Read from the parsed words rather than the joined head, so a flag
        # spelled inside a quoted argument stays one word: `git commit -m "bump
        # --version output"` is a commit, and still a gate.
        return not any(w in PROBE_FLAGS for w in words[1:])

    def is_gate(self, words):
        """Whether these words are a registered gate whose status is the answer:
        registered, not exempted, and not a capability probe."""
        return self._matches(self.gates, words)

    def is_mutator(self, words):
        """Whether these words are a registered state change, screened the same
        way -- a read is not a state change, and neither is a probe."""
        return self._matches(self.mutators, words)


DEFAULT_REGISTRY = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', 'pipe-guard.json')


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def project_registry_path(cwd):
    """`.claude/pipe-guard.json` under the project root, or None.

    CLAUDE_PROJECT_DIR is the root Claude Code resolved; the payload's cwd is
    the fallback for older CLIs.
    """
    root = os.environ.get('CLAUDE_PROJECT_DIR') or cwd
    if not root:
        return None
    path = os.path.join(root, '.claude', 'pipe-guard.json')
    return path if os.path.isfile(path) else None


def load_registry(cwd=''):
    """Shipped defaults, extended by the project's own file when present.

    A project file adds patterns rather than replacing them, so a repo that
    names one extra gate does not silently drop every default. `"replace": true`
    in that file opts into full control.
    """
    base = _read_json(os.environ.get('PIPE_GUARD_REGISTRY') or DEFAULT_REGISTRY) or {}
    local = _read_json(project_registry_path(cwd) or '') or {}
    if local.get('replace'):
        merged = local
    else:
        merged = dict(base)
        for key in ('gates', 'exempt', 'mutators'):
            merged[key] = list(base.get(key) or []) + list(local.get(key) or [])
        if 'repo_state' in local:
            merged['repo_state'] = local['repo_state']
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
    has to say why, and a bare `PIPE_GUARD_OVERRIDE= <command>` would be the
    switch-it-off form.

    Only the LEADING assignment run of a segment is read, which is what makes
    the name asymmetric: a real assignment sits in command position, while the
    name quoted in a commit message or echoed into a pipe is an argument and
    disables nothing.
    """
    prefix = OVERRIDE_VAR + '='
    for seg in segs:
        for tok in strip_sh_keywords(seg.tokens):
            if not ASSIGNMENT_RE.match(tok):
                break                             # past the assignment run
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


# The tail every reason ends with. The sentence before it names the way out,
# and that differs by rule: three of them are about a status nobody will read,
# and the repo-state pair is about an overlap, where "the status does not
# matter" is an answer to a question nobody asked.
OVERRIDE_TAIL = (
    " If this call is not the mistake the rule describes, that is a defect in "
    "the rule: report it at https://github.com/karlkfi/claude-pipe-guard/issues "
    "rather than overriding it every time.")

OVERRIDE_HINT = (
    " If the status genuinely does not matter here, re-run prefixed with "
    + OVERRIDE_VAR + "=<reason>." + OVERRIDE_TAIL)

REPO_STATE_HINT = (
    " If the overlap is known and deliberate, re-run prefixed with "
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


# --- Repo state -------------------------------------------------------------
# The fourth check, and the only one not about exit status. Off unless a
# project's own registry carries a `repo_state` key. Everything here shells
# out, so everything here fails silent: a caller reads None or an empty list as
# no opinion, and a missing binary, an old git, an offline machine, or a
# rate-limited token costs a missed catch rather than a blocked push.

PROBE_TIMEOUT = 5                                 # seconds, per subprocess
CONTEXT_LINES = 3                                 # what a diff hunk carries
MAX_OPEN_PRS = 20
MAX_PR_DIFFS = 3

DEFAULT_BASE_REF = 'origin/main'

# A release branch is cut from the base and left diverged on purpose, so
# telling it to rebase would publish everything merged since the tag -- a wrong
# answer rather than a noisy one. Nothing in the commit graph separates it from
# a stale topic branch: both are behind the base and ahead of the fork point.
# The name is the only signal there is, which is why it is configuration.
DEFAULT_RELEASE_PATTERNS = (
    r'^(release|rel|stable|maint|maintenance|hotfix)[/-]',
    r'^v?[0-9]+\.[0-9]+([./-]|$)',
)

# Flags that leave the base untouched: nothing lands on it, so there is no
# overlap to have.
PUSH_SKIP_FLAGS = frozenset({'--dry-run', '-n', '--delete', '-d'}) | PROBE_FLAGS

HUNK_RE = re.compile(r'^@@ -([0-9]+)(?:,([0-9]+))? \+')
PUSH_HEAD_RE = re.compile(r'^git\s+push(\s|$)')
PR_CREATE_HEAD_RE = re.compile(r'^gh\s+pr\s+create(\s|$)')

Repo = collections.namedtuple('Repo', 'root cfg')


def capture(argv, cwd, timeout=PROBE_TIMEOUT):
    """(exit status, stdout) for a subprocess, or (None, '') if it never ran."""
    try:
        proc = subprocess.run(argv, cwd=cwd, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, ''
    return proc.returncode, proc.stdout.decode('utf-8', 'replace')


def git(root, *args):
    """stdout of a successful `git` invocation, or None."""
    status, out = capture(('git', '-C', root) + args, root)
    return out if status == 0 else None


def hunk_range(start, count):
    """A hunk's pre-image span, widened by the context git carries either side.

    `-a,0` is an insertion after line ``a`` and covers no pre-image line of its
    own; it still collides with an edit beside it, so it spans that one.
    """
    last = start + count - 1 if count else start
    return max(1, start - CONTEXT_LINES), last + CONTEXT_LINES


def parse_hunks(diff):
    """{path: [(start, end)]} from a unified diff, in pre-image line numbers.

    The pre-image side is what makes two diffs comparable: taken from a shared
    ancestor, both sides' `-` ranges are numbered in that ancestor. Post-image
    numbers are each side's own and mean nothing to the other.

    A `--- ` line only names a file inside a header run, because a removed line
    carries a `-` of its own: an SQL comment `-- DROP` comes out of the diff as
    `--- DROP` and is content, not a header. `@@` needs no such guard -- a
    removed hunk header is prefixed too, and a context line starts with a space.
    """
    ranges, path, in_header = {}, '', False
    for line in diff.splitlines():
        if line.startswith('diff --git '):
            path, in_header = '', True
            continue
        if in_header and line.startswith('--- '):
            path = '' if line == '--- /dev/null' else line[6:]
            continue
        if not path:
            continue
        m = HUNK_RE.match(line)
        if m:
            in_header = False
            ranges.setdefault(path, []).append(
                hunk_range(int(m.group(1)),
                           1 if m.group(2) is None else int(m.group(2))))
    return ranges


def changed_ranges(root, old, new):
    """What changed between two revisions, or None.

    `-U0` so a hunk covers only the lines that moved; the context comes back in
    `hunk_range`, which is where its width is stated once. The prefixes are
    pinned rather than inherited, because `diff.mnemonicPrefix` renames them
    and the path would then be read out of the wrong column.
    """
    out = git(root, 'diff', '-U0', '--no-color', '--no-ext-diff',
              '--src-prefix=a/', '--dst-prefix=b/', old, new)
    return None if out is None else parse_hunks(out)


def ranges_meet(mine, theirs):
    return any(a[0] <= b[1] and b[0] <= a[1] for a in mine for b in theirs)


def merge_conflicts(root, base):
    """Paths git reports conflicting when ``base`` merges into HEAD.

    None when git would not say -- no `--write-tree` before 2.38, a missing
    ref, a shallow clone. The one caller discounts the path either way, since
    an old git must not start denying pushes; the two are kept apart so the
    suite can tell "clean" from "could not ask" and skip rather than pass.
    """
    status, out = capture(('git', '-C', root, 'merge-tree', '--write-tree',
                           '--name-only', base, 'HEAD'), root)
    if status not in (0, 1):
        return None
    if status == 0:
        return frozenset()
    paths = []
    for line in out.splitlines()[1:]:             # line 1 is the merged tree
        if not line:
            break                                 # then the conflict messages
        paths.append(line)
    return frozenset(paths)


def current_branch(root):
    out = git(root, 'symbolic-ref', '--short', '--quiet', 'HEAD')
    return out.strip() if out else ''


def is_release_branch(branch, cfg):
    patterns = cfg.get('release_patterns')
    if patterns is None:
        patterns = DEFAULT_RELEASE_PATTERNS
    for pattern in patterns:
        try:
            if re.search(pattern, branch):
                return True
        except re.error:
            continue                              # a bad row degrades, not breaks
    return False


def is_ignored(path, cfg):
    return any(fnmatch.fnmatch(path, pat)
               for pat in cfg.get('overlap_ignore') or [])


def push_overlap(root, cfg):
    """Paths where the base's own movement lands in this branch's edits, or [].

    A stale base is benign under a merge queue. An overlap is not: the queue
    validates the candidate merge, kicks the entry back, and the check cycle
    that found it is spent. A local rebase catches it in seconds.
    """
    branch = current_branch(root)
    if not branch or is_release_branch(branch, cfg):
        return []
    base = cfg.get('base_ref') or DEFAULT_BASE_REF
    fork = git(root, 'merge-base', 'HEAD', base)
    tip = git(root, 'rev-parse', '--verify', '--quiet', base + '^{commit}')
    if fork is None or tip is None:
        return []
    fork, tip = fork.strip(), tip.strip()
    if not fork or fork == tip:
        return []                                 # the base has not moved
    mine = changed_ranges(root, fork, 'HEAD')
    theirs = changed_ranges(root, fork, base)
    if mine is None or theirs is None:
        return []
    shared = sorted(set(mine) & set(theirs))
    ignored = [p for p in shared if is_ignored(p, cfg)]
    # An `overlap_ignore` path is contended by construction -- a custom merge
    # driver owns it and nearly every branch edits it -- so counting its ranges
    # would fire always. A driver still refuses some of them, a row deleted one
    # side and edited the other, so the discount is conditional on asking.
    # git declining to answer discounts the path, same as a clean merge: an
    # old git or a shallow clone must not turn into a wall of denied pushes.
    conflicts = merge_conflicts(root, base) if ignored else frozenset()
    hits = []
    for path in shared:
        if path in ignored:
            if conflicts and path in conflicts:
                hits.append(path)
        elif ranges_meet(mine[path], theirs[path]):
            hits.append(path)
    return hits


def open_prs(root):
    """Open PRs with the paths each one touches, or None."""
    status, out = capture(
        ('gh', 'pr', 'list', '--state', 'open', '--limit', str(MAX_OPEN_PRS),
         '--json', 'number,title,headRefName,files'), root)
    if status != 0:
        return None
    try:
        prs = json.loads(out)
    except ValueError:
        return None
    return prs if isinstance(prs, list) else None


def pr_ranges(root, number):
    """What an open PR changes, or None.

    Numbered from that PR's own merge base rather than this branch's, so a
    long-lived PR's ranges drift. Close enough to tell an edit in the same
    function from one at the other end of the file, which is the question.
    """
    status, out = capture(('gh', 'pr', 'diff', str(number)), root)
    return None if status != 0 else parse_hunks(out)


def pr_overlap(root, cfg):
    """[(number, title, paths, precise)] for open PRs on this branch's own
    lines, or [].

    ``precise`` is False when the PR's diff was not fetched -- the cap was
    reached, or the call failed -- and the entry rests on a shared path alone.
    Ranges need a fetch per PR that already shares a path, and this runs while
    someone waits on a `gh pr create`.
    """
    branch = current_branch(root)
    base = cfg.get('base_ref') or DEFAULT_BASE_REF
    fork = git(root, 'merge-base', 'HEAD', base)
    if not branch or fork is None:
        return []
    mine = changed_ranges(root, fork.strip(), 'HEAD')
    if not mine:
        return []
    prs = open_prs(root)
    if prs is None:
        return []
    hits, fetched = [], 0
    for pr in prs:
        if pr.get('headRefName') == branch:
            continue
        shared = sorted(p for p in (f.get('path')
                                    for f in pr.get('files') or [])
                        if p in mine and not is_ignored(p, cfg))
        if not shared:
            continue
        theirs = None
        if fetched < MAX_PR_DIFFS:
            fetched += 1                          # a failed fetch spends it too
            theirs = pr_ranges(root, pr.get('number'))
        if theirs is not None:
            shared = [p for p in shared
                      if p in theirs and ranges_meet(mine[p], theirs[p])]
            if not shared:
                continue
        hits.append((pr.get('number'), pr.get('title') or '', shared,
                     theirs is not None))
    return hits

PUSH_OVERLAP_REASON = (
    "`git push`: `%s` has moved since this branch left it, and its new commits "
    "edit the same lines this branch does in %s. A merge queue validates that "
    "candidate merge, kicks the entry back, and a whole check cycle is spent "
    "finding what a rebase finds in seconds. Rebase first -- "
    "`git fetch && git rebase %s` -- then push the result." + REPO_STATE_HINT)

PR_OVERLAP_REASON = (
    "`gh pr create`: this branch edits lines an open PR already changes -- %s. "
    "That is duplicated or mutually invalidating work, and review is an "
    "expensive place to find it. Read that diff first, then either fold this "
    "into that branch or narrow this one to what does not overlap."
    + REPO_STATE_HINT)


def push_reason(cfg, paths):
    base = cfg.get('base_ref') or DEFAULT_BASE_REF
    return PUSH_OVERLAP_REASON % (base, ', '.join(paths), base)


def pr_reason(prs):
    parts = ['#%s %s (%s%s)' % (number, truncate(title, 50), ', '.join(paths),
                                '' if precise else
                                '; shared path only, diff not fetched')
             for number, title, paths, precise in prs]
    return PR_OVERLAP_REASON % '; '.join(parts)

def repo_state_reason(segs, repo):
    """The deny reason for a push or a PR the repo's own state contradicts.

    Matched against the segment head like every other rule, so a `git push`
    inside a commit message or a heredoc is not one.
    """
    for seg in segs:
        words = head_words(seg)
        if not words:
            continue
        head = ' '.join(words)
        if PUSH_HEAD_RE.match(head):
            if PUSH_SKIP_FLAGS.isdisjoint(words[1:]):
                paths = push_overlap(repo.root, repo.cfg)
                if paths:
                    return push_reason(repo.cfg, paths)
        elif PR_CREATE_HEAD_RE.match(head) and PROBE_FLAGS.isdisjoint(words[1:]):
            prs = pr_overlap(repo.root, repo.cfg)
            if prs:
                return pr_reason(prs)
    return ''


def decide(cmd, background, reg, scratch='', depth=0, repo=None):
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

    # Last, and only at the top level: a lost status is the primary job, and
    # this one shells out. `repo` is None unless the project asked for it, and
    # the recursion above never passes it on -- a `git push` inside a
    # substitution is still one command's worth of repo state.
    if repo is not None:
        return repo_state_reason(segs, repo)
    return ''


# --- Hook I/O ---------------------------------------------------------------

def repo_state(cwd, reg):
    """The repo the state checks should ask about, or None when they are off.

    The root is resolved from the session's cwd rather than this file's own
    location. In a worktree the hook is the launch checkout's, so reading the
    repo around it reports overlaps the session's branch does not have.
    """
    if reg.repo_state is None:
        return None
    root = cwd or os.environ.get('CLAUDE_PROJECT_DIR') or ''
    if not root or not os.path.isdir(root):
        return None
    top = git(root, 'rev-parse', '--show-toplevel')
    return Repo(top.strip(), reg.repo_state) if top and top.strip() else None


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
                    scratch_dir(data.get('session_id') or ''),
                    repo=repo_state(cwd, reg))
    if reason:
        # Always `deny`, never `ask`. The reason reaches the model, so the fix
        # lands where the command is rewritten; an `ask` goes to the user and
        # the model never sees why.
        emit('deny', reason)


if __name__ == '__main__':
    main()
