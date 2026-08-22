# Plan: guard commands inside quoted `"$(…)"` / backtick substitution bodies (Q33)

## Goal

A guarded command buried in a **quoted** command substitution — `"$(mktemp)"`,
`"$(grep foo /outside/f)"`, or a backtick body `` `mktemp` `` — is currently
invisible to the hook, so a host-temp/outside write or read it performs isn't
flagged. Parse those bodies and route their file operations through the same
guard machinery. The **unquoted** `$(…)` form is already caught (the subshell
`(`/`)` split the inner command into its own group), so this only closes the
quoted/backtick gap.

## Why the gap exists

shlex tokenizes with `punctuation_chars` including `(`/`)`, so an *unquoted*
`$(mktemp)` surfaces `(`, `mktemp`, `)` as separate tokens and the group loop
parses `mktemp` as its own command. Inside double quotes or backticks those
metacharacters are **not** punctuation — shlex keeps the whole substitution as a
single word token (`$(mktemp)`), quotes stripped — so the body is never
tokenized as a command. Verified end-to-end (CLI-shaped stdin JSON):

| command | today | want |
|---|---|---|
| `cat $(mktemp)` (bare) | deny (hosttemp) | unchanged |
| `echo "$(mktemp -p /tmp x.XXXX)"` | **defer** | deny (hosttemp) |
| `` x=`mktemp` `` | **defer** | deny (hosttemp) |
| `` echo `cat /outside/f` `` | **defer** | ask (outside) |
| `cat "$(grep foo /outside/f)"` | ask (expand only) | ask (outside read named) |
| `echo '$(mktemp -p /tmp x)'` (single-quoted) | defer | **defer** (bash does not substitute) |

## Approach

1. **Quote-aware substitution scanner** `command_substitutions(text)` over the
   **raw** command string (not the post-shlex tokens — those have lost the
   single-vs-double quote distinction). Tracks single-quote / double-quote /
   backslash state and, only in unquoted or double-quoted context, extracts each
   `$(…)` body (balanced `)`, honoring nested quotes and nested `$(…)`) and each
   `` `…` `` body (to the next unescaped backtick). Skips `$((…))` arithmetic
   (no command inside). Single-quoted regions are ignored, matching bash. On an
   unbalanced/at-EOF substitution it simply yields nothing for that one —
   fail-safe (may miss an offender, never fabricates one).

2. **Refactor the analysis core** out of `handle_bash` into
   `analyze_command(cmd, ctx, base_cwd) -> (offenders, guarded)`: tokenize →
   group split → the existing cwd/varmap/loopmap group loop, returning the
   `outside` offenders list and the `guarded` flag instead of emitting. The
   nested closures (`resolve_token`, `check_file`, `stage_ln`) move inside it
   unchanged. `handle_bash` calls it once for the top command and keeps the
   existing emit logic verbatim.

3. **Recurse into substitution bodies inside `analyze_command`.** After the
   group loop, for each body from `command_substitutions(cmd)`, recurse and
   extend `offenders` with the body's offenders — **discarding the body's
   `guarded` flag**. Bodies are proper substrings, so recursion strictly shrinks
   and terminates; a depth cap is a belt-and-suspenders backstop. Nested
   substitutions are handled by the recursion re-scanning each body.

## Key decisions

- **Substitution analysis only ever ADDS offenders — it never produces an
  `allow`.** The body's `guarded` flag is dropped on the way up, so a *clean*
  guarded command inside a substitution (`echo "$(cat in-workspace.txt)"`) does
  NOT flip the deferring outer `echo` into a hook `allow` that suppresses normal
  permissions. Only the top command's own `guarded` can emit `allow`
  (unchanged). This keeps the change strictly friction-adding — the
  secure-by-default direction, and no surprise auto-approvals.
- **Scan the raw string, not tokens, for quote fidelity.** Single-quoted
  `'$(…)'` is a literal in bash; extracting it from quote-stripped tokens would
  false-*deny* legitimate literals. Raw-string scanning gets the single-quote
  skip right. (This is stricter than the pre-existing `cd`-substitution
  whitelist, which knowingly matches after quote stripping for its two closed
  cases.)
- **Consistency with direct commands.** A body's file ops go through the exact
  same `check_file` → `classify_outside`, so `echo "$(cat /tmp/x)"` reaches the
  identical host-temp `deny` as a direct `cat /tmp/x`, and an outside read is
  named by its real inner path in the reason.
- **Body cwd = the command's starting cwd (`ctx.cwd`).** A substitution runs in
  a subshell with the enclosing cwd; we do not re-thread a prior in-chain `cd`
  (`cd /x && echo "$(cat f)"`) into the body, because the raw substitution isn't
  associated back to a per-group tracked cwd after tokenization. Worst case is a
  friction-only false positive (relative body path judged against the wrong cwd)
  or the same miss today's full-defer already has — never a silent allow of a
  resolved outside path. Documented as a Limitation.
- **Out of scope (naturally):** process substitution `<(…)`/`>(…)` is only ever
  bare and already caught by the subshell split; backtick nesting via `` \` ``
  and exotic arithmetic edges degrade toward defer/skip (fail-safe).

## Tests

- Unit: `command_substitutions` — double-quoted `$(…)`, backtick, bare,
  single-quoted (skipped), nested `$(… $(…) …)`, inner double quotes,
  `$((arith))` skipped, unbalanced (yields nothing).
- End-to-end (subprocess, synthetic `/tmp/q33-fake-target` style paths only):
  the table above — quoted mktemp → deny, backtick assign → deny, backtick
  outside read → ask, quoted grep outside → ask naming the inner path,
  single-quoted literal → defer, plus a regression that `echo
  "$(cat in-workspace.txt)"` still **defers** (no new `allow`).

## Docs

- README decision table: add the quoted/backtick substitution rows.
- README "How it works": note substitution-body recursion.
- README Limitations: replace the "command buried inside a command substitution
  (`cd $(mktemp -d)`)" bullet — the inner command IS now parsed for the
  quoted/backtick and bare forms; document the remaining `cd`-interaction and
  single-quote-fidelity edges.
- STATUS.md: remove the Q33 row (its own commit).
