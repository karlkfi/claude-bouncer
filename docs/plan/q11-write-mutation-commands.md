# Q11 — Guarding write/mutation commands

## Problem

Today the hook guards file *reads* (`grep`, `cat`, `jq`, `wc`, etc.). The
mirror-image risk is **writes that escape the workspace** or **destructions
triggered by Claude** — and they have higher blast radius than reads:

- `cp ./prod-config.yaml /tmp/exfil.yaml` — copies workspace data to an
  outside path. The current hook reads `./prod-config.yaml`, sees it inside,
  and allows.
- `mv .env ~/leaked` — same shape, but destructive on the inside path too.
- `rm -rf ../../etc` — irreversible destruction outside the workspace.
- `tee /etc/hosts` — overwrites an outside file with whatever Claude piped
  into it.
- `dd if=/dev/urandom of=/dev/sda` — non-flag operand shape; none of the
  existing SPEC rows would touch it.
- `ln /etc/passwd link && cat link` — hard-link TOCTOU (Q17), the same
  bypass as Q8 minus the `-s`.

None of these commands are in `SPEC`. The hook defers and the default Bash
permission decides — usually `allow` for tools the user has already
permitted broadly.

## Threat model

Two distinct directions, both worth guarding:

| Direction | Example | What's at risk |
|---|---|---|
| **Inside → outside** (exfiltration) | `cp .env /tmp/x`, `tee /etc/hosts`, `dd of=/dev/sda` | Workspace data leaks; outside file is overwritten |
| **Outside → inside** (tampering) | `cp /tmp/payload ./app.py`, `mv /tmp/foo .` | Outside code lands in the workspace |

The read-side hook already catches outside *sources*. Q11's net-new coverage
is outside *destinations*. Hard-link TOCTOU (Q17) is the third axis and
slots in naturally since `ln` is already partially handled by Q8.

### Out of scope: workspace-internal destruction

`rm -rf .` from inside the workspace is irreversible but **not** part of
this plan. The hook's invariant is "outside the workspace = friction" — it
doesn't gate destructive ops inside the workspace any more than it gates
`sed -i` rewrites. If we want that, it's a separate policy decision (file
as Q-item if observed in real sessions).

## Decision policy: `ask`, not `deny`

Stay consistent with the read-side model: outside-workspace path → `ask`.
The hook's job is friction at the boundary, not policy enforcement.
Hard-blocking remains an opt-in via local edit, documented in the README.

Rejected alternatives:

- **Tiered (`deny` for `rm`, `ask` for everything else).** Tempting because
  `rm -rf` is irreversible, but it breaks the "one knob in the README to
  flip allow→ask→deny" mental model. A user who wants `deny` for `rm` can
  flip the global one and accept the same for the rest.
- **`deny` for outside destinations, `ask` for outside sources.** Same
  argument — splitting the policy per direction means two knobs to tune and
  doubles the surface for mistakes. Out-of-workspace writes are already
  rare in Claude's normal flow, so an `ask` prompt is not noisy.

## Scope

In scope (one SPEC row or classifier each):

- **`cp`** — `cp SRC... DEST`, including `-t DIR`/`-T`, `-r`/`-R`, `-a`,
  `-p`, `-i`, `-f`, `-n`, `-v`, `-d`, `-l`, `-s`. Both sources and dest
  participate in the workspace check.
- **`mv`** — `mv SRC... DEST`, including `-t DIR`/`-T`, `-i`, `-f`, `-n`,
  `-v`. Same shape as `cp`.
- **`rm`** — all positionals are targets. Flags: `-r`/`-R`, `-f`, `-i`,
  `-v`, `-d`, `--one-file-system`. `--no-preserve-root` is *not* a special
  case here; the hook only cares about the lexical paths.
- **`tee`** — all positionals are output files. Flags: `-a`/`--append`,
  `-i`/`--ignore-interrupts`, `-p`.
- **`dd`** — `key=value` operand shape. `if=PATH` (read) and `of=PATH`
  (write) are the file operands. Others (`bs=`, `count=`, `seek=`, `skip=`,
  `conv=`, `iflag=`, `oflag=`, `status=`) are values, not files.
- **`ln`** (non-symbolic, i.e. hard-link form) — absorbs **Q17**. The
  existing `classify_ln` already runs for the symbolic form; extend it so
  hard links also stage the LINK path (or, simpler, guard the LINK
  positional directly as a write target whenever it resolves outside).

Out of scope (file as follow-ups if observed):

- **`rsync`** — own flag universe, different threat model (network
  transfers). Worth its own plan doc.
- **`install`** — cp-shape with mode/ownership flags. Low Claude usage.
- **`truncate`**, **`shred`**, **`mkdir -p`**, **`touch`**, **`chmod`**,
  **`chown`**. Mostly low-blast-radius; add ad-hoc if a real session uses
  them to bypass.
- **`>` redirects to outside paths** — already covered by the existing
  `redir_files` collection.
- **Heredoc body** as data source for `cat >FILE <<EOF` — Q15 covers the
  body parsing problem; the redirect target is already checked.

## Tokenization design

The existing SPEC table assumes:

1. Some flags consume N value tokens (`consume`).
2. Some flags name files via their values (`file_flags`).
3. The first `prog` positionals are program/pattern, the rest are files.

This shape **does not fit** three of the new commands:

### A. `cp` / `mv` — last-positional-is-dest

Current SPEC has one `prog` count; it can't distinguish "first is program,
rest are files" from "last is dest, rest are sources." Two options:

- **Option 1 — Add a `roles` field**: `cp` declares `roles: 'src_dst'`
  meaning "all positionals are files; last one is dest, the rest are
  sources." Both source and dest go into the `files` return — the workspace
  check is the same for either direction.
- **Option 2 — Treat all positionals as files** with the existing `prog:0`
  shape. Source vs. dest distinction doesn't matter to the workspace check.

**Recommend Option 2.** The hook returns "any of these files resolves
outside" — direction doesn't affect the decision. Document in the SPEC
comment that `cp`/`mv` rely on positional symmetry being safe here.

`-t DIR` (target-directory) and `-T` (treat dest as file, not dir) need
parsing: `-t` is a `file_flag` (DIR is a file path); `-T` is a no-arg flag
that doesn't change the file list.

### B. `dd` — `key=value` operands

`dd` doesn't take flags at all. Every argument is `key=value`. Add a
`classify_dd(tokens)` analogous to `classify_ln`:

```
def classify_dd(tokens):
    if not tokens or os.path.basename(tokens[0]) != 'dd':
        return None
    files = []
    for t in tokens[1:]:
        if t.startswith('if=') or t.startswith('of='):
            files.append(t.split('=', 1)[1])
    return files
```

Wire it into `main()` between `classify_ln` and `files_in_command`. `dd`
does **not** go in `SPEC`; it gets its own short-circuit because the SPEC
shape doesn't fit.

### C. `ln` (hard-link form) — absorbs Q17

Currently `classify_ln` returns `None` when `-s` is absent. Two paths:

- **Extend staging**: drop the `if not symbolic: return None` early-out;
  stage the LINK path for hard links too. The same `cat link && ...` chain
  in Q8 is then caught.
- **Guard the LINK positional**: add `ln` (non-`-s`) as a write target
  classifier — if LINK resolves outside, `ask` on the `ln` group itself.

**Recommend the staging extension.** Symmetric with Q8, no new code path,
and the threat model is identical (LINK is the surface bash creates that
later commands read through).

## Implementation phasing

This is M-sized — split across PRs to keep each diff reviewable:

1. **PR 1 — `cp`, `mv`, `tee`** (one SPEC row each). All use the existing
   `prog:0` shape with `file_flags` for `-t`/`--target-directory` (cp/mv).
   Tests: outside source, outside dest, `-t` flag, `--` end-of-options,
   chained-with-cd, mixed positionals.
2. **PR 2 — `rm`**. SPEC row with consume entries for the value-taking
   flags (none in practice; `rm`'s flags are all no-arg). Tests: `rm -rf
   /etc`, `rm -- -filename`, chained-with-cd, multiple positionals.
3. **PR 3 — `dd`**. New `classify_dd` + wiring in `main()`. Tests: `if=`,
   `of=`, both, neither, `dd` with no operands.
4. **PR 4 — `ln` hard-link staging** (Q17 absorbed). Extend `classify_ln`
   and update its docstring; remove the symbolic-only short-circuit. Tests:
   `ln /etc/passwd link && cat link`, `ln ./local link && cat link`
   (innocent), single-positional form.

Each PR updates the README decision table and lands `docs/STATUS.md` as a
separate commit per the project rule. Q17's queue row is removed in PR 4.

## Acceptance per command

- `cp ./secret.txt /tmp/exfil` → **ask** (`/tmp/exfil` outside).
- `cp /tmp/payload ./app.py` → **ask** (`/tmp/payload` outside).
- `cp ./a.txt ./b.txt` → **allow**.
- `cp -t /tmp ./a.txt ./b.txt` → **ask** (`/tmp` outside).
- `mv .env ~/leaked` → **ask** (`~` runtime-expanded → outside).
- `rm -rf ../../etc` → **ask** (resolves outside).
- `rm -rf ./build` → **allow**.
- `tee /etc/hosts` → **ask**.
- `echo foo | tee ./log.txt` → **allow**.
- `dd if=/dev/urandom of=/tmp/x bs=1M count=1` → **ask** (`/tmp/x` outside;
  `/dev/urandom` is allowlisted).
- `dd if=./in of=./out bs=1M` → **allow**.
- `ln /etc/passwd link && cat link` → **ask** (Q17, staged LINK).
- `ln ./a ./b && cat ./b` → **allow** (innocent hard link).
- All existing tests continue to pass.

## README impact

- Add `cp`, `mv`, `rm`, `tee`, `dd`, `ln` to the "Guarded commands" list.
- Add new rows to the decision table:
  - `cp ./secret /tmp/x` → **ask**
  - `rm -rf /etc` → **ask**
  - `tee /etc/hosts` → **ask**
  - `dd if=./in of=/tmp/x` → **ask**
  - `ln /etc/passwd link && cat link` → **ask**
- Update "How it works" to mention `dd`'s `key=value` shape and the
  hard-link staging extension.
- Update "Limitations":
  - Remove the hard-link bullet (now covered).
  - Note that workspace-internal destructive ops (`rm -rf .`) are *not*
    gated by design.

## Known gaps after this lands

- `rsync` (own plan doc).
- `install`, `truncate`, `shred` and similar low-frequency writers.
- Multi-source `ln SRC1 SRC2 DESTDIR/` still not staged (out-of-scope per
  Q8; same applies here).
- Per-group redirect cwd tracking is still Q16's problem; this plan doesn't
  fix it.
