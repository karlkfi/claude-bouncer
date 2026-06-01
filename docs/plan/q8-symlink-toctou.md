# Q8 — Symlink TOCTOU within a chained command

## Problem

The hook treats a relative path as in-workspace when its lexical realpath stays
inside `$CLAUDE_PROJECT_DIR`. For paths that don't yet exist on disk, `realpath`
falls back to lexical normalization — no symlink resolution happens.

That gives an attacker (or a confused model) a one-shot bypass in a chained
command:

```
ln -s /etc/passwd link && cat link
```

At hook time `link` doesn't exist, so `os.path.realpath("<cwd>/link")` returns
`<cwd>/link` — inside the workspace — and the whole chain is `allow`ed. Bash
then runs the `ln`, materialising the workspace-pointing-out symlink, and
`cat link` exfiltrates `/etc/passwd`.

Verified on `main` at the time this plan was written:

```
$ echo '{"tool_input":{"command":"ln -s /etc/passwd link && cat link"},"cwd":"…"}' \
    | CLAUDE_PROJECT_DIR=… python3 scripts/bash-workspace-guard.py
{"hookSpecificOutput": {"…", "permissionDecision": "allow", …}}
```

## Approach

**Stage symlink intent across groups.** When an earlier group is `ln -s TARGET
LINK` (or `ln -s TARGET` with `LINK` omitted) and `TARGET` resolves outside the
workspace, record `LINK`'s resolved path as a *staged outside symlink*. Later
guarded groups whose file argument resolves to a staged path are flagged as
outside.

This is option A from the Q8 queue note. Rejected alternatives:

- **Option B — taint any guarded group after an `ln -s`.** Simpler but produces
  false positives for innocuous in-workspace symlinks (`ln -s ./a ./b && cat
  ./b`). The hook should add friction at the boundary, not at every link.
- **Option C — guard `ln` itself as a SPEC entry and check both positionals.**
  Would catch the same attack via `ask` on the `ln` group, but conflates Q8
  with Q11 (write-command guarding), which has its own threat model and plan.
  Better to stay narrowly scoped.

## Scope

In scope:

- `ln -s TARGET LINK` and `ln -s TARGET` (two- and one-positional forms).
- Symbolic mode detected via `-s`, combined short flags containing `s`
  (`-sf`, `-fs`, `-fns`), and `--symbolic`.
- `TARGET` resolution honours the same rules as `check_file`: absolute,
  relative-to-group-cwd, `~`/`$`-expansion treated as outside (secure-by-default),
  `cd`-shifted cwd (Q7), and `group_cwd_unknown` short-circuit.
- `LINK` omitted → use `basename(TARGET)` as the link name in the current
  group cwd.

Out of scope (document as limitations / follow-up Q-items):

- **Hard links** (`ln SRC LINK` without `-s`). Same TOCTOU shape on a single
  filesystem; defer to a follow-up because the queue text scopes Q8 to `ln -s`
  and adding hard-link coverage doubles the test surface.
- **Multi-source form** (`ln -s a b c destdir/`). Would require staging
  `destdir/basename(a)` for each source. Real-world usage by Claude is rare;
  if observed, file as a follow-up.
- **`-t DIR` / `--target-directory=DIR`.** Same multi-target shape as above.
- **`cp -s` (symbolic copy).** A different command entirely; covered by Q11.

## Implementation sketch

In `scripts/bash-workspace-guard.py`:

1. Add `classify_ln(tokens)` returning `(target_token, link_token_or_None)`
   for symbolic-mode `ln` with 1 or 2 positionals, else `None`. Recognise
   `-s` in combined short flags and `--symbolic` long form. Consume the value
   flags `-t/--target-directory` and `-S/--suffix` so they don't get picked up
   as positionals.
2. In `main()`, between `classify_cd` and `files_in_command`, call
   `classify_ln`. If it matches, resolve the target the same way `check_file`
   does and, if outside-workspace, resolve the link path and add it to a
   `staged_outside_paths: set[str]`.
3. Extend `check_file` so that after computing `rp`, it returns the original
   token whenever `rp in staged_outside_paths` (in addition to the existing
   workspace-boundary check).
4. The set persists across groups within one hook invocation; nothing crosses
   invocations (each Bash call gets a fresh hook process).

## Acceptance

- `ln -s /etc/passwd link && cat link` → **ask**, citing `link`.
- `ln -s ./a ./b && cat ./b` → **allow** (innocent in-workspace symlink).
- `ln -s /etc/passwd` (LINK omitted) → stages `<cwd>/passwd`; a follow-up
  `cat passwd` in the same chain asks.
- `ln -s /etc/passwd /tmp/x && cat /tmp/x` → **ask** (the `cat` already
  catches `/tmp/x` as absolute-outside; staging is a no-op here).
- `ln -s ./local /tmp/x && cat /tmp/x` → **ask** (`/tmp/x` is absolute-outside).
- `cd /tmp && ln -s /etc/passwd link && cat link` → **ask** (`cd`-shifted
  cwd; staging resolves `link` against `/tmp`).
- All existing tests continue to pass.

## Known gaps after this lands

- Hard-link form (`ln SRC LINK`) — file as follow-up Q-item if not already
  covered by Q11.
- Multi-source form (`ln -s a b destdir/`).
- Cross-invocation staging is unnecessary: a separate hook fire re-runs
  `realpath`, which will now follow the materialised symlink and resolve
  outside the workspace correctly.
