"""Shell-command parsing shared by the claude-bouncer guards.

Every guard in this marketplace answers a different question -- which files a
command touches, which branch it pushes to, which cluster it mutates, whether
its exit status survives, how long it holds the foreground -- but all five must
first agree on what the command *is*. They each grew their own copy of that
lexer, and the copies drifted: on 2026-08-20 workspace-guard and
exit-status-guard fixed the same heredoc bug, on the same day, with different
mechanisms and different residual gaps. This module is the single copy they now
share.

The rule for what lives here is deliberately narrow: a primitive belongs in this
module when the guards need it to behave IDENTICALLY. Tokenizing a command is
one such primitive. Deciding what a token means is not -- each guard keeps its
own segmentation and classification, because their contracts genuinely differ
(exit-status-guard needs the operator that joined two commands; foreground-guard
needs to know which segment was backgrounded). Sharing those would force one
guard's answer onto another's question.

Layers, in the order a command passes through them:

  raw string  -> strip_comments, strip_heredoc_bodies   (text bash never lexes)
              -> command_substitutions                   (bodies to recurse into)
              -> lex                                     (shlex, POSIX quoting)
  tokens      -> split_operator_runs, glue_dollar_paren  (operator repair)
              -> strip_env_prefix, strip_sh_keywords     (find the real argv[0])

Fail-safe direction: a parse that cannot be completed returns less, never more.
`lex` raises ValueError on unbalanced quotes so callers defer rather than guess,
and an unterminated substitution or heredoc contributes nothing.

This file is vendored. The canonical copy is `lib/bouncer_parse.py` at the
repository root; `scripts/sync-lib.py` copies it into each plugin, and CI fails
if a copy has drifted. Edit the root copy, never a vendored one.
"""
import re
import shlex


# ---------------------------------------------------------------- constants

ASSIGNMENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')

# Shell keywords that can precede the real command word in a compound statement.
SH_KEYWORDS = frozenset({
    'while', 'until', 'if', 'then', 'elif', 'else', 'do', 'done', 'fi',
    'case', 'esac', 'in', 'time', 'function', '!', '{', '}', '[[', ']]',
})

# Command separators and redirect operators, after shlex punctuation grouping.
# `|&` is bash's pipe-both-streams operator; omitting it splits `a |& b` into a
# `|` and a stray `&`, which reads as a backgrounded command that never ran.
SEPARATORS = {'|', '|&', '||', '&&', '&', ';', '\n', '(', ')'}
REDIR = {'>', '>>', '<', '<<', '<<<', '>|', '&>', '&>>'}
DUP = {'>&', '<&'}

PIPE_OPS = {'|', '|&'}
CHAIN_OPS = PIPE_OPS | {'&&', '||'}
END_OPS = {';', '\n', '&'}

# Every char shlex treats as punctuation. `\n` is included so a newline command
# boundary surfaces as its own token instead of being eaten as whitespace,
# merging the commands on either side.
PUNCT_CHARS = frozenset(';()<>|&\n')

# Longest-first, so `&&` is matched before `&` when splitting an operator run.
_OPERATORS = tuple(sorted(SEPARATORS | REDIR | DUP, key=len, reverse=True))

# Characters after which an unquoted `#` starts a comment, per bash: a `#` that
# begins a word. Mid-word (`file#1`) it is ordinary text.
COMMENT_PRECEDERS = frozenset(' \t\n;|&()<>')

# Backstop on substitution recursion. A command nested deeper than this is not
# analysed rather than risking unbounded work on a pathological input.
MAX_SUBST_DEPTH = 25

# The `last` value that leaves the scanner at a command position, where a `#`
# starts a comment -- what bash sees just inside a `$(` or a backtick.
SUBST_OPEN = '('

# Reserved words after which bash reads another command, so a `case` following
# one is the keyword and not an argument (`if x; then case $y in ...`). Any
# other word ends command position: in `echo case`, `case` is a plain operand.
_CMD_POS_KEYWORDS = frozenset({
    'if', 'then', 'elif', 'else', 'while', 'until', 'do', 'time',
})

_WORD_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')


# -------------------------------------------------- raw-string preprocessing
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

def _consume_heredoc_body(text, i, delim, strip_tabs):
    """Skip a heredoc body starting at ``i`` (first char after the command
    line's newline) up to and including the terminator line, or end-of-input.

    Body lines are compared RAW — no quote/expansion parsing — so an apostrophe,
    an unbalanced quote, `</div>`, or `func(` in the body can never affect the
    scan. A line equals the terminator when it is exactly ``delim`` (for
    ``<<-``, after stripping leading tabs). Returns the index just past the
    terminator's newline; on an unterminated body, ``len(text)`` (matching bash,
    which swallows to end-of-input)."""
    return _consume_heredoc_body_ex(text, i, delim, strip_tabs)[0]


def _consume_heredoc_body_ex(text, i, delim, strip_tabs):
    """``_consume_heredoc_body`` plus whether the terminator was actually found.

    Callers that judge a malformed command need the distinction: bash hands an
    unterminated body to the command as data, but a guard reading a command
    that could never have run this way may prefer to keep inspecting the text
    rather than assume the friendlier reading.
    """
    n = len(text)
    while i < n:
        j = i
        while j < n and text[j] != '\n':
            j += 1
        line = text[i:j]
        if (line.lstrip('\t') if strip_tabs else line) == delim:
            return (j + 1 if j < n else n), True  # drop the terminator line
        i = j + 1 if j < n else n                 # drop this body line
    return n, False

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

def strip_heredoc_bodies(cmd, expanded=None, unterminated=None):
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
    ``files_in_command`` and the `<<`-delimiter skip there are unchanged; a
    trailing `<<EOF > out` redirect still parses. The body and its terminator
    line are dropped — which is what lets literal variable propagation stay live
    across a heredoc (Q67): no body line ever reaches the group loop.

    Command-line quote state is tracked so a `<<` inside a quoted string is not
    mistaken for a heredoc. A `$(…)` or backtick body opens a FRESH quote
    context, as it does in bash, so a heredoc inside one is found even when the
    substitution itself sits in double quotes — the shape a multi-paragraph
    commit message takes (``git commit -F "$(cat <<'MSG' … MSG\n)"``). Tracked
    flat, the enclosing `"` hid that `<<`, the body survived into shlex, and an
    odd number of `"` in it aborted the parse of the WHOLE command, so a
    guarded outside-workspace path later on the line went unchecked (issue 169).
    The `(` depth of each context is counted so a subshell's `)` does not end
    the substitution early. An unquoted `#` comment is skipped for `<<`
    detection (its text is left for ``strip_comments`` to remove). Arithmetic
    `$((a<<b))` / `((a<<b))` regions are copied verbatim — their `<<` is a shift,
    not a redirection, so they never arm a bogus delimiter. `<<<` here-strings
    are a distinct operator and never match. A `<<` with no delimiter word arms
    nothing; an unterminated body swallows to end-of-input, both matching bash.

    Pass a list as ``unterminated`` to collect the bodies whose terminator line
    never appeared, quoted or not. Bash swallows such a body to end-of-input and
    hands it over as data, which is what this function does; a guard that would
    rather keep judging text from a command that could never have run this way
    reads them back out and scans them itself.

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
    depth = 0                                     # unclosed `(` in this context
    stack = []                                    # (term, in_double, pending, depth)
    while i < n:
        c = cmd[i]
        if in_single:
            out.append(c); last = c
            if c == "'":
                in_single = False
            i += 1
            continue
        if c == '\\' and i + 1 < n:               # escapes, quoted or not
            out.append(c); out.append(cmd[i+1]); last = cmd[i+1]; i += 2
            continue
        # A substitution body is parsed in its own quote context, so these two
        # openers are recognised whether or not a `"` is still open.
        if c == '$' and i + 2 < n and cmd[i+1] == '(' and cmd[i+2] != '(':
            stack.append((')', in_double, pending, depth))
            in_double = False; pending = []; depth = 0
            out.append('$('); last = SUBST_OPEN; i += 2
            continue
        if c == '`':
            if stack and stack[-1][0] == '`':     # closes the body it opened
                _, in_double, pending, depth = stack.pop()
                out.append(c); last = c; i += 1
                continue
            stack.append(('`', in_double, pending, depth))
            in_double = False; pending = []; depth = 0
            out.append(c); last = SUBST_OPEN; i += 1
            continue
        if in_double:
            out.append(c); last = c
            if c == '"':
                in_double = False
            i += 1
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
        if c == '(':
            depth += 1                            # subshell — not our terminator
            out.append(c); last = c; i += 1
            continue
        if c == ')':
            if depth:
                depth -= 1
            elif stack and stack[-1][0] == ')':
                _, in_double, pending, depth = stack.pop()
            out.append(c); last = c; i += 1
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
                end, closed = _consume_heredoc_body_ex(cmd, i, delim, strip_tabs)
                if expanded is not None and not quoted:
                    expanded.append(cmd[i:end])
                if unterminated is not None and not closed:
                    unterminated.append(cmd[i:end])
                i = end
            continue
        out.append(c); last = c; i += 1
    return ''.join(out)


# ------------------------------------------- command substitution scanning

def _scan_case_pattern(text, start):
    """Scan a ``case`` pattern list from ``start`` to the ``)`` that ends it.

    Returns the index just past that ``)``, or ``None`` when the text runs out
    first. A leading ``(`` is bash's optional pattern opener and is consumed
    without nesting, so ``(a)`` and ``a)`` end at the same place; parens written
    INSIDE the pattern -- an extglob ``@(a|b)`` -- do nest.
    """
    i, n, depth = start, len(text), 0
    if i < n and text[i] == '(':
        i += 1
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
        elif c == '"':
            in_double = True
        elif c == '(':
            depth += 1
        elif c == ')':
            if depth == 0:
                return i + 1
            depth -= 1
        i += 1
    return None


def _scan_dollar_paren(text, start):
    """Scan a ``$(`` body from ``start`` (just past ``$(``) to its matching ``)``.

    Returns ``(body, end)`` — the inner substring and the index just past the
    close — or ``(None, start)`` if no balanced terminator is found. Paren
    nesting, single/double quotes, and backslash escapes inside the body are
    tracked so a ``)`` inside a quoted string or a nested ``(…)``/``$(…)`` does
    not close early. Quote tracking is flat (it does not recurse into nested
    substitutions); on the exotic input where that mis-locates the close, the
    body handed to shlex is unbalanced and analysis defers for it — fail-safe.

    A ``case`` clause is tracked too, because its pattern's ``)`` needs no
    opener: in ``$(case $x in a) cmd;; esac)`` the first ``)`` ends the pattern
    and only the last closes the substitution. Untracked, the body came back as
    ``case $x in`` and nothing the clause ran was ever scanned (Q81). Only bash
    3.2 agrees with that reading; 5.x and zsh run the clause. An odd quote in a
    heredoc body inside the substitution still stops the scan, which is a
    separate mechanism -- Q109. The parenthesised form ``(a)`` already
    worked, since its opener balanced the terminator, which is what kept the gap
    to the bare form.

    ``case`` counts only in command position, so the operand in ``echo case``
    stays an operand -- mistaking one for the keyword would swallow the real
    close and drop a substitution that reads fine today. A ``)`` at depth 0
    still closes whatever the clause state says, which keeps a missed ``esac``
    costing nothing.
    """
    i, n, depth = start, len(text), 0
    in_single = in_double = False
    cmd_pos = True        # bash reads a command just past the `$(`
    clauses = []          # one entry per open `case`: 'in' | 'pat' | 'body'
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
        if c in ' \t\n':
            if c == '\n':
                cmd_pos = True
            i += 1
            continue
        if clauses and clauses[-1] == 'pat':
            m = _WORD_RE.match(text, i)
            if m and m.group(0) == 'esac':        # `case $x in esac` -- no clauses
                clauses.pop()
                cmd_pos = False
                i = m.end()
                continue
            end = _scan_case_pattern(text, i)
            if end is None:
                return (None, start)
            clauses[-1] = 'body'
            cmd_pos = True
            i = end
            continue
        if c == '\\':
            i += 2
            continue
        if c == "'":
            in_single = True
            cmd_pos = False
            i += 1
            continue
        if c == '"':
            in_double = True
            cmd_pos = False
            i += 1
            continue
        if c == ';':
            cmd_pos = True
            if clauses and clauses[-1] == 'body':
                for term in (';;&', ';;', ';&'):  # longest first
                    if text.startswith(term, i):
                        clauses[-1] = 'pat'
                        i += len(term)
                        break
                else:
                    i += 1
                continue
            i += 1
            continue
        m = _WORD_RE.match(text, i)
        if m:
            word = m.group(0)
            state = clauses[-1] if clauses else None
            if state == 'in':
                if word == 'in':
                    clauses[-1] = 'pat'
            elif word == 'esac':
                if state == 'body':
                    clauses.pop()
            elif word == 'case' and cmd_pos:
                clauses.append('in')
            cmd_pos = cmd_pos and word in _CMD_POS_KEYWORDS
            i = m.end()
            continue
        if c == '(':
            depth += 1
            cmd_pos = True
            i += 1
            continue
        if c == ')':
            if depth == 0:
                return (text[start:i], i + 1)
            depth -= 1
            cmd_pos = True
            i += 1
            continue
        cmd_pos = c in '&|{'
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



# ------------------------------------------------------------------- lexing

def lex(text):
    """shlex-tokenize `text` with bash's quoting and operator grouping.

    Raises ValueError on unbalanced quotes; every caller treats that as "defer",
    because a command this function cannot read is one no guard should judge.

    shlex's own comment handling is disabled: it swallows the newline that ends
    a comment (merging the next line into the commented command) and starts a
    comment at a mid-word `#`, which bash does not. `strip_comments` has already
    applied bash's actual rule.
    """
    lx = shlex.shlex(text, posix=True, punctuation_chars=';()<>|&\n')
    lx.whitespace_split = True
    lx.whitespace = lx.whitespace.replace('\n', '')
    lx.commenters = ''
    return list(lx)


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


# -------------------------------------------------- command-head normalising

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
