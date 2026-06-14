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

# Every char shlex treats as punctuation (see `punctuation_chars` in main).
# A token built only from these is an operator run; anything else is a word
# (so a quoted filename containing one of these survives normalization).
PUNCT_CHARS = frozenset(';()<>|&\n')

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


def split_newline_separators(tokens):
    """Peel newlines out of operator-run tokens so each becomes its own token.

    With `\\n` in shlex's `punctuation_chars` it is emitted (not swallowed as
    whitespace), but it glues onto adjacent operators: `cmd1;\\ncmd2` tokenizes
    `;\\n`, `cmd1 |\\ncmd2` tokenizes `|\\n`, a blank line tokenizes `\\n\\n`.
    None of those match `SEPARATORS`, so a newline-only command boundary would
    be missed and the two commands would merge into one group — the very bug
    this guards against (false positives from extra tokens read as file args,
    and a false negative when a guarded command trails an unguarded one).

    Splitting is applied ONLY to pure operator runs (every char in
    `PUNCT_CHARS`); a quoted filename that happens to contain a newline is a
    word token with non-punctuation chars and is left intact. The non-newline
    chunks are whatever shlex already grouped (`;`, `|`, `&>`, ...), so they
    stay valid `SEPARATORS`/`REDIR` entries.
    """
    out = []
    for t in tokens:
        if t and '\n' in t and all(c in PUNCT_CHARS for c in t):
            out += [p for p in re.split(r'(\n)', t) if p]
        else:
            out.append(t)
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
    contains `$`, so only the deterministic, fully-resolvable cases are expanded
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


def build_reason(offenders):
    """Build the permissionDecisionReason for a blocked command.

    `offenders` is a list of `(token, category)` pairs from `check_file`.
    The message names the offending token(s) AND tells the agent how to avoid
    the prompt, tailored per category so each gets the fix that applies:

      * 'outside'   — a path that genuinely resolves outside the project root.
      * 'expand'    — a `~`/`$VAR`/`$(...)` token bash expands at runtime; the
                      hook can't see where it lands, so it may in fact be
                      in-root and fixable by writing a literal path.
      * 'untracked' — a relative path after a `cd` the hook couldn't follow.

    Categories are emitted in a stable order; tokens within each are sorted and
    de-duplicated.
    """
    buckets = {'outside': [], 'expand': [], 'untracked': []}
    for tok, cat in offenders:
        buckets[cat].append(tok)

    hints = []
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
            "path (drop the $VAR / $(...) / leading ~); otherwise use the "
            "Read/Grep tools.")
    if buckets['untracked']:
        hints.append(
            "Relative path(s) after an untracked cd: "
            + ", ".join(sorted(set(buckets['untracked'])))
            + ". Fix: avoid cd outside the root and bare cd / cd - / cd $HOME; "
            "pass an in-root path or use the Read/Grep tools.")
    return " ".join(hints)


def main():
    data = json.load(sys.stdin)
    cmd = (data.get('tool_input') or {}).get('command', '') or ''
    cwd = data.get('cwd') or os.getcwd()
    proj = os.path.realpath(os.environ.get('CLAUDE_PROJECT_DIR') or cwd)
    if not cmd.strip():
        return

    try:
        # `\n` is a punctuation char so a newline command boundary surfaces as
        # a token (it is otherwise eaten as whitespace, merging the commands on
        # either side). Removing it from `whitespace` stops shlex re-swallowing
        # it; quoted newlines stay inside their word token regardless. The runs
        # this produces (`;\n`, `|\n`, ...) are split back apart below.
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=';()<>|&\n')
        lex.whitespace_split = True
        lex.whitespace = lex.whitespace.replace('\n', '')
        tokens = split_newline_separators(list(lex))
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
        f = expand_tilde(f)
        if f.startswith('~') or '$' in f:
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

    def check_file(f, group_cwd, group_cwd_unknown):
        """Return `(token, category)` if the file resolves outside the
        workspace (directly, or via a link staged by an earlier `ln` —
        symbolic or hard — in this chain), else None.

        `category` is one of 'outside' (a resolved path outside the root),
        'expand' (a runtime-expanded `~`/`$` token), or 'untracked' (a
        relative path after a `cd` we couldn't follow). All three block
        identically; the category only steers the advice in the reason."""
        kind, rp = resolve_token(f, group_cwd, group_cwd_unknown)
        if kind == 'skip':
            return None
        if kind in ('expand', 'untracked'):
            return (f, kind)
        if rp in staged_outside_paths or is_outside(rp):
            return (f, 'outside')
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
        # In `bypassPermissions` / full-auto runs there is no human to answer an
        # `ask`. Verified behavior (CLI 2.1.159): `ask` still *blocks* there, but
        # only feeds the model an unanswerable approval prompt it stalls on.
        # `deny` blocks identically *and* feeds the reason back, so the model can
        # route around the outside path instead of stalling. Interactive/headless
        # `default` mode keeps `ask` so a human still gets the approve/reject
        # prompt. Both decisions are equally blocking — this is a recoverability
        # choice, not a weakening of the boundary. `default` is indistinguishable
        # from interactive at the hook, so `bypassPermissions` is the only clean
        # "no human" signal we can act on. (Q17)
        block = "deny" if data.get("permission_mode") == "bypassPermissions" else "ask"
        decision, reason = block, build_reason(outside)
    else:
        decision, reason = "allow", "Guarded commands target workspace/pipe only"
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason}}))


if __name__ == "__main__":
    main()
