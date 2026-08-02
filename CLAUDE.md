# foreground-guard

A Claude Code plugin that adds a `PreToolUse` hook for `Bash`. It guards the session's **main-thread time**: Class A catches foreground polling/watching (`gh run watch`, `kubectl logs -f`, `tail -f`, `watch`, `sleep`-loops, repeat-with-sleep chains, long bare `sleep`s) and prompts `ask` (config may escalate to `deny`); Class B catches commands the repo has registered as slow when they are about to run with an inadequate Bash-call timeout, and prompts `ask` naming the exact fix (config may escalate to `deny`). Everything else defers silently. In an unattended permission mode (`auto`, `dontAsk`, `bypassPermissions`) an `ask` is emitted as a `deny` instead — equally blocking, but the reason reaches the agent so it self-corrects rather than parking on a prompt. See `README.md` for the decision table, covered forms, exemptions, and config reference.

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
- Fail OPEN on infrastructure errors (bad JSON, unreadable config, unexpected exception → silent defer). Unknown durations (`sleep $N`) lean toward friction (`ask`). Parsing uncertainty defers — this is a productivity guard, not a security boundary; a missed poll costs waiting, not an outage.
- Never parse heredoc bodies as command segments — `strip_heredoc_bodies` runs before tokenization (the workspace-guard #83 bug class). `bash some-script.sh` stays opaque — no script-file inspection.
- False-positive discipline is a contract: `tail -n 50`, `grep -f patterns.txt`, `git log --follow`, startup-grace sleeps below the floor, backgrounded segments, and `timeout N`-wrapped commands must not prompt. Add a regression test with any matcher change.

## Testing

Tests live in `tests/test_foreground_guard.py` (stdlib `unittest`, no third-party deps). Run with:

```
python3 -m unittest discover tests
```

Three layers: unit (heredoc/tokenize/split/sleep-parse), end-to-end (the script as a subprocess with a fixture `$HOME` and fixture `$CLAUDE_PROJECT_DIR` config; asserts deny/ask/defer), and wiring (hooks.json/plugin.json/marketplace.json validity and version lockstep). End-to-end tests must use a controlled env so the developer's real `~/.claude` config never leaks into a verdict.
