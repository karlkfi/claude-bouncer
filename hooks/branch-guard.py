#!/usr/bin/env python3
"""branch-guard: a Claude Code PreToolUse hook.

Auto-approves git commits on non-protected branches (e.g. claude/*, feature
branches) and prompts (ask) before commits or file edits that target a
protected branch (main/master). Emits no decision for anything else, so the
normal permission flow applies.

Reads the hook JSON on stdin, emits a PreToolUse decision on stdout. On any
parsing uncertainty (unbalanced quotes, empty input, unresolvable branch) it
defers silently so normal permissions apply — never fail closed.
"""
import sys, os, json, re, shlex, subprocess

PROTECTED_BRANCH_RE = re.compile(r'^(main|master)$')

# POSIX command-prefix assignment (`FOO=bar git commit`): NAME then `=`.
# Bash treats leading assignments as inline env exports; they don't change
# the command name lookup.
ASSIGNMENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')

# Operator-run tokens that separate one simple command from the next.
SEPARATORS = {'|', '||', '&&', '&', ';', '\n', '(', ')'}
# Redirect operators; the following token is a target, not part of a command.
REDIR = {'>', '>>', '<', '<<', '<<<', '>|', '&>', '&>>'}
# Every char shlex treats as punctuation (matches the tokenizer below).
PUNCT_CHARS = frozenset(';()<>|&\n')

# git global options that consume a separate following value token (so the
# subcommand isn't mistaken for the value). `--opt=value` forms are a single
# token and need no entry here.
GIT_VALUE_OPTS = {
    '-C', '-c', '--git-dir', '--work-tree', '--namespace',
    '--super-prefix', '--config-env', '--exec-path',
}

# `git push` options that consume a separate following value token.
PUSH_VALUE_OPTS = {'--repo', '-o', '--push-option', '--receive-pack', '--exec'}
# `git push` flags that push more than the current branch.
PUSH_MANY_FLAGS = {'--all', '--mirror', '--branches'}

# Push-guard policy (env var BRANCH_GUARD_PUSH_POLICY):
#   protected (default) — ask before a push whose target is main/master.
#   strict              — ask before any push that isn't the worktree's own
#                         current branch (also blocks other branches, foreign
#                         refspecs like HEAD:main, and --all/--mirror).
#   off                 — don't guard pushes at all.
PUSH_POLICIES = ('off', 'protected', 'strict')


def split_newline_separators(tokens):
    """Peel newlines out of operator-run tokens so each becomes its own token.

    `\\n` is a punctuation char, so a newline command boundary surfaces as a
    token, but it can glue onto adjacent operators (`;\\n`, `|\\n`). Those
    wouldn't match SEPARATORS, so a newline-only boundary would merge two
    commands. Split applies only to pure operator runs; a quoted filename
    containing a newline is a word token and is left intact.
    """
    out = []
    for t in tokens:
        if t and '\n' in t and all(c in PUNCT_CHARS for c in t):
            out += [p for p in re.split(r'(\n)', t) if p]
        else:
            out.append(t)
    return out


def command_segments(cmd):
    """Tokenize a shell command and split it into simple-command segments.

    Returns a list of token-lists, one per command separated by top-level
    operators (`&&`, `||`, `;`, `|`, `&`, newlines, subshell parens) and with
    redirect targets stripped out. Raises ValueError on unbalanced quotes.
    """
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=';()<>|&\n')
    lex.whitespace_split = True
    lex.whitespace = lex.whitespace.replace('\n', '')
    lex.commenters = ''            # `#` mid-command is not a comment in a shell line
    tokens = split_newline_separators(list(lex))

    segments, cur, i = [], [], 0
    while i < len(tokens):
        t = tokens[i]
        if t in SEPARATORS:
            if cur:
                segments.append(cur)
                cur = []
            i += 1
            continue
        if t in REDIR:
            i += 2 if i + 1 < len(tokens) else 1   # drop operator + its target
            continue
        cur.append(t)
        i += 1
    if cur:
        segments.append(cur)
    return segments


def parse_git(tokens):
    """If a segment is a `git` invocation, return {'sub': <subcommand or None>,
    'args': [tokens after the subcommand]}; otherwise return None. Strips
    leading env assignments and git global options so
    `FOO=bar git -C path -c k=v commit -m x` -> {'sub': 'commit', 'args': ['-m', 'x']}."""
    i = 0
    while i < len(tokens) and ASSIGNMENT_RE.match(tokens[i]):
        i += 1
    if i >= len(tokens) or tokens[i].rsplit('/', 1)[-1] != 'git':
        return None
    i += 1
    while i < len(tokens):
        t = tokens[i]
        if t == '--':
            i += 1
            break
        if not t.startswith('-'):
            break
        i += 2 if t in GIT_VALUE_OPTS else 1
    if i >= len(tokens):
        return {'sub': None, 'args': []}
    return {'sub': tokens[i], 'args': tokens[i + 1:]}


def ref_to_branch(ref, current):
    """Map one side of a push refspec to (branch_name_or_None, is_wildcard).
    `HEAD` -> current branch; `refs/heads/x` -> `x`; an empty side (deletion
    source) or a non-branch ref (`refs/tags/...`) -> None; a `*` glob sets the
    wildcard flag. A bare name is assumed to be a branch (best-effort: it could
    be a tag, but that only ever errs toward asking, never toward allowing)."""
    if ref == '':
        return (None, False)
    if '*' in ref:
        return (None, True)
    if ref == 'HEAD':
        return (current, False)
    if ref.startswith('refs/heads/'):
        return (ref[len('refs/heads/'):], False)
    if ref.startswith('refs/'):
        return (None, False)
    return (ref, False)


def parse_refspec(spec, current, delete):
    """Resolve a refspec to (src_branch, dst_branch, is_wildcard). With
    `--delete`, the token is a destination ref to remove (src is None)."""
    if delete:
        dst_b, glob = ref_to_branch(spec, current)
        return (None, dst_b, glob)
    if spec.startswith('+'):
        spec = spec[1:]
    src_raw, dst_raw = spec.split(':', 1) if ':' in spec else (spec, spec)
    src_b, src_glob = ref_to_branch(src_raw, current)
    dst_b, dst_glob = ref_to_branch(dst_raw, current)
    return (src_b, dst_b, src_glob or dst_glob)


def push_decision(args, current, policy):
    """Given the tokens after `push`, the worktree's current branch, and the
    policy, return an `ask` reason string if the push should be confirmed, or
    None to defer. On parsing uncertainty it leans toward deferring so the
    normal permission flow applies (pair with a pre-push hook / server-side
    branch protection for a hard guarantee)."""
    positionals, many, delete, i = [], False, False, 0
    while i < len(args):
        t = args[i]
        if t == '--':
            positionals += args[i + 1:]
            break
        if t.startswith('-'):
            if t in PUSH_MANY_FLAGS:
                many = True
            if t in ('--delete', '-d'):
                delete = True
            i += 2 if t in PUSH_VALUE_OPTS else 1
            continue
        positionals.append(t)
        i += 1

    if many:
        return "Push targets multiple branches (--all/--mirror) — confirm before proceeding."

    # positionals[0] is the repository; the rest are refspecs. With no refspec,
    # git pushes the current branch to its same-named upstream.
    refspecs = positionals[1:] if positionals else []
    pairs = ([parse_refspec(s, current, delete) for s in refspecs]
             if refspecs else [(current, current, False)])

    for src_b, dst_b, glob in pairs:
        if glob:
            return "Push uses a wildcard refspec (multiple branches) — confirm before proceeding."
        if dst_b and is_protected(dst_b):
            return f"Push targets protected branch '{dst_b}' — confirm before proceeding."
        if policy == 'strict':
            if dst_b is not None and dst_b != current:
                return (f"Push targets '{dst_b}', not the worktree branch "
                        f"'{current}' — confirm before proceeding.")
            if src_b is not None and src_b != current:
                return (f"Push sends local branch '{src_b}', not the worktree "
                        f"branch '{current}' — confirm before proceeding.")
    return None


def push_policy():
    """Read BRANCH_GUARD_PUSH_POLICY; default and fall back to 'protected'."""
    v = (os.environ.get('BRANCH_GUARD_PUSH_POLICY') or 'protected').strip().lower()
    return v if v in PUSH_POLICIES else 'protected'


def current_branch(cwd):
    """Current branch via `git -C <cwd> rev-parse --abbrev-ref HEAD`, or None
    if the directory isn't a repo / git is unavailable / HEAD won't resolve."""
    try:
        r = subprocess.run(
            ['git', '-C', cwd, 'rev-parse', '--abbrev-ref', 'HEAD'],
            capture_output=True, text=True,
        )
    except OSError:
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def is_protected(branch):
    return bool(PROTECTED_BRANCH_RE.match(branch))


def emit(decision, reason):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason,
    }}))


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return                                     # unparseable input -> defer
    if not isinstance(data, dict):
        return

    tool = data.get('tool_name') or ''
    tool_input = data.get('tool_input') or {}

    if tool == 'Bash':
        cmd = tool_input.get('command') or ''
        if not cmd.strip():
            return
        try:
            segments = command_segments(cmd)
        except ValueError:
            return                                 # unbalanced quotes -> defer
        parsed = [parse_git(seg) for seg in segments]
        has_commit = any(p and p['sub'] == 'commit' for p in parsed)
        policy = push_policy()
        push_segs = ([p for p in parsed if p and p['sub'] == 'push']
                     if policy != 'off' else [])
        if not has_commit and not push_segs:
            return                                 # nothing we guard -> defer

        branch = current_branch(data.get('cwd') or os.getcwd())
        if branch is None:
            return                                 # not a repo / detached -> defer

        # Push guard takes priority: a chain containing a push is never
        # auto-approved via the commit path (that would auto-approve the push).
        if push_segs:
            for p in push_segs:
                reason = push_decision(p['args'], branch, policy)
                if reason:
                    emit('ask', reason)
                    return
            return                                 # push allowed by policy -> defer

        # Commit guard.
        if is_protected(branch):
            emit('ask', f"Targets protected branch '{branch}' — confirm before proceeding.")
            return
        # Non-protected branch: auto-approve only when EVERY segment is a git
        # invocation (e.g. `git add -A && git commit -m x`). A chain that mixes
        # in a non-git command (`git commit && rm -rf ~`) is deferred to the
        # normal permission prompt rather than silently auto-approved.
        if all(p is not None for p in parsed):
            emit('allow', f"Commit on non-protected branch '{branch}' — auto-approved.")
        return

    if tool in ('Edit', 'Write', 'MultiEdit'):
        file_path = tool_input.get('file_path') or ''
        if not file_path:
            return
        branch = current_branch(os.path.dirname(file_path) or '.')
        if branch is None:
            return
        if is_protected(branch):
            emit('ask', f"Targets protected branch '{branch}' — confirm before proceeding.")
        return

    # Any other tool -> defer.


if __name__ == '__main__':
    main()
