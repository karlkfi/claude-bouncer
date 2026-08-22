# Plan: friction report — measure where prod-guard prompts accumulate (Q6)

## Goal

Port the workspace-guard `friction-report.py` approach to prod-guard so a user
(or an agent tuning config) can see, in one command, which prod-guard prompts
dominate their sessions — and specifically which **unknown targets** prompt
repeatedly, because those are the pattern gaps a `.claude/prod-guard.json`
`nonprod` entry would close.

Like its workspace-guard sibling, this is a **read-only analyzer**: it adds no
telemetry and touches no hook behavior. It re-reads the decisions Claude Code
already recorded in `~/.claude/projects/**/*.jsonl` (each `PreToolUse:Bash` hook
run is persisted as an `attachment` of type `hook_success` carrying the hook
`command` and the decision `stdout`) and ranks them.

## Non-goals

- **Changing the hook.** `bash-prod-guard.py` is untouched. No new output, no
  telemetry, no on-disk state. The report only parses data already persisted.
- **Auto-editing config.** The report *surfaces* unknown targets and suggests
  adding a `nonprod` pattern; it never writes `prod-guard.json`. Applying a
  suggestion is a human decision (a prod target must not be silently reclassified
  nonprod — that would be the exact secure-by-default regression the project
  forbids).
- **Cross-plugin reuse.** The script is self-contained in prod-guard (stdlib
  only); it borrows the proven transcript-parsing scaffold but does not depend on
  the workspace-guard install.
- **A separate `reduce-prompts` skill.** workspace-guard pairs the report with a
  cause-by-cause skill; prod-guard's causes are fewer and already documented in
  the README ("Avoiding prod-guard permission prompts"). The command points there
  instead. Note as future work if the report proves it's needed.

## What friction means for prod-guard

The hook builds every prompt reason from one of four helpers in
`bash-prod-guard.py`, each with a stable signature substring:

| Category | Builder | Signature substring | Fix the user reaches for |
|---|---|---|---|
| `deny-prod` | `deny_prod` | `matches a production pattern` | intended block — or `PROD_GUARD_OVERRIDE` if truly intentional |
| `ask-unknown` | `ask_unknown` | `matches neither a production` | **add a `nonprod` pattern** for the target (the actionable gap) |
| `ask-ambient` | `ask_ambient` | `shared mutable state that a parallel session` | pin the target (`--context`/`--project`/`--profile`/…) |
| `ask-switch` | `ask_switch` | `is shared by every session` | pin per-command instead of switching shared state |

A deny downgraded by `PROD_GUARD_OVERRIDE` keeps its underlying `deny-prod`
signature but is emitted as `ask` with an `override acknowledged` prefix — the
report counts these separately so an over-used override is visible.

Every builder wraps the resolved **target** in single quotes (`kube-context
'gke_acme_prod-us'`, `aws profile 'foo'`, `terraform workspace 'bar'`), and every
reason starts with the action in backticks (`` `kubectl delete ns` ``). That
gives two more rankings for free: **top targets** and **by tool** (first word of
the action). Unresolved placeholders (`<unresolved>`, `<unresolved at hook
time>`) start with `<` and are filtered out of target rankings.

## Design

New file `scripts/friction-report.py` (stdlib only, 3.10+), plus a
`commands/friction-report.md` slash command (auto-discovered — plugin.json needs
no `commands` key, same as workspace-guard).

Reused scaffold (adapted from workspace-guard, which is proven):
- `parse_since` — `Nd/Nh/Nm` or `YYYY-MM-DD`; `all` = no cutoff.
- `parse_ts`, `guard_name` (strips the `bash-` prefix so `bash-prod-guard.py`
  → `prod-guard`).
- `iter_decisions(paths, plugin, cutoff, repo)` — per-file `toolUseID → Bash
  command` map for the join; yields `{decision, reason, cwd, ts, command}` for
  each `PreToolUse:Bash` decision, defaulting `--plugin` to `prod-guard`.

prod-guard-specific analysis:
- `CATEGORY_PATTERNS` — the four signatures above.
- `split_reasons(reason)` — split the `|`-joined reason into per-finding
  segments so each segment's category and targets are attributed cleanly.
- `category_of(segment)` — first matching signature, else `other`.
- `targets_of(segment)` — single-quoted tokens, `<…>` placeholders dropped.
- `tool_of(reason)` — first word inside the first backtick group.
- `build_report` — counters: outcomes (deny/ask), categories, tools, overrides,
  `unknown_targets` (targets from `ask-unknown` segments only — the pattern-gap
  list), `targets` (all ask/deny), and top triggering commands.
- `print_text` / `--json`.

Report sections (text mode):
1. Summary — decisions analyzed, outcomes, friction % (ask+deny), overrides.
2. By category.
3. By tool.
4. **Unclassified targets (pattern-gap candidates)** — top `ask-unknown`
   targets, with a one-line hint to add a vetted `nonprod` pattern.
5. Top targets (all prompts).
6. Top triggering commands.

CLI: `--transcripts` (default `~/.claude/projects`), `--plugin` (default
`prod-guard`, or `all`), `--since` (default `7d`), `--repo`, `--top`, `--json`.

## Security / privacy invariants

- Read-only. No writes, no network, no telemetry. Fail-open on unreadable or
  malformed transcript lines (skip the line, never crash).
- The report must never present an unknown target as safe — section 4 is
  explicitly labeled "candidates" and the hint says *vet before adding*, because
  reclassifying a real prod name as nonprod is the forbidden regression.
- `PRIVACY.md`: add a short paragraph — the optional `friction-report` command
  reads local session transcripts read-only and adds no telemetry.

## Tests (`tests/test_friction_report.py`, stdlib unittest)

Import the hyphenated module by path (same `spec_from_file_location` trick as
`test_prod_guard.py`).

- `parse_since`: `7d`/`24h`/`30m`/`YYYY-MM-DD`; bad spec exits.
- `guard_name`: `…/bash-prod-guard.py` → `prod-guard`; non-`.py` → None.
- `category_of`: one real reason per builder → correct category; joined
  multi-segment reason → both categories.
- `targets_of`: extracts quoted targets; drops `<unresolved>`.
- `tool_of`: `` prod-guard: `gcloud compute instances delete` … `` → `gcloud`.
- `iter_decisions` over a synthetic `.jsonl` fixture (write attachment records +
  matching Bash `tool_use`) → correct decision/command join; `--plugin` filter
  excludes a workspace-guard decision; `--repo` and cutoff filters work.
- `build_report`: unknown targets land in the pattern-gap counter; a downgraded
  override is counted as override + ask.
- End-to-end: run the script as a subprocess against a temp transcripts dir →
  assert summary text and `--json` shape.

Fixtures use synthetic names only (`gke_acme_prod-us`, `kind-ci`, `bluefin`) —
never real targets.

## Docs

- `README.md`: a short "Measuring friction" subsection under "Avoiding
  prod-guard permission prompts" — what `/prod-guard:friction-report` shows and
  the flags. Human-facing; no link to CLAUDE.md.
- `PRIVACY.md`: the read-only-transcripts paragraph above.
- `.claude-plugin/plugin.json`: add `command` (and maybe `friction`) keywords.
- `docs/STATUS.md`: delete the Q6 row (isolated commit).
- This plan doc: delete or mark done when shipped.
</content>
</invoke>
