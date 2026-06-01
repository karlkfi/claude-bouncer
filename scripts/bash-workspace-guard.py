#!/usr/bin/env python3
"""PreToolUse hook: prompt (ask) when a guarded command targets a file
outside the workspace; allow when it only touches workspace files or pipes.

Reads the hook JSON on stdin, emits a PreToolUse decision on stdout.
"""
import sys, os, json, re, shlex

# POSIX command-prefix assignment: NAME starts with letter/underscore,
# followed by letters/digits/underscores, then `=`. Anything after the `=`
# (including empty) is the value. Bash treats one or more of these tokens
# at the start of a simple command as inline env exports for that command;
# they do not change the command name lookup.
ASSIGNMENT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')

# Command separators and redirect operators (after shlex punctuation grouping).
SEPARATORS = {'|', '||', '&&', '&', ';', '\n', '(', ')'}
REDIR = {'>', '>>', '<', '<<', '<<<', '>|', '&>', '&>>'}

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


def split_eq(tok):
    """--opt=val -> ('--opt','val'); otherwise (tok, None)."""
    if tok.startswith('--') and '=' in tok:
        k, v = tok.split('=', 1)
        return k, v
    return tok, None


def classify_ln(tokens):
    """For an `ln -s ...` command, return `(target_token, link_token_or_None)`.

    Returns None when the command isn't `ln`, isn't in symbolic mode, or uses
    the multi-source form (3+ positionals — `ln -s a b destdir/`), which the
    Q8 staging logic deliberately doesn't track.

    Recognises symbolic mode via `-s`, combined short flags containing `s`
    (`-sf`, `-fs`, `-fns`), and `--symbolic`. Consumes the value-taking flags
    (`-t`/`--target-directory`, `-S`/`--suffix`, `--backup`) so they don't
    surface as positionals.
    """
    if not tokens or os.path.basename(tokens[0]) != 'ln':
        return None
    consume = {'-t': 1, '--target-directory': 1,
               '-S': 1, '--suffix': 1, '--backup': 1}
    symbolic = False
    positionals = []
    i, n, end_opts = 1, len(tokens), False
    while i < n:
        tok = tokens[i]
        if not end_opts and tok == '--':
            end_opts = True; i += 1; continue
        if not end_opts and tok.startswith('-') and tok != '-':
            key, inline = split_eq(tok)
            if key == '--symbolic':
                symbolic = True
            elif not key.startswith('--') and 's' in key[1:]:
                symbolic = True
            if key in consume:
                i += 1 + (0 if inline is not None else consume[key]); continue
            i += 1; continue
        positionals.append(tok); i += 1
    if not symbolic:
        return None
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


def classify_cd(tokens):
    """Classify a command group as a cwd-shifting builtin.

    Returns:
      ('arg', path)      — cd/pushd with a resolvable positional path
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
        if t.startswith('+') or t.startswith('~') or '$' in t:
            return ('unknown', None)
        return ('arg', t)
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


def main():
    data = json.load(sys.stdin)
    cmd = (data.get('tool_input') or {}).get('command', '') or ''
    cwd = data.get('cwd') or os.getcwd()
    proj = os.path.realpath(os.environ.get('CLAUDE_PROJECT_DIR') or cwd)
    if not cmd.strip():
        return

    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=';()<>|&')
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return                                    # unbalanced quotes -> defer

    groups, cur, redir_files, i = [], [], [], 0
    while i < len(tokens):
        t = tokens[i]
        if t in SEPARATORS:
            if cur: groups.append(cur); cur = []
            i += 1; continue
        if t in REDIR:
            if i + 1 < len(tokens):
                # `<<TAG` heredoc delimiter and `<<<STR` here-string content
                # are not file paths — skip without adding to redir_files.
                if t in ('<<', '<<<'):
                    i += 2; continue
                redir_files.append(tokens[i+1]); i += 2; continue
            i += 1; continue
        cur.append(t); i += 1
    if cur: groups.append(cur)

    def is_outside(rp):
        return rp != proj and not rp.startswith(proj + os.sep)

    def resolve_token(f, group_cwd, group_cwd_unknown):
        """Resolve a file token. Returns one of:
          ('skip', None)         — '-', flag, or allowlisted device
          ('outside', None)      — runtime-expanded (`~`/`$`) or cwd is
                                   unknown; secure-by-default outside
          ('path', abspath)      — caller compares against the workspace and
                                   the staged-outside set
        """
        if not f or f == '-' or f.startswith('-'):
            return ('skip', None)
        if is_allowed_device(f):
            return ('skip', None)
        # Bash expands `~` and `$VAR` at runtime; shlex leaves them literal.
        if f.startswith('~') or '$' in f:
            return ('outside', None)
        if os.path.isabs(f):
            return ('path', os.path.realpath(f))
        if group_cwd_unknown:
            return ('outside', None)
        return ('path', os.path.realpath(os.path.join(group_cwd, f)))

    # Symlinks staged by an earlier `ln -s OUTSIDE LINK` in the same chain.
    # Tracks the resolved abspath of each `LINK` whose target is outside the
    # workspace, so a later `cat link` can be flagged before bash materialises
    # the symlink and breaks the lexical-realpath check (Q8).
    staged_outside_paths = set()

    def check_file(f, group_cwd, group_cwd_unknown):
        """Return the original token if it resolves outside the workspace
        (directly, or via a symlink staged by an earlier `ln -s` in this
        chain), else None."""
        kind, rp = resolve_token(f, group_cwd, group_cwd_unknown)
        if kind == 'skip':
            return None
        if kind == 'outside':
            return f
        if rp in staged_outside_paths or is_outside(rp):
            return f
        return None

    def stage_ln(target, link, group_cwd, group_cwd_unknown):
        """If `ln -s TARGET LINK` points outside, record LINK's resolved path.
        LINK may be None (omitted) — then the link name is `basename(TARGET)`
        in the current group cwd, matching POSIX `ln` semantics."""
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
    for g in groups:
        if not g: continue
        g = strip_env_prefix(g)
        if not g: continue                        # group was env-only (no cmd)
        kind, arg = classify_cd(g)
        if kind is not None:
            if kind == 'arg':
                new_cwd = arg if os.path.isabs(arg) else os.path.join(group_cwd, arg)
                group_cwd = os.path.realpath(new_cwd)
                group_cwd_unknown = False
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
        for f in fs:
            o = check_file(f, group_cwd, group_cwd_unknown)
            if o is not None:
                outside.append(o)
    if not guarded:
        return                                    # no guarded command -> defer

    # Redirects are collected at the top level (not associated with a group),
    # so resolve them against the original cwd — they don't track cd-shifts.
    for f in redir_files:
        o = check_file(f, cwd, False)
        if o is not None:
            outside.append(o)

    if outside:
        decision, reason = "ask", "Outside-workspace path(s): " + ", ".join(sorted(set(outside)))
    else:
        decision, reason = "allow", "Guarded commands target workspace/pipe only"
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason}}))


if __name__ == "__main__":
    main()
