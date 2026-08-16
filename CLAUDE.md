# prod-guard

A Claude Code plugin that adds a `PreToolUse` hook for `Bash` (plus a `PostToolUse` hook that records session-scoped override grants). When a mutating infrastructure command (`kubectl`/`oc`, `helm`, `gcloud`, `aws`, `az`, `terraform`, `docker`/`podman`/`nerdctl`, `gh`, `ssh`, `flux`, `argocd`, `eksctl`, `doctl`, `kubectx`/`kubens`) resolves to a production target, the hook returns `deny`; when the target is unknown or comes from clobber-prone ambient context, it returns `ask`; everything else defers silently. See `README.md` for the user-facing overview and the decision table, and `docs/design.md` for the two threat models (prod blast-radius, ambient-context clobbering) and why each decision default was chosen.

The load-bearing piece is `scripts/bash-prod-guard.py` — a stdlib-only Python script that tokenizes the command with `shlex`, splits it into simple commands (recursing into `sh -c`/`eval` bodies), resolves each covered tool's effective target (explicit flag → local config file → unknown), classifies it against configurable regex patterns, and emits a `PreToolUse` decision.

## Development philosophy

Build the right thing AND build it well. Before writing any code, state the goal in one sentence and the approach in two or three. If the goal is unclear, ask one focused question rather than guessing.

Make the smallest change that achieves the goal. If you notice problems outside the current task's scope, flag them rather than fixing them:
- New near-term work → add a row to the Queue in `docs/STATUS.md` in priority order.
- Larger / speculative work → add a row to the Deferred table in `docs/STATUS.md` with a concrete revive trigger (`**Demand:**` / `**Event:**` / `**Decision:**`).

Capture knowledge durably, don't leave it in chat. When the user states a standing preference or decision, persist it in the repo (CLAUDE.md, the relevant `docs/` file, or memory) rather than applying it once and moving on. When follow-up work surfaces mid-task, record it on the Queue in `docs/STATUS.md` — including the *why* of any decision it depends on — instead of only mentioning it in the response.

Before introducing a new pattern or abstraction, check whether the existing model already solves the problem: a new tool is usually an `EVALUATORS` entry (often an alias of an existing one), a new verb is a table row, a new environment name is a config pattern — not a parser change.

## Workflow

1. **At session start, check whether the worktree is stale.** Run `git fetch origin main` and compare with `git log --oneline HEAD..origin/main`; if `origin/main` has new commits, rebase before doing any other work.
   - **Work on a `claude/`-prefixed branch, never on `main`.** In a worktree session, do all work via the worktree path.
2. **Before making changes** — read `README.md` and skim `scripts/bash-prod-guard.py` so the proposed change matches the existing parsing/policy model. If picking the next task, run `gh pr list` first and skip any Queue item from `docs/STATUS.md` already covered by an open PR.
   - **Verify 🚫 blockers are still real.** Grep for the deliverables before treating an item as blocked.
   - **Investigation findings marked ✅ must be end-to-end verified, not just source-read.** Shell and CLI parsing are full of surprises that only show up when you exec the thing — but see the fixture-safety rule in Testing before exec'ing anything.
3. **For complex tasks** — write an explicit plan to `docs/plan/<slug>.md` and follow it. Keep it updated so completed scope is verifiable at the end.
4. **After making changes** — review the diff. Update docs proactively:
   - **Changed decision semantics, verb tables, or covered tools** → update the decision table and "Covered tools" section in `README.md`, and `docs/design.md` if a rationale changed.
   - **New configuration or hook surface** → `README.md` Configuration section and `.claude-plugin/plugin.json` keywords/description.
   - Update `docs/STATUS.md`: remove the completed Queue row.
5. **Commit when done** — small, focused, Conventional Commits. **Always commit `docs/STATUS.md` changes in their own isolated commit** (see `docs/development/maintaining-backlog.md`).

## Code standards

### Python (`scripts/bash-prod-guard.py`)

- Stdlib only — no third-party deps. The hook runs on whatever `python3` the user has on their PATH (3.10+ per CI).
- The per-tool tables are the contract: read-only verb sets are **allowlists** — any verb not listed is mutating. Never "fix" a spurious prompt by widening a default; add the specific read-only verb with a test.
- On parsing uncertainty the split must err toward *more* segments to inspect, never fewer. False negatives (a missed destructive form) are the failure mode to fear; a false positive costs one prompt.
- Fail OPEN on infrastructure errors (bad JSON, unreadable config, unexpected exception → silent defer). Fail CLOSED on the security decision (unknown target + mutating verb → ask). Do not mix these up: they point in opposite directions on purpose.
- The hook never emits `allow` — only `deny`, `ask`, or silence — so it composes with the sibling guards instead of overriding them.
- No network calls, no invoking the guarded tools, no cluster access. Ambient resolution is local file reads (plus one local `git config` subprocess for gh).

## Security principles

**Secure by default, not opt-in.** This plugin exists to add a guardrail; its defaults must never trade away a security property for convenience. If a proposed change weakens any property — even partially, even with mitigations — the more secure behavior stays the default. The looser behavior may be offered as an explicit opt-in (env var, config, local edit) but must be documented as a trade-off.

Examples of regressions that must not silently become defaults:
- Flipping unknown-target + mutating from `ask` to silent defer.
- Making config patterns replace the built-ins instead of extending them.
- Turning the `PROD_GUARD_OVERRIDE` downgrade into a silent allow.
- Moving a verb to a read-only table because it was "noisy", without verifying it cannot mutate (e.g. `kubectl exec`, `aws s3 cp`).
- Emitting `allow` for any case, ever.

When in doubt, ask before shipping. The hook's job is to add friction at the security boundary; removing friction is the change that needs sign-off, not adding it.

## Testing

Tests live in `tests/test_prod_guard.py` (stdlib `unittest`, no third-party deps). Run with:

```
python3 -m unittest discover tests
```

Three layers: unit tests import the module (classification, tokenization, wrapper stripping); end-to-end tests invoke the script as a subprocess with a **fixture `$HOME`** (synthetic kubeconfig/gcloud/docker/azure configs) and assert the emitted decision; wiring tests assert hooks.json/plugin.json/marketplace.json agree.

When changing tables or policy, add the case that motivated the change as a fixture, and hand-exercise the README decision table against the change before committing.

**Never use real production names, contexts, projects, or credentials paths in test fixtures or hand-exercised commands.** Use synthetic placeholders (`gke_acme_prod-us`, `kind-ci`, `bluefin`). And never hand-exercise a bypass attempt through the user's real `Bash` tool against a real tool on PATH — if the hook erroneously defers, bash will *run* the command. The subprocess tests read the command as a JSON string and never execute it; add cases there instead.

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

Human-facing docs (`README.md`, anything under `docs/` outside `docs/development/`) must never link to `CLAUDE.md` or `AGENTS.md`. This file is the entrypoint for Claude/agents only; humans start at `README.md`. The dependency direction is one-way: `CLAUDE.md` may link out to `docs/` and `README.md`, but nothing under those may link back to it.

**Editing `CLAUDE.md` — protect the context budget.** This file is loaded in full into every session, so every line costs context. Keep it lean: add only load-bearing, must-act-on rules, and put the explanation/how-to in the relevant `docs/` page with a one-line pointer here rather than growing a self-contained copy past a few sentences. When in doubt, write the detail in `docs/` and link it; prefer tightening an existing line over adding a new one.

## Agent reference docs

When working on specific tasks, read the relevant doc before starting:

| Task | Reference |
|---|---|
| Picking the next task, tracking progress, adding new items | `docs/STATUS.md` — also run `gh pr list` and skip any Queue item already covered by an open PR |
| Editing `docs/STATUS.md` (any change to the Queue or header) | Invoke the `session-backlog` skill; repo wiring in `docs/development/maintaining-backlog.md` |
| Naming or linking a globally installed skill from a doc | `docs/development/skills.md` — link that page's anchor, never a `~/.claude/skills/...` path |
| Changing decision semantics, verb tables, or covered tools | `scripts/bash-prod-guard.py` + `README.md` decision table + `docs/design.md` |
| Plugin packaging / marketplace listing | `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json` |
| Cutting a release (version bump, tag, GitHub Release) | `docs/development/release-process.md` |
| Rendering or regenerating brand images (social preview, favicon, icon) | `docs/development/rendering-images.md` |
| Hook registration | `hooks/hooks.json` |
