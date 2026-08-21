# foreground-guard

A Claude Code plugin that adds a `PreToolUse` hook for `Bash`. It guards the session's **main-thread time**: Class A catches foreground polling/watching (`gh run watch`, `kubectl logs -f`, `tail -f`, `watch`, `sleep`-loops, repeat-with-sleep chains, long bare `sleep`s); Class B catches commands the repo has registered as slow when they are about to run with an inadequate Bash-call timeout. Everything else defers silently.

Both classes **deny**, and the reason carries the rewrite the agent applies. The verdict routes the uncertainty to whoever can resolve it, and here that is never the human: the resource at stake is the session's own main thread, so a person at the prompt holds no fact the agent lacks, and Class B's fixes are parameters of the Bash call that only the agent can re-issue. `"action": "ask"` on either class de-escalates it to a prompt — the supervised posture, opt-in because it is the setting that spends attention. A `FOREGROUND_GUARD_OVERRIDE=<reason>` prefix makes the hook defer, in every permission mode. See `README.md` for the decision table, covered forms, exemptions, and config reference.

The load-bearing piece is `scripts/bash-foreground-guard.py` — a stdlib-only Python script that strips heredoc bodies textually, tokenizes with `shlex`, splits into simple commands (recursing into `bash -c`/`eval` bodies), classifies each segment (watch/follow registry, loop-with-sleep, sandwich, bare sleep), checks the Class B registry against the call's timeout, and emits a `PreToolUse` decision.

## Development philosophy

Build the right thing AND build it well. Before writing any code, state the goal in one sentence and the approach in two or three. If the goal is unclear, ask one focused question rather than guessing.

Make the smallest change that achieves the goal. Before introducing a new pattern or abstraction, check whether the existing model already solves the problem: a new watch form is usually a `BUILTIN_WATCH` row (or a config `extra_watch_patterns` example in the README), a new slow command is per-repo config — not a parser change.

## Workflow

1. **Work on a `claude/`-prefixed branch, never on `main`.** At session start run `git fetch origin main` and rebase if behind.
2. **Before making changes** — read `README.md` and skim `scripts/bash-foreground-guard.py` so the proposed change matches the existing parsing/policy model.
3. **After making changes** — review the diff; update the README decision table / covered-forms / config sections when semantics change; keep `plugin.json` and `marketplace.json` versions in lockstep.
4. **Commit when done** — small, focused, Conventional Commits.

Work is tracked in [`docs/STATUS.md`](docs/STATUS.md): pick the next task from the top of the Queue, run `gh pr list` before picking, and commit `docs/STATUS.md` changes in isolated `docs(status):` commits.

## Code standards

### Python (`scripts/bash-foreground-guard.py`)

- Stdlib only — no third-party deps. The hook runs on whatever `python3` the user has on their PATH (3.10+ per CI).
- The hook never emits `allow` — only `deny`, `ask`, or silence — so it composes with the sibling guards (workspace-guard, branch-guard, prod-guard) instead of overriding them. This invariant is asserted on every end-to-end test call; never weaken it.
- Every reason opens `foreground-guard: ` — the name, a colon, one space, at position 0, on `ask` and `deny` alike, with no exempt path. Claude Code names the plugin in neither the prompt nor the deny text, and a deny leaves no hook attachment, so the opener is the only trace. Asserted on every end-to-end call against the shipped reader's own regex. See [`docs/development/cross-guard-deny-convention.md`](docs/development/cross-guard-deny-convention.md).
- Prefer a `deny` carrying a runnable rewrite over an `ask`. An `ask` is for what only a person can resolve, and nothing here qualifies — a deny's false positive costs the agent one retry, an `ask`'s costs a person a context switch on every matching call. A new finding needs a fix that is pasteable, not a principle.
- Fail OPEN on infrastructure errors (bad JSON, unreadable config, unexpected exception → silent defer). Unknown durations (`sleep $N`) lean toward blocking, and the reason asks for the literal — an argument the hook cannot expand is never routed at a human, who cannot expand it either. Parsing uncertainty defers — this is a productivity guard, not a security boundary; a missed poll costs waiting, not an outage.
- Never parse heredoc bodies as command segments — `strip_heredoc_bodies` runs before tokenization (the workspace-guard #83 bug class). `bash some-script.sh` stays opaque — no script-file inspection.
- False-positive discipline is a contract: `tail -n 50`, `grep -f patterns.txt`, `git log --follow`, startup-grace sleeps below the floor, backgrounded segments, and `timeout N`-wrapped commands must defer silently. Add a regression test with any matcher change.

## Testing

Tests live in `tests/test_foreground_guard.py` (stdlib `unittest`, no third-party deps). Run with:

```
python3 -m unittest discover tests
```

Three layers: unit (heredoc/tokenize/split/sleep-parse), end-to-end (the script as a subprocess with a fixture `$HOME` and fixture `$CLAUDE_PROJECT_DIR` config; asserts deny/ask/defer), and wiring (hooks.json/plugin.json/marketplace.json validity and version lockstep). End-to-end tests must use a controlled env so the developer's real `~/.claude` config never leaks into a verdict.
