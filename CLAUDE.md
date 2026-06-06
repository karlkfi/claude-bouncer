# branch-guard

A Claude Code plugin that adds a `PreToolUse` hook for `Bash`, `Edit`, `Write`, and `MultiEdit`. It auto-approves `git commit` on non-protected branches (e.g. `claude/*`, feature branches), returns `ask` before a `git commit` or file edit that targets a protected branch (`main`/`master`), and guards `git push` according to a configurable policy. Everything else defers silently so the normal permission flow applies. See `README.md` for the user-facing overview and the decision tables.

The load-bearing piece is `hooks/branch-guard.py` — a stdlib-only Python hook that reads the `PreToolUse` JSON from stdin, classifies the tool (`Bash` git-commit/git-push vs `Edit`/`Write`/`MultiEdit`), resolves the relevant branch with `git rev-parse`, and emits a decision against `PROTECTED_BRANCH_RE`. For Bash it tokenizes the command with `shlex` (matching workspace-guard's parsing model) via `command_segments`, splits it into simple-command segments, and identifies `git` subcommands robustly with `parse_git` (env prefixes, global flags, chains) rather than by substring match. For edits it resolves the branch of **the file's own repository** (`git -C <dir-of-file>`), not the session cwd.

The push guard (`push_decision`/`push_policy`) is driven by the `BRANCH_GUARD_PUSH_POLICY` env var: `protected` (default — ask only when a push targets `main`/`master`), `strict` (ask unless the push is the worktree's own current branch), or `off`. A Bash chain that contains a `git push` is never auto-approved via the commit path; the push is always routed through the push guard first.

## Model selection

Use the `model-advisor` skill to assess the right model and thinking level at session start and whenever the task type shifts significantly (e.g. moving from a one-line regex tweak to reworking how the hook tokenizes commands or resolves branches).

## Development philosophy

Build the right thing AND build it well. Before writing any code, state the goal in one sentence and the approach in two or three. If the goal is unclear, ask one focused question rather than guessing.

Make the smallest change that achieves the goal. If you notice problems outside the current task's scope, flag them rather than fixing them — mention them at the end of the turn or open a separate PR.

Before introducing a new pattern or abstraction, check whether the existing tool dispatch in `main()` and `PROTECTED_BRANCH_RE` already solve the problem with a small edit. The tokenizer (`command_segments`, `git_subcommand`) is deliberately shared in spirit with workspace-guard — reuse that model rather than inventing a parallel one.

## Workflow

1. **At session start, check whether the worktree is stale.** New worktrees are branched from `main` at creation time, but `main` may have advanced since then — particularly if a previous session merged a PR. Run `git fetch origin main` and compare with `git log --oneline HEAD..origin/main`; if `origin/main` has new commits, rebase with `git rebase origin/main` before doing any other work.
2. **Before making changes** — read `README.md` and the whole of `hooks/branch-guard.py` so the proposed change matches the existing dispatch and tokenization model. If picking the next task, run `gh pr list` first and skip anything already covered by an open PR.
   - **Verify behavioral claims end-to-end, not just by source-reading.** Shell tokenization is full of surprises that only show up when you exec the thing. If a change depends on "command X parses as a git commit" or "this branch resolves to Y," actually run `./test/run.sh` (or a targeted reproduction) and confirm.
3. **After making changes** — review the diff and update docs proactively:
   - **Changed the decision logic, the git-commit detection, or `PROTECTED_BRANCH_RE`** → update the behavior table and "Known limitation" section in `README.md`.
   - **New configuration or hook surface** → `README.md`, `hooks/hooks.json`, and `.claude-plugin/plugin.json` keywords/description.
4. **Commit when done** — small, focused, Conventional Commits.

## Code standards

### Python (`hooks/branch-guard.py`)

- Stdlib only — no third-party deps. The hook runs on whatever `python3` the user has on their PATH (`hooks/hooks.json` invokes `python3 …`). `git` must be on PATH; the hook no longer shells out to `jq`.
- The tool dispatch in `main()`, `PROTECTED_BRANCH_RE`, and `PUSH_POLICIES` are the contract. Adding a guarded tool, protected branch, or push policy means an explicit edit there — don't infer behavior at runtime.
- Tokenize Bash commands with `shlex` via `command_segments`/`parse_git`; never go back to substring/regex matching on the raw command — that's the exact gap the python port closed. `GIT_VALUE_OPTS`/`PUSH_VALUE_OPTS` list the options that consume a following value token, and `PUSH_MANY_FLAGS` the ones that push more than one branch; extend these explicitly rather than guessing at parse time.
- The push guard defers (rather than asks) on refspec forms it can't classify — secure-by-default here means *not* fooling the user into thinking a push is blocked when it isn't. Hard guarantees belong in a git `pre-push` hook or server-side branch protection; keep `README.md`'s "best-effort" framing honest.
- On any uncertainty — not a git repo, detached HEAD, empty/missing input, unbalanced quotes (`shlex` raises `ValueError`), unresolvable branch — the hook **defers silently** (returns, emits nothing) so normal permissions apply. Never fail closed without an explicit reason.
- Default decision for a protected branch is `ask`, not `deny`. Hard-blocking is opt-in via a local edit and must be documented in `README.md` if introduced.
- Emit decisions only through the `emit()` helper (`json.dumps`) — don't hand-build decision JSON elsewhere.

## Security principles

**Secure by default, not opt-in.** This plugin exists to add a guardrail; its defaults must never trade away a security property for convenience. If a proposed change weakens any property — even partially, even with mitigations — the more secure behavior stays the default. The looser behavior may be offered as an explicit opt-in (env var, config, local edit) but must be documented as a trade-off.

Examples of regressions that must not silently become defaults:
- Flipping the protected-branch decision from `ask` to `allow`.
- Removing `main` or `master` from `PROTECTED_BRANCH_RE` because it was "noisy".
- Treating an unresolvable branch or unparseable input as `allow` rather than deferring.
- Auto-approving a command chain that mixes a `git commit` with a non-git command (e.g. `git commit && rm -rf foo`). Auto-allow fires only when *every* segment is a git invocation; widening this lets a trailing command ride along into a silent approval — the exact gap the Python port closed.
- Auto-approving a chain that contains a `git push` (it must route through the push guard, never the commit auto-allow), or weakening a push policy's default — `protected` must stay the default; `off` is opt-in only.
- Resolving the edit branch from the session cwd instead of the file's own repo (`git -C <dir-of-file>`), so edits through a checkout sitting on `main` are no longer caught.

When in doubt, ask before shipping. The hook's job is to add friction at the protected-branch boundary; removing friction is the change that needs sign-off, not adding it.

## Testing

Tests live in `test/run.sh`. Run with:

```bash
./test/run.sh
```

It spins up a throwaway git repo under `tmp/` and asserts the emitted `permissionDecision` for each tool/branch combination (commit on a feature branch, commit on `main`, all-git and mixed chains, env-prefixed/global-flag commits, non-commit `git` invocations, edits on protected vs non-protected branches, and defer cases). The harness parses the hook's JSON with `jq`, so `jq` is a test-only dependency.

When changing the decision logic, the git-commit detection, or `PROTECTED_BRANCH_RE`, add the case that motivated the change as a fixture, and hand-exercise the behavior table in `README.md` against the change before committing.

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
| Changing the decision logic, git-commit/git-push detection, or protected-branch set | `hooks/branch-guard.py` + `README.md` behavior & push-guard tables |
| Changing push-guard policies or the `BRANCH_GUARD_PUSH_POLICY` config | `hooks/branch-guard.py` (`push_decision`/`push_policy`/`PUSH_POLICIES`) + `README.md` "Push guard" section |
| Hook registration / matcher | `hooks/hooks.json` |
| Plugin packaging / marketplace listing | `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` |
| Testing the decision matrix | `test/run.sh` |
