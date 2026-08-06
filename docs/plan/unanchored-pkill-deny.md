# Plan: deny an unanchored process-kill pattern (issue 125)

**Goal:** deny `pkill` / `killall` when no operand anchors the pattern to this
workspace's path, so a kill meant for this checkout can't reach a sibling
worktree's processes.

**Approach:** add a `classify_pkill` classifier alongside the existing
`classify_ln` / `classify_dd` / `classify_mktemp` handlers, check its operands
for a workspace path anchor, and emit a new `'kill'` offender category that
`decide` upgrades to `deny`. `WORKSPACE_GUARD_OVERRIDE=<reason>` downgrades it
to `ask`, the same knob the sibling-checkout deny (issue 62) uses.

Tracking issue: <https://github.com/karlkfi/claude-workspace-guard/issues/125>.

## Why this shape

- **A signal is a write to another session's process.** The guard already
  denies a *file* write that lands in a sibling checkout; a pattern-addressed
  kill is the same mistake addressed by pattern instead of by path. Reusing the
  offender/`decide`/`build_reason` pipeline keeps one message shape and one
  override.
- **Deny, not ask.** Measured across local transcripts: 36 of 38 observed
  `pkill` targets would match a sibling, so an `ask` fires on nearly every kill
  and trains reflexive approval — the exact failure it is meant to prevent. A
  deny self-heals in one agent round trip.
- **Anchored kills defer, they do not `allow`.** The file classifiers emit
  `allow` for a clean guarded command; a kill must not, because an `allow`
  short-circuits the user's own permission settings on a destructive command.
  Anchored ⇒ emit nothing ⇒ normal permissions apply.
- **Not gated on `in_worktree`.** Unlike the sibling-checkout deny, this fires
  in every session. The primary checkout is where a human most often sits, and
  `pkill -f make` from there reaches every worktree of the repo.

## The anchor rule

A pattern is **anchored** when it contains the project root's directory name as
a whole path component *with a path separator on at least one side*:

```
pkill -f "issue-95-fa18a8/.build/ginkgo"   # anchored -> defer
pkill -f "/Users/k/ws/repo/bin/server"     # anchored -> defer  (root basename `repo`)
pkill -f ginkgo                            # -> deny
pkill -f issue-95-fa18a8                   # -> deny  (bare word, no separator)
killall node                               # -> deny  (a name can never anchor)
```

Component bounds treat `[A-Za-z0-9._-]` as name characters, so a root named
`repo` does not match inside `repo-branch1`. The separator requirement is what
makes the anchor a *path* anchor: a bare word is a substring match against a
command line, and the guard has no way to judge whether a given word is
distinctive enough to exclude a sibling.

`~` and a leading `$(pwd)` / `$(git rev-parse --show-toplevel)` are resolved
first, reusing `expand_tilde` and `resolve_subst_prefix`, so the forms the guard
already resolves for file arguments resolve here too. Anything else unresolved
(`$VAR`, `~user`) simply fails to match and denies — the safe direction.

## Operand extraction

`classify_pkill(tokens)` returns `None` for a non-kill command, else the list of
non-flag operands. Value-taking flags are consumed from a per-command table
(`pkill`: `-F -G -J -M -N -P -T -U -g -j -s -t -u --signal --ns …`; `killall`:
`-u -t -c -s -y -o -n -Z …`), `--opt=val` splits, `--` ends options, and a
signal flag (`-9`, `-TERM`) falls through as an ordinary flag.

Both misparse directions are safe: swallowing a real pattern leaves no anchored
operand (deny), and mistaking a flag value for an operand yields an unanchored
operand (deny). Precision here buys fewer false denies, never a hole.

An invocation with **no** operand at all (`pkill -u karl`, `pkill -P 1234`)
denies too — it selects processes with nothing tying them to this workspace.

## Decision integration

- Group loop: `classify_pkill` runs after `classify_mktemp`, before
  `files_in_command`. Unanchored ⇒ append a `('kill', detail)` offender; the
  `guarded` flag is **not** set either way.
- `decide`: `kill_hit and ctx.override is None` ⇒ `deny`; with the override set
  ⇒ `ask`, wording adjusted like the sibling hint.
- `build_reason`: a `build_kill_hint` naming the offending pattern, the
  workspace root to anchor to, and the two safe rewrites (`pgrep -fl` then kill
  by pid, or anchor the pattern).

`WORKSPACE_GUARD_OVERRIDE` now drives two denies, so `sibling_override()` →
`guard_override()` and the `Ctx` field `sib_override` → `override`.

## Scope / deliberate limitations

- **Bash only.** PowerShell's `Stop-Process` is not covered; filed as a Queue
  row rather than grown here (Q53 already tracks the PowerShell surface).
- **`kill <pid>` is untouched.** Killing by pid is the recommended rewrite, not
  a hazard.
- **A process started with a relative command line can't be anchored.** `make
  check` has no path in its command line, so `-f` matching cannot be scoped to a
  worktree at all — that case's answer is `pgrep -fl` and kill by pid, which is
  the first rewrite the message names.
- **Nested worktrees under the project root count as in-workspace.** A pattern
  naming the primary checkout's path also matches `<root>/.claude/worktrees/*`
  processes. That is consistent with the guard's boundary: those paths *are*
  under the project root.

One extra rule the smoke tests forced, beyond the shape above: an operand still
carrying an expansion after `expand_tilde`/`resolve_subst_prefix` can never
anchor. `$HOME/repo/bin` contains the literal text `/repo/` and would otherwise
read as anchored while bash sends it somewhere unverifiable — the same
unresolvable-means-outside rule `resolve_token` applies to file arguments.

## Deliverables

- [x] `scripts/bash-workspace-guard.py` — `classify_pkill`, the anchor check,
      the `'kill'` category, `build_kill_hint`, `decide` upgrade, override rename.
- [x] Tests — unit (operand extraction, anchor matching, `build_reason`) + e2e
      (unanchored deny, anchored defer, override downgrade, no-operand deny,
      substitution body, `killall`).
- [x] `README.md` — decision-table rows, What-it-does, an "Unanchored
      process-kill deny" section, How-it-works step 13, Configuration,
      Limitations, agent-guidance bullet.
- [x] `docs/design.md` — why a non-file command is in scope.
- [x] `.claude-plugin/plugin.json` — `pkill` / `process` keywords.
- [x] `docs/STATUS.md` — Q56 for the PowerShell `Stop-Process` gap.
- [x] `CLAUDE.md` (this repo) — nothing; the agent-guidance block ships in README.
