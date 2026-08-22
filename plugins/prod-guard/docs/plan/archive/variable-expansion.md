# Plan: expand shell variables in resolved targets (Q11)

## Goal

Make the guard see through `$VAR`/`${VAR}` references so a target pinned via a
shell variable — `CTX=kind-ci kubectl --context $CTX ...` — is classified by its
expanded value instead of the literal string `$CTX`.

## Why

A 30-day friction report on the maintainer's transcripts (155 decisions, all
prompts) showed **~68% of prompts** came from variable-expanded targets
(`$CTX` alone accounted for 131). The maintainer's habitual pattern is
`CTX=<ctx> kubectl --context $CTX ...`. `shlex` does not expand variables, so
the `--context` value the hook classifies is the literal `$CTX`, which matches
no pattern → UNKNOWN → `ask`.

This is not only a noise bug. It is also a **security hole**: a *production*
target hidden behind a variable currently slips from `deny` down to `ask`.

```
CTX=gke_acme_prod kubectl --context $CTX delete ns x   → ask   (should be DENY)
CTX=kind-ci       kubectl --context $CTX delete ns x   → ask   (should be defer)
```

Expanding the variable fixes both directions at once: quieter on kind, stricter
on prod. No security property is traded away — the change only makes
classification *more* specific, and anything unresolvable stays literal → still
prompts. This satisfies the "fix the bug, then re-measure" decision before
considering any session-scoped override for the dogfood cluster.

## Approach

One expansion pass over `argv` (and the segment's own env-assignment values)
before the evaluator classifies, using the segment's effective environment.
No per-evaluator changes: every tool sees expanded values for free.

### 1. `expand_vars(value, env)` — conservative expansion

- Expand only simple `$NAME` and `${NAME}` references, where `NAME` is
  `[A-Za-z_][A-Za-z0-9_]*`.
- An **undefined** name is left **literal** (`$CTX` stays `$CTX`), never blanked
  — so it stays UNKNOWN and still prompts. Fail toward friction.
- Any `$` form that is not a plain name reference is left untouched:
  command substitution `$(...)`, arithmetic `$((...))`, defaults/alternates
  `${V:-x}` / `${V:+x}` / `${V:=x}` / `${V?x}`, indirection `${!V}`,
  length `${#V}`, positional/special `$1` `$@` `$$` `$?`. The regex naturally
  declines all of these (the `{NAME}` branch requires the `}` immediately after
  the name; the bare branch requires a letter/underscore right after `$`), so
  the value stays literal → prompts. Under-expanding is always the safe
  direction here.
- Env values are already fully resolved when this runs (see step 3), so a
  single pass suffices — no unbounded recursion, no `A=$A` loop risk.

### 2. Effective environment per segment

Shell-faithful, biased toward friction where faithfulness is ambiguous:

- Base: `os.environ` (a var genuinely exported into the hook's session env is
  what the command would actually expand — classifying it correctly is right).
- Overlaid by **chain env**: assignments that *persist* across `;`/newline
  segments within the command string — an assignment-only segment (`P=x` with no
  command) or an `export NAME=val` segment.
- Overlaid by the segment's **inline** assignments (`A=x cmd`) from
  `extract_env_prefix`. These do *not* persist to the chain env (bash semantics:
  `A=1 cmd` scopes A to `cmd` only) — so a later segment referencing them
  resolves to nothing and still prompts. Conservative and correct.

### 3. Wire into `evaluate_command_string`

Thread a `chain_env` dict through the segment loop:

- Compute `eff_env = {**os.environ, **chain_env, **seg_env}` for the segment.
- Expand each `seg_env` value against `eff_env` (so `TF_WORKSPACE=$W` /
  `AWS_PROFILE=$P` pins work), then re-key: the expanded `seg_env` is what the
  evaluator receives.
- Expand every `argv` token against `eff_env` before `strip_wrappers` /
  evaluator dispatch, so `--context $CTX`, a `bash -c "$CMD"` body, and an
  `eval $CMD` arg are all expanded.
- **Persist** to `chain_env`: an assignment-only segment folds its (expanded)
  assignments into `chain_env`; an `export NAME=val ...` segment parses its
  operands as assignments and folds those in. Chain assignments are expanded
  against the env-so-far at fold time, so `CTX=gke_${P}_${Z}_${C}` resolves
  left-to-right exactly as the shell would.

Recursion (`sh -c`, `eval`) starts with a fresh `chain_env` — a nested command
string is its own scope; the parent's inline assignments already reached it via
`argv` expansion of the `-c` body.

## Out of scope (leave literal → prompt)

- `export` only; not `declare` / `local` / `typeset` / `readonly` (rare in
  agent-issued commands; note in code comment).
- Tilde `~`, brace expansion `{a,b}`, globs — not target-bearing here.
- Command/arithmetic substitution and all `${...}` operators (see step 1).

## Testing

Add unit tests for `expand_vars` (defined/undefined/nested/declined-forms) and
end-to-end subprocess cases mirroring the real transcript patterns:

- `CTX=kind-ci kubectl --context $CTX delete ns x` → defer
- `CTX=gke_acme_prod-us kubectl --context $CTX delete ns x` → **deny** (prod)
- `P=acme_prod; Z=z; C=c; CTX=gke_${P}_${Z}_${C} kubectl --context $CTX delete ns x`
  → **deny** (chained + nested)
- `export CTX=kind-ci; kubectl --context $CTX delete ns x` → defer
- `kubectl --context $NOPE apply -f m.yaml` → `ask` (unresolved stays literal,
  still prompts — never silently allowed)
- `kubectl --context ${CTX:-gke_acme_prod} delete ns x` → not-defer (the `:-`
  operator form is declined and left literal; the literal still contains
  `gke_acme_prod`, so it denies — the point is it never silently *defers*)
- bare (unexported) var must not leak into a `bash -c` body → not silently
  deferred as nonprod; inline `CTX=x bash -c …` does export and resolves

Use only synthetic names (`gke_acme_prod-us`, `kind-ci`, `gag-...`). Never exec
these — subprocess tests read the command as a JSON string.

## Docs to update on completion

- `README.md` Limitations: replace/soften the "quoted string can split a
  segment" note with the new expansion behavior; note the conservative decline
  of `${...}` operator forms.
- `docs/design.md`: a short subsection under the parsing rationale on why
  expansion is safe (only ever more specific; unresolved → prompt).
- Delete the Q11 Queue row.
