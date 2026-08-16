# Judge an unlink by the link, not by what it points at

**Status: done.** All eight rows below now match, with the three controls
unmoved.

## Goal

Stop the sibling-checkout rule denying `rm <symlink>` because the link's target
sits in another checkout, without weakening the wrong-branch protection it
exists for.

## Approach

The rule reads one resolved path at two ends, and both ends are wrong at the
edges:

1. Every file operand is `realpath`'d before any checkout comparison exists, so
   an operand that *is* a symlink gets judged by its target. `rm link` unlinks
   the link and cannot write the target.
2. `sibling_checkout_for` walks up from `os.path.dirname(rp)`, so a path that
   *is* a checkout root looks one level too high and escapes the rule.

Fixing either alone makes the other worse, so both land here. Split file
operands by what the command does to them, and start the checkout walk at the
path itself unless its final component is a link.

## Measured before the change

Installed 1.9.0 (byte-identical to the `v1.9.0` tag) and `main` at e359215,
same output on both:

```
over-block: an operand that is itself a link
deny   rm <live link into main>        want ask
deny   rm <dangling link into main>    want ask
deny   mv <live link> <newname>        want ask

under-block: the operand is a checkout root
ask    rm -rf <main checkout>          want deny
ask    rm -rf <another session's wt>   want deny

controls, must not move
ask    rm <plain file outside>         ask
deny   rm <dir link>/root.txt          deny
deny   rm <main>/root.txt              deny
```

## Changes

1. Extract `_split_args` from `files_in_command` so a second caller can see
   where flag values end and positionals begin, without duplicating the loop.
2. `ENTRY_OPERANDS` below the row table, next to `OUTPUT_POSITIONALS` (same
   shape, same reason for living there): `rm` marks every file operand,
   `mv` marks all but the destination.
3. `entry_operand_mask(tokens)` returns booleans parallel to
   `files_in_command(tokens)`.
4. `entry_realpath(p)` resolves the parent and keeps the final component.
   Identical to `realpath` when that component is not a symlink.
5. `resolve_token` and `check_file` take `entry=False` and thread it through.
6. `sibling_checkout_for` walks from `rp`, or from `dirname(rp)` when `rp` is a
   symlink.

## Why the scoping is load-bearing

Entry semantics must not reach reads. `claude_code_dirs()` exempts
`~/.claude/skills/` for reads and its docstring is explicit that the exemption
keys on where a file really is, so an exempt directory cannot launder a symlink
into one. Stop resolving the final component for reads and
`cat ~/.claude/skills/x`, where `x` links anywhere at all, becomes an `allow`.
`ENTRY_OPERANDS` names two write commands, and nothing consults it on a read
path.

The `islink` test in `sibling_checkout_for` is the other half. Without it, the
walk-from-`rp` change follows a link into the checkout it names, and
`rm <link to a checkout root>` moves from `ask` to `deny`.

## Deliberately out of scope

- **PowerShell.** `Remove-Item`/`Move-Item` bind through `PS_SPEC` and
  `ps_realpath`, which have their own read/write `role` and no entry role. The
  under-block half reaches PowerShell for free through `classify_outside`; the
  over-block half needs a port, filed as a Queue row (the Q73 precedent).
- **`unlink(1)`.** Not in `SPEC`, so it defers today. Guarding it is a new
  command and new prompts, filed as a Queue row.
- **The `build_sibling_hint` wording.** It reads `X -> Y is inside another
  checkout`, which was false for the operand in the reported case. That case
  no longer denies, and the wording is accurate for every case that still does.

## Verification

- The repro above, re-run: every `want` row matches and the three controls have
  not moved.
- Fixtures in `SiblingCheckoutTests` for each row, plus unit tests for
  `entry_realpath` and `entry_operand_mask` (including `mv -t DIR a b`, where
  the destination is the flag value and every positional is a source).
- Shapes that could have regressed, checked by hand and holding: `ln -s
  <outside> evil && rm evil` still asks, so the Q8/Q17 staging defense survives
  entry resolution; `rm ./sub/../../outside-x` still asks, so `../` traversal is
  unaffected; `mv -t /tmp/<dir> ./a` still denies, so a flag-named destination
  stays a content operand.
- `python3 scripts/run-tests.py` over the final tree.
