# workspace-guard

A Claude Code plugin that adds a `PreToolUse` hook for `Bash`. When a guarded command (`grep`, `sed`, `jq`, `awk`, `cat`, `head`, `tail`) is about to read or write a file outside `$CLAUDE_PROJECT_DIR`, the hook returns `ask` so the user can confirm; in-workspace files and pure pipelines are allowed silently. See `README.md` for the user-facing overview and the decision table.

The load-bearing piece is `scripts/bash-workspace-guard.py` — a stdlib-only Python script that tokenizes the command with `shlex`, classifies tokens against a per-command `SPEC` table, resolves file arguments with `realpath`, and emits a `PreToolUse` decision.

## Model selection

Use the `model-advisor` skill to assess the right model and thinking level at session start and whenever the task type shifts significantly (e.g. moving from a small `SPEC` row addition to redesigning the tokenizer).

## Development philosophy

Build the right thing AND build it well. Before writing any code, state the goal in one sentence and the approach in two or three. If the goal is unclear, ask one focused question rather than guessing.

Make the smallest change that achieves the goal. If you notice problems outside the current task's scope, flag them rather than fixing them:
- New near-term work → add a row to the Queue in `docs/STATUS.md` in priority order.
- Larger / speculative work → add a Queue row marked `💤 deferred` with a one-line rationale.

Capture knowledge durably, don't leave it in chat. When the user states a standing preference or decision, persist it in the repo (CLAUDE.md, the relevant `docs/` file, or memory) rather than applying it once and moving on. When follow-up work surfaces mid-task, record it on the Queue in `docs/STATUS.md` — including the *why* of any decision it depends on — instead of only mentioning it in the response.

Before introducing a new pattern or abstraction, check whether the existing `SPEC`/`ALIASES` model already solves the problem with a new row.

## Workflow

1. **At session start, check whether the worktree is stale.** New worktrees are branched from `main` at creation time, but `main` may have advanced since then — particularly if a previous session merged a PR. Run `git fetch origin main` and compare with `git log --oneline HEAD..origin/main`; if `origin/main` has new commits, rebase with `git rebase origin/main` before doing any other work. This avoids editing files against an outdated baseline and surfacing phantom conflicts at PR time.
   - **Work on a `claude/`-prefixed branch, never on `main`.** In a worktree session, do all work via the worktree path — never edit files through the parent repo's path.
2. **Before making changes** — read `README.md` and skim `scripts/bash-workspace-guard.py` so the proposed change matches the existing parsing model. If picking the next task, run `gh pr list` first and skip any Queue item from `docs/STATUS.md` already covered by an open PR.
   - **Verify 🚫 blockers are still real.** A previous session may have silently completed the dependency without flipping the Queue row. Grep for the deliverables before treating the item as truly blocked.
   - **Investigation findings marked ✅ must be end-to-end verified, not just source-read.** If a `§Findings` block claims "command X with flag Y produces Z" because of source inspection, actually run the command and confirm. Shell parsing is full of surprises that only show up when you exec the thing.
3. **For complex tasks** — write an explicit plan to `docs/plan/<slug>.md` and follow it. Keep it updated so completed scope is verifiable at the end. Revise the plan if new information changes the approach.
4. **After making changes** — review the diff. Update docs proactively:
   - **Changed parsing behavior or `SPEC` table** → update the decision table in `README.md` and the "How it works" / "Limitations" sections.
   - **New configuration or hook surface** → `README.md` and `.claude-plugin/plugin.json` keywords/description.
   - Update `docs/STATUS.md`: remove the completed Queue row.
5. **Commit when done** — small, focused, Conventional Commits. **Always commit `docs/STATUS.md` changes in their own isolated commit**, separate from code and plan-doc changes (see `docs/development/maintaining-backlog.md`).

## Code standards

### Python (`scripts/bash-workspace-guard.py`)

- Stdlib only — no third-party deps. The hook runs on whatever `python3` the user has on their PATH.
- The `SPEC` table is the contract. Adding a guarded command means adding a row with explicit `consume` / `file_flags` / `prog` / `prog_suppressed_by` entries — do not "infer" flag behavior at runtime.
- On any parsing uncertainty (unbalanced quotes, unknown shell construct, empty input), the hook **defers silently** (returns nothing) so normal permissions apply. Never fail closed without an explicit reason.
- Default decision for outside-workspace paths is `ask`, not `deny`. Hard-blocking is opt-in via a local edit, documented in `README.md`.

### Bash (if any helper scripts are added)

There are no Bash scripts in the repo today. If one is added: start with `set -euo pipefail`, use `local` inside functions, use `[[ ]]` / `(( ))` (never `[ ]`), and quote all variable expansions.

## Security principles

**Secure by default, not opt-in.** This plugin exists to add a guardrail; its defaults must never trade away a security property for convenience. If a proposed change weakens any property — even partially, even with mitigations — the more secure behavior stays the default. The looser behavior may be offered as an explicit opt-in (env var, config, local edit) but must be documented as a trade-off.

Examples of regressions that must not silently become defaults:
- Flipping the outside-workspace decision from `ask` to `allow`.
- Removing a guarded command from the `SPEC` table because it was "noisy".
- Treating an unparseable command as `allow` rather than deferring.
- Aliasing a new tool to an existing `SPEC` row when their flag sets diverge (see Q3 on `rg`).
- Skipping `realpath` resolution for a class of paths so that `../` traversal is no longer caught.

When in doubt, ask before shipping. The hook's job is to add friction at the security boundary; removing friction is the change that needs sign-off, not adding it.

## Testing

Tests live in `tests/test_workspace_guard.py` (stdlib `unittest`, no third-party deps). Run with:

```
python3 -m unittest discover tests
```

Two layers:
- **Unit tests** import `files_in_command` from the script and exercise per-`SPEC`-row parsing, `prog_suppressed_by`, `--opt=val`, end-of-options `--`, and aliases.
- **End-to-end tests** invoke the script as a subprocess, feed it the hook's stdin JSON, and assert the emitted `permissionDecision` for workspace-vs-outside paths, redirect targets, pipe chains, and defer cases (unguarded command, empty input, unbalanced quotes).

When changing `SPEC` or tokenization, add the case that motivated the change as a fixture, and hand-exercise the decision table in `README.md` against the change before committing.

**Never use real outside-workspace paths — especially sensitive ones — as the target in test fixtures or hand-exercised bypass commands.** Use a synthetic placeholder like `/tmp/q8-fake-target` instead. The hook only checks lexical resolution against `$CLAUDE_PROJECT_DIR`; file existence and contents don't matter for coverage. The placeholder exercises identical code paths with zero risk of:
- materialising a real symlink to `/etc/passwd`, `~/.ssh/id_rsa`, or `~/.aws/credentials` during a bypass hand-exercise (if the hook erroneously returns `allow`, bash will *run* the command);
- leaking sensitive content into shell scrollback, test output, or CI logs;
- normalising the bad habit of reaching for real credential paths when a fake one is just as instructive.

This applies to anything you'd run through the user's `Bash` tool while developing or demoing — not just committed test fixtures. Subprocess-only tests that never invoke bash on the command (the script reads it as a JSON string) are technically safe, but follow the same rule for consistency.

## Commits

- Commit after each task is complete and validated.
- Use small, focused commits.
- Follow the Conventional Commits standard.
- Amending an unpushed commit is fine — fix up the message or staged changes before pushing without asking. Once a commit is pushed, prefer a follow-up commit; only amend + force-push (always `--force-with-lease`, never on `main`/`master`) when the user asks for it.
- After pushing, check whether a PR exists (`gh pr view`). If one does, update its description with `gh pr edit` to reflect any new commits.
- Always commit `docs/STATUS.md` changes in their own isolated commit, separate from code and plan-doc changes. STATUS.md is high-contention across parallel sessions; isolating it makes rebase conflicts trivial to resolve.
- If a change doesn't belong in the current PR, open a separate PR for it. Working multiple PRs in parallel is fine and preferable to bundling unrelated concerns.
- Act only on your own branch and PR. Never re-run, edit, or push to a PR or branch owned by another session; when CI fails on another session's PR, reproduce the failure locally instead.
- Queue items have `Q`-prefixed IDs (e.g. `Q1`). Use the bare ID in commit messages and PR bodies — the `Q` stops GitHub from auto-linking the number to PR/issue 1.

## Documentation conventions

Spell out acronyms on first use: write the full term first, then the acronym in parentheses — e.g. "continuous integration (CI)". Subsequent uses may use the acronym alone.

Human-facing docs (`README.md`, anything under `docs/` outside `docs/development/maintaining-backlog.md`) must never link to `CLAUDE.md` or `AGENTS.md`. This file is the entrypoint for Claude/agents only; humans start at `README.md`. The dependency direction is one-way: `CLAUDE.md` may link out to `docs/` and `README.md`, but nothing under those may link back to it.

**Editing `CLAUDE.md` — protect the context budget.** This file is loaded in full into every session, so every line costs context. Keep it lean: add only load-bearing, must-act-on rules, and put the explanation/how-to in the relevant `docs/` page with a one-line pointer here rather than growing a self-contained copy past a few sentences. When in doubt, write the detail in `docs/` and link it; prefer tightening an existing line over adding a new one.

## Agent reference docs

When working on specific tasks, read the relevant doc before starting:

| Task | Reference |
|---|---|
| Picking the next task, tracking progress, adding new items | `docs/STATUS.md` — also run `gh pr list` and skip any Queue item already covered by an open PR |
| Editing `docs/STATUS.md` (any change to the Queue or header) | `docs/development/maintaining-backlog.md` |
| Changing parsing behavior or the `SPEC` table | `scripts/bash-workspace-guard.py` + `README.md` decision table |
| Plugin packaging / marketplace listing | `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` |
| Cutting a release (version bump, tag, GitHub Release) | `docs/development/release-process.md` |
| Measuring where prompts accumulate (friction review) | `docs/development/measuring-friction.md` + `scripts/friction-report.py` |
| Rendering or regenerating brand images (social preview, favicon) | `docs/development/rendering-images.md` |
| Hook registration | `hooks/hooks.json` |
