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

And two bash-isms zsh evaluates differently, which is a neighbouring failure
rather than a fourth way of losing a status -- the status is honest, and it
answers a question nobody asked:

  4. `$PIPESTATUS`, which zsh does not have. It expands to empty, so the test
     against it reads as success whatever the pipeline did.
  5. `$name:` followed by a modifier character. zsh reads `:s`/`:h`/`:t` and ten
     others as history modifiers, and `:f`/`:g`/`:w`/`:F` iterate whichever one
     follows them -- so `git show $ref:tests/x` and
     `git show $ref:frontend/app.tsx` both run `git show origin/main`, a valid
     command printing the wrong object at exit 0. Braces are the only fix;
     double-quoting is not.

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

# The parsing primitives every claude-bouncer guard shares. The copy under this
# plugin's `lib/` is vendored from the repository root; see scripts/sync-lib.py.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib'))
from bouncer_parse import (                                    # noqa: E402
    ASSIGNMENT_RE, CHAIN_OPS, COMMENT_PRECEDERS, DUP, END_OPS, MAX_SUBST_DEPTH,
    PIPE_OPS, PUNCT_CHARS, REDIR, SEPARATORS, SH_KEYWORDS, _OPERATORS,
    _consume_heredoc_body, _scan_backticks, _scan_dollar_paren,
    _skip_balanced_parens, command_substitutions, glue_dollar_paren,
    split_operator_runs, strip_comments, strip_env_prefix,
    strip_heredoc_bodies, strip_sh_keywords,
)

# --- Ported from claude-workspace-guard (scripts/bash-workspace-guard.py) ----
# These carry the quote-state tracking and bracket counting. Kept structurally
# identical to their source so a fix there transfers by inspection.

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
        self.restores = self._compile(data.get('restores') or [])

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

    def is_restore(self, words):
        """Whether this state change reverts local state rather than publishing.

        Read only after `is_mutator` has already said yes, so the screens have
        run; what this adds is the direction of the change. `git reset --hard`
        undoes, `git push` announces.
        """
        return self._matches(self.restores, words)


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
        for key in ('gates', 'exempt', 'mutators', 'restores'):
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


# zsh's modifiers, restricted to the ones that bind to an UNBRACED `$name:`.
#
# Measured against zsh 5.9 over all 52x52 two-letter openers, each case a fresh
# `zsh -c` on literal script text -- no `eval`, which adds an expansion pass and
# does not describe what a typed command does. The braced form is the control:
# it is what the author meant, so a difference is the modifier firing. Both
# earlier cuts of this rule were measured through `eval` and both were wrong,
# in opposite directions.
#
# The partition is 13 + 4 + 35 = 52, and `tests/test_exit_status_guard.py`
# carries the table and asserts every pair against it. The three groups are the
# two frozensets below and the 35 letters in neither.
#
# bash has no modifiers at all, so every match here is a `:` the author wrote
# as a separator and zsh reads as an operator.

# Diverge whatever follows: `$ref:tests/x` is already broken at the `t`.
ZSH_MODIFIERS = frozenset('acehlqrstuAPQ')

# Iteration modifiers: inert alone, and they apply the modifier that FOLLOWS
# them. `$ref:frontend/app.tsx` is `f` iterating `r` and expands to
# `origin/mainontend/app.tsx`, while `$ref:foo/bar` is correct because `o` is
# not a modifier. A measurement that puts a non-modifier spacer after each
# candidate letter reports all four as literal -- the spacer blocks the
# iteration it was meant to isolate -- which is how the first cut missed them.
#
# `W` is deliberately NOT here: it diverges on no second letter at all, so
# including it would deny thirteen legitimate shapes. `F` additionally diverges
# on `g`, `o` and `x`, because it takes a numeric argument and errors on a
# non-numeric one; those three are left uncovered and are LOUD
# (`bad math expression`), and modelling zsh's argument grammar to reach them
# is the hand-rolled shell parsing this guard is not allowed to grow.
ZSH_ITERATORS = frozenset('fgwF')

VAR_NAME_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')

# Where the swallowed word ends. Whitespace and the shell metacharacters that
# would end a word anyway; the quote characters are in here because the scanner
# tracks them itself and must not run past one.
WORD_END = frozenset(' \t\n;|&()<>"\'')


def modifier_swallow(text, quotes=True):
    """The `$name:...` word whose colon zsh eats as a modifier, or ''.

    Scans the raw string with quote state, for the reason `reads_var` does:
    single quotes suppress expansion, so `'$v:x'` is literal text, while double
    quotes suppress nothing -- which is what makes this survive the defensive
    reflex of quoting it. A braced `${name}:x` or `${name:h}` is unambiguous in
    either shell and never matches, because the brace is the fix and so cannot
    also be the bug.
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
            m = VAR_NAME_RE.match(text, i + 1)
            # `${...}` fails the match on the brace, which is the whole screen.
            if m and m.end() < n and text[m.end()] == ':':
                nxt = m.end() + 1
                first = text[nxt] if nxt < n else ''
                second = text[nxt + 1] if nxt + 1 < n else ''
                if first in ZSH_MODIFIERS or (
                        first in ZSH_ITERATORS and second in ZSH_MODIFIERS):
                    end = nxt
                    while end < n and text[end] not in WORD_END:
                        end += 1
                    return text[i:end]
                i = m.end() + 1
                continue
        i += 1
    return ''


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


# A segment that is nothing but `NAME=$?`. Requiring the whole segment is what
# separates a capture from an inline assignment: `rc=$? make check` sets `rc`
# for the duration of one command and leaves nothing a later segment can read.
CAPTURE_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)=\$\?$')


def status_captured(segs, gate_at, mutator_at):
    """The variable a `$?` capture between the gate and the mutator names, or ''.

    Top-level only: a capture inside a subshell dies with it, so `$rc` outside is
    empty and the recheck it appears to feed tests nothing.
    """
    for seg in segs[gate_at + 1:mutator_at]:
        if seg.depth != 0 or len(seg.tokens) != 1:
            continue
        m = CAPTURE_RE.match(seg.tokens[0])
        if m:
            return m.group(1)
    return ''


def status_rechecked(segs, mutator_at, var):
    """Whether any segment after the mutator reads ``var``.

    Reading is enough, the same way a foreground `echo "EXIT=$?"` is: the status
    reaches somewhere it can be acted on. shlex has already removed the quoting,
    so the tokens are scanned as plain text.
    """
    return any(reads_var(' '.join(seg.tokens), var, quotes=False)
               for seg in segs[mutator_at + 1:])


def restore_form(segs, reg, words, gate_at, mutator_at):
    """Whether this mutator is the capture-and-restore rewrite SEQUENCED_REASON
    hands over, rather than a state change the gate should have gated.

    `cmd > <LOG> 2>&1; rc=$?; restore; [ "$rc" -ne 0 ] || exit 1` is what the
    deny recommends for a gate whose failure is the assertion, and a restore
    that is itself a registry mutator -- `git reset --hard`, `kubectl delete` --
    tripped rule 3 on the way back, so the suggestion was denied a second time
    (Q106). All three conditions are required. Dropping the restore screen would
    silence `make check > log; rc=$?; git push; [ "$rc" -ne 0 ] || exit 1`, and
    capturing a status does not make a push conditional on it.
    """
    if not reg.is_restore(words):
        return False
    var = status_captured(segs, gate_at, mutator_at)
    return bool(var) and status_rechecked(segs, mutator_at, var)


def sequenced_mutation(segs, reg):
    """(gate, mutator) when a gate is sequenced before a state-changing command
    with `;` rather than `&&`, or ('', '').

    `make check; git push` reads the gate's status correctly and then ignores
    it: the push runs whatever the check did. Only top-level segments count --
    inside a subshell the sequence is that subshell's own business -- and only a
    `;`/newline separator, since `&&` is the form that already gates.

    A restore in the capture-and-recheck form is skipped rather than returned,
    so a publish later in the same command is still caught: in
    `make check > log; rc=$?; git reset --hard; git push; [ "$rc" -ne 0 ]` the
    reset is the recommended rewrite and the push is the defect. The skip is a
    `continue` rather than a fall-through, because most restores are gates too
    -- letting one become the gate would restart the capture search after it,
    and a two-step teardown would then deny on its second step.
    """
    gate, gate_at = '', -1
    for i, seg in enumerate(segs):
        if seg.depth != 0:
            continue
        words = head_words(seg)
        if gate_at >= 0 and i > gate_at and reg.is_mutator(words):
            if not restore_form(segs, reg, words, gate_at, i):
                return gate, ' '.join(words)
            continue
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


# The opener every deny carries. What reaches the model is this string and
# nothing else -- the hook's name is in the transcript, but never in front of
# the thing doing the rewrite -- so the reason is the only place a verdict can
# say who issued it. `<name>-guard: ` is the form the sibling guards use.
REASON_PREFIX = 'exit-status-guard: '

# The tail every reason ends with. Every rule here is about a status nobody
# will read, so the sentence before it is the same one throughout.
OVERRIDE_TAIL = (
    " If this call is not the mistake the rule describes, that is a defect in "
    "the rule: report it at https://github.com/karlkfi/claude-bouncer/issues "
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

ZSH_MODIFIER_REASON = (
    " -- zsh, the shell the Bash tool runs, reads that `:` as a history "
    "modifier rather than as a separator (`:s` substitutes, `:h` is dirname, "
    "`:t` basename, `:r` strips the extension), so the colon and everything "
    "after it are consumed and the command runs against the variable's bare "
    "value. It usually SUCCEEDS at that: `git show $ref:path | wc -l` prints "
    "the commit instead of the blob, well-formed and at exit 0. Where the "
    "modifier does not parse you get `bad substitution` instead, which is the "
    "same bug arriving loudly. bash has no "
    "modifiers, so this is a zsh-only reading, and double-quoting does not "
    "rescue it -- only braces do. Write `${name}:rest` for a literal colon, or "
    "`${name:h}` if the modifier was what you meant."
    # Not OVERRIDE_HINT: its sentence is about a status that does not matter,
    # which is the wrong question here and the exact defect Q106 was about.
    " If the modifier is deliberate and the braced form will not express it, "
    "re-run prefixed with " + OVERRIDE_VAR + "=<reason>." + OVERRIDE_TAIL)

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
    "check passing. Where the second command has to run even when the first "
    "fails -- a restore after a gate whose failure is the assertion -- capture "
    "the status and check it afterwards: "
    '<MKDIR>cmd > <LOG> 2>&1; rc=$?; restore; [ "$rc" -ne 0 ] || exit 1.'
    + OVERRIDE_HINT)


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

    # The same class as $PIPESTATUS and read the same way: bash syntax that zsh
    # evaluates differently, yielding a plausible answer to a question nobody
    # asked. Unconditional, gate or no gate -- there is no reading of
    # `$ref:path` under which the swallowed colon was wanted.
    word = modifier_swallow(cleaned)
    for body in heredocs:
        word = word or modifier_swallow(body, quotes=False)
    if word:
        return '`' + truncate(word) + '`' + ZSH_MODIFIER_REASON

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
        return with_log_path('`' + truncate(gate) + SEQUENCED_REASON_HEAD
                             + truncate(mutator) + SEQUENCED_REASON, scratch)

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
        emit('deny', REASON_PREFIX + reason)


if __name__ == '__main__':
    main()
