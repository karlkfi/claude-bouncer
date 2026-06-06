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


def git_subcommand(tokens):
    """If a segment is a `git` invocation, return its subcommand (e.g.
    'commit'); otherwise return None. Strips leading env assignments and git
    global options so `FOO=bar git -C path -c k=v commit` -> 'commit'."""
    i = 0
    while i < len(tokens) and ASSIGNMENT_RE.match(tokens[i]):
        i += 1
    if i >= len(tokens):
        return None
    if tokens[i].rsplit('/', 1)[-1] != 'git':
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
    return tokens[i] if i < len(tokens) else None


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
        subcommands = [git_subcommand(seg) for seg in segments]
        if 'commit' not in subcommands:
            return                                 # not a git commit -> defer

        branch = current_branch(data.get('cwd') or os.getcwd())
        if branch is None:
            return                                 # not a repo / detached -> defer
        if is_protected(branch):
            emit('ask', f"Targets protected branch '{branch}' — confirm before proceeding.")
            return
        # Non-protected branch: auto-approve only when EVERY segment is a git
        # invocation (e.g. `git add -A && git commit -m x`). A chain that mixes
        # in a non-git command (`git commit && rm -rf ~`) is deferred to the
        # normal permission prompt rather than silently auto-approved.
        if all(sub is not None for sub in subcommands):
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
