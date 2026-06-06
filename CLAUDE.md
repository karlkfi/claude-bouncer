# branch-guard

A Claude Code plugin that adds a `PreToolUse` hook for `Bash`, `Edit`, `Write`, and `MultiEdit`. It auto-approves `git commit` on non-protected branches (e.g. `claude/*`, feature branches) and returns `ask` before a `git commit` or a file edit that targets a protected branch (`main`/`master`). Everything else defers silently so the normal permission flow applies. See `README.md` for the user-facing overview and the decision table.

The load-bearing piece is `hooks/branch-guard.sh` — a Bash hook that reads the `PreToolUse` JSON from stdin, classifies the tool (`Bash` git-commit vs `Edit`/`Write`/`MultiEdit`), resolves the relevant branch with `git rev-parse`, and emits a decision against `PROTECTED_BRANCH_REGEX`. For edits it resolves the branch of **the file's own repository** (`git -C <dir-of-file>`), not the session cwd.

## Model selection

Use the `model-advisor` skill to assess the right model and thinking level at session start and whenever the task type shifts significantly (e.g. moving from a one-line regex tweak to reworking how the hook tokenizes commands or resolves branches).

## Development philosophy

Build the right thing AND build it well. Before writing any code, state the goal in one sentence and the approach in two or three. If the goal is unclear, ask one focused question rather than guessing.

Make the smallest change that achieves the goal. If you notice problems outside the current task's scope, flag them rather than fixing them — mention them at the end of the turn or open a separate PR.

Before introducing a new pattern or abstraction, check whether the existing `case "$tool"` dispatch and `PROTECTED_BRANCH_REGEX` already solve the problem with a small edit.

## Workflow

1. **At session start, check whether the worktree is stale.** New worktrees are branched from `main` at creation time, but `main` may have advanced since then — particularly if a previous session merged a PR. Run `git fetch origin main` and compare with `git log --oneline HEAD..origin/main`; if `origin/main` has new commits, rebase with `git rebase origin/main` before doing any other work.
2. **Before making changes** — read `README.md` and the whole of `hooks/branch-guard.sh` so the proposed change matches the existing dispatch model. If picking the next task, run `gh pr list` first and skip anything already covered by an open PR.
   - **Verify behavioral claims end-to-end, not just by source-reading.** Shell parsing is full of surprises that only show up when you exec the thing. If a change depends on "command X matches the git-commit grep" or "this branch resolves to Y," actually run `./test/run.sh` (or a targeted reproduction) and confirm.
3. **After making changes** — review the diff and update docs proactively:
   - **Changed the decision logic, the git-commit matcher, or `PROTECTED_BRANCH_REGEX`** → update the behavior table and "Known limitation" section in `README.md`.
   - **New configuration or hook surface** → `README.md`, `hooks/hooks.json`, and `.claude-plugin/plugin.json` keywords/description.
4. **Commit when done** — small, focused, Conventional Commits.

## Code standards

### Bash (`hooks/branch-guard.sh`)

- Start with `set -euo pipefail`. Use `local` inside functions, `[[ ]]` / `(( ))` (never `[ ]`), and quote all variable expansions.
- Stay dependency-light: the hook may rely only on `git` and `jq` (documented requirements). Don't add new runtime dependencies without flagging it.
- The `case "$tool"` dispatch and `PROTECTED_BRANCH_REGEX` are the contract. Adding a guarded tool or protected branch means an explicit edit there — don't infer behavior at runtime.
- On any uncertainty — not a git repo, detached HEAD, empty/missing input, unresolvable branch — the hook **defers silently** (`exit 0`, emits nothing) so normal permissions apply. Never fail closed without an explicit reason.
- Default decision for a protected branch is `ask`, not `deny`. Hard-blocking is opt-in via a local edit and must be documented in `README.md` if introduced.
- When emitting JSON, escape the reason string (see the `emit()` helper). Don't hand-build decision JSON elsewhere.

## Security principles

**Secure by default, not opt-in.** This plugin exists to add a guardrail; its defaults must never trade away a security property for convenience. If a proposed change weakens any property — even partially, even with mitigations — the more secure behavior stays the default. The looser behavior may be offered as an explicit opt-in (env var, config, local edit) but must be documented as a trade-off.

Examples of regressions that must not silently become defaults:
- Flipping the protected-branch decision from `ask` to `allow`.
- Removing `main` or `master` from `PROTECTED_BRANCH_REGEX` because it was "noisy".
- Treating an unresolvable branch or unparseable input as `allow` rather than deferring.
- Narrowing the git-commit matcher so a `commit` invocation slips through unguarded.
- Resolving the edit branch from the session cwd instead of the file's own repo (`git -C <dir-of-file>`), so edits through a checkout sitting on `main` are no longer caught.

When in doubt, ask before shipping. The hook's job is to add friction at the protected-branch boundary; removing friction is the change that needs sign-off, not adding it.

## Testing

Tests live in `test/run.sh`. Run with:

```bash
./test/run.sh
```

It spins up a throwaway git repo under `tmp/` and asserts the emitted `permissionDecision` for each tool/branch combination (commit on a feature branch, commit on `main`, edits on protected vs non-protected branches, and defer cases).

When changing the decision logic, the git-commit matcher, or `PROTECTED_BRANCH_REGEX`, add the case that motivated the change as a fixture, and hand-exercise the behavior table in `README.md` against the change before committing.

## Commits

- Commit after each task is complete and validated.
- Use small, focused commits following the Conventional Commits standard.
- Amending an unpushed commit is fine — fix up the message or staged changes before pushing without asking. Once a commit is pushed, prefer a follow-up commit; only amend + force-push (always `--force-with-lease`, never on `main`/`master`) when the user asks for it.
- After pushing, check whether a PR exists (`gh pr view`). If one does, update its description with `gh pr edit` to reflect any new commits.
- If a change doesn't belong in the current PR, open a separate PR for it. Working multiple PRs in parallel is fine and preferable to bundling unrelated concerns.

## Documentation conventions

Human-facing docs (`README.md` and anything user-facing) must never link to `CLAUDE.md` or `AGENTS.md`. This file is the entrypoint for Claude/agents only; humans start at `README.md`. The dependency direction is one-way: `CLAUDE.md` may link out to `README.md` and other docs, but nothing user-facing may link back to it.

## Agent reference docs

| Task | Reference |
|---|---|
| Changing the decision logic, git-commit matcher, or protected-branch set | `hooks/branch-guard.sh` + `README.md` behavior table |
| Hook registration / matcher | `hooks/hooks.json` |
| Plugin packaging / marketplace listing | `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` |
| Testing the decision matrix | `test/run.sh` |
