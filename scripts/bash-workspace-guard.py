#!/usr/bin/env python3
"""PreToolUse hook: prompt (ask) when a guarded command targets a file
outside the workspace; allow when it only touches workspace files or pipes.

Reads the hook JSON on stdin, emits a PreToolUse decision on stdout.
"""
import sys, os, json, shlex

# Command separators and redirect operators (after shlex punctuation grouping).
SEPARATORS = {'|', '||', '&&', '&', ';', '\n', '(', ')'}
REDIR = {'>', '>>', '<', '<<', '<<<', '>|', '&>', '&>>'}

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
    'cat':  {'consume':{}, 'file_flags':{}, 'prog':0},
    'head': {'consume':{'-n':1,'-c':1,'--lines':1,'--bytes':1},'file_flags':{},'prog':0},
    'tail': {'consume':{'-n':1,'-c':1,'--lines':1,'--bytes':1},'file_flags':{},'prog':0},
}
ALIASES = {'egrep':'grep','fgrep':'grep','rg':'grep','gawk':'awk','mawk':'awk'}


def split_eq(tok):
    """--opt=val -> ('--opt','val'); otherwise (tok, None)."""
    if tok.startswith('--') and '=' in tok:
        k, v = tok.split('=', 1)
        return k, v
    return tok, None


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
                redir_files.append(tokens[i+1]); i += 2; continue
            i += 1; continue
        cur.append(t); i += 1
    if cur: groups.append(cur)

    candidates, guarded = list(redir_files), False
    for g in groups:
        if not g: continue
        fs = files_in_command(g)
        if fs is None: continue
        guarded = True
        candidates += fs
    if not guarded:
        return                                    # no guarded command -> defer

    outside = []
    for f in candidates:
        if not f or f == '-' or f.startswith('-'):
            continue
        path = f if os.path.isabs(f) else os.path.join(cwd, f)
        rp = os.path.realpath(path)
        if rp != proj and not rp.startswith(proj + os.sep):
            outside.append(f)

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
