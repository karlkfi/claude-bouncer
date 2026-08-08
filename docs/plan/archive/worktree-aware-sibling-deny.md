# Plan: worktree-aware sibling-checkout deny (issue 62)

**Goal:** deny writes that land in a *sibling checkout of the same repo* (the
primary checkout or another worktree) when a session runs inside a git
worktree, with a message that names the offending checkout, its branch, and the
corrected in-session path.

**Approach:** add sibling-checkout detection to the existing guard script,
upgrade the outside-workspace decision for *writes* into a sibling from `ask` to
`deny`, and add one narrow `PreToolUse` hook for `Edit`/`Write`/`MultiEdit`/
`NotebookEdit` whose only active rule is the same sibling deny. Reads keep
today's behavior. `WORKSPACE_GUARD_OVERRIDE=<reason>` downgrades the deny to
`ask`.

Tracking issue: <https://github.com/karlkfi/claude-workspace-guard/issues/62>.

## Why this shape

- **One plugin, no duplication.** Detection lives once in
  `scripts/bash-workspace-guard.py`; the Edit/Write surface dispatches to the
  same functions in the same script (matched on `tool_name`), so there is no
  second tokenizer/root-detector and no competing messages on the same Bash
  call. This is the reason the issue rejected a standalone worktree-guard.
- **Reuse the existing `is_read` contract.** The sibling deny fires only for
  write-context file arguments — exactly the `is_read=False` set the hook
  already computes (redirect targets, `dd` operands, and the `WRITE_COMMANDS`
  `cp`/`mv`/`tee`/`rm`). Read commands keep their current outside/host-temp/
  read-prefix handling untouched. This is consistent with how
  `ALLOWED_READ_PREFIXES` already treats those write commands wholesale.
- **Per-path detection, not full enumeration.** Rather than enumerate every
  worktree (this repo alone has ~60), we resolve the *offending path's*
  enclosing checkout and compare its git common-dir to the session's. Same
  common-dir + different checkout root ⇒ sibling. This is the `--git-common-dir`
  equivalence the issue names, and it avoids reading N worktrees' metadata per
  invocation. A path in an unrelated git repo has a different common-dir and is
  never treated as a sibling (stays a generic outside `ask`).

## Detection (stdlib, reads only tiny git metadata)

Git's on-disk layout (verified against a real worktree):

- Linked worktree root has a `.git` **file**: `gitdir: <common>/worktrees/<name>`.
- That admin dir holds `commondir` (`../..` → the shared `.git`) and `HEAD`.
- The main checkout root has a `.git` **directory** (which *is* the common-dir);
  its `HEAD` names the main branch.

Functions to add:

- `_resolve_checkout(start_dir)` — walk up to the nearest enclosing `.git`
  (dir ⇒ main checkout; file ⇒ linked worktree via `gitdir:`), returning
  `{root, admin, common}` realpaths, or `None`.
- `resolve_session_worktree(proj)` — resolve the session's checkout and set
  `in_worktree = (admin != common)`. Sibling detection is a **no-op** unless the
  session is itself a linked worktree (honors "no-op when not in a worktree").
- `sibling_checkout_for(rp, session)` — resolve `rp`'s enclosing checkout;
  return `(root, branch)` iff same `common` and different `root`, else `None`.
- `_branch_label(admin)` — read `admin/HEAD`; `ref: refs/heads/X` → `X`,
  else `(detached <sha>)`.

All reads are wrapped so any failure yields `None`/no-sibling (fail-safe: the
path keeps its normal outside `ask`; the boundary is never weakened).

## Decision integration

- **Bash** (`check_file`): inside `is_outside(rp)`, before the host-temp branch,
  if `not is_read` and `sibling_checkout_for(rp, session)` matches → category
  `'sibling'` carrying `{root, branch, corrected}`.
- **Final decision** (`handle_bash`): `sibling_hit and not WORKSPACE_GUARD_OVERRIDE`
  → `deny`; with the override set → `ask`. Host-temp deny and `bypassPermissions`
  deny still apply as today.
- **Edit/Write/MultiEdit/NotebookEdit** (`handle_edit`): resolve `file_path`
  (or `notebook_path`); if inside the session workspace or not a sibling → defer
  (emit nothing, builtin permissions apply); if a sibling → `deny` (or `ask`
  under the override), reusing `build_reason` for an identical message.

`corrected = session_root / relpath(rp, sibling_root)` — same relative path,
under the session's checkout.

## Wiring

`hooks/hooks.json`: add a second `PreToolUse` entry matching
`Edit|Write|MultiEdit|NotebookEdit` pointing at the same script. The script
dispatches on `tool_name` (absent ⇒ Bash, preserving existing behavior).

## Scope / deliberate limitations

- **`cp`/`mv` sources and `dd if=` into a sibling deny too** (they are
  `is_read=False` write-context in the existing model). This is stricter than a
  pure "destination only" reading, in the secure direction, and recoverable via
  the override. Documented as a limitation.
- **Only writes.** Reads of a sibling checkout keep today's `ask` (staleness,
  not damage).
- **Symmetric by common-dir, gated on `in_worktree`.** A main-checkout session
  is a no-op even if worktrees exist (matches the issue's stated no-op).

## Privacy posture change

The hook now reads small git metadata files (`.git`, `commondir`, `HEAD`) to
detect sibling checkouts — still local, offline, no network, no
version-controlled file contents. Update `PRIVACY.md` and the README Privacy
section.

## Deliverables

- [ ] `scripts/bash-workspace-guard.py` — detection + dispatch + `handle_edit`
      + `'sibling'` category + override + `build_reason` sibling hint.
- [ ] `hooks/hooks.json` — Edit/Write matcher.
- [ ] Tests — unit (detection helpers, `build_reason` sibling) + e2e (Bash
      write/read into sibling, Edit/Write deny, override, non-worktree no-op,
      unrelated-repo stays `ask`) using a real `git worktree` fixture.
- [ ] `README.md` — decision-table rows, How-it-works step, Configuration
      (`WORKSPACE_GUARD_OVERRIDE`), Limitations, agent-guidance bullet, Privacy.
- [ ] `PRIVACY.md` — git-metadata read disclosure.
- [ ] `.claude-plugin/plugin.json` — `worktree` keyword; description if apt.
- [ ] `docs/design.md` — brief note on the new hook surface (optional).
</content>
</invoke>
