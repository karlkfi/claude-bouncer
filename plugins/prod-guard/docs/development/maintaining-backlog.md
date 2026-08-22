# Agent reference: Maintaining the backlog

The canonical rules for `docs/STATUS.md` — format, ID allocation, adding/completing/deferring items, grooming, parallel dispatch, and commit discipline — live in the globally installed [**`session-backlog` skill**](skills.md#session-backlog), which is installed on the workstation rather than committed here. Invoke that skill for any change to the Queue or header, and follow it rather than a copy of its rules here; this doc stays a pointer so the two cannot drift.

Repo-specific wiring:

- `scripts/lint-backlog.sh`, `scripts/next-task.sh`, and `scripts/backlog-metrics.sh` are vendored from the skill so the checks work without it installed.
- A pre-commit hook (`.githooks/pre-commit`, enabled via `git config core.hooksPath .githooks`) runs `lint-backlog.sh --staged`, which lints the file and rejects commits that stage `docs/STATUS.md` alongside other changes.
- The two rules that bite hardest, worth repeating: commit `docs/STATUS.md` changes in their own isolated commit, and run `gh pr list` before picking a task — the open PR, not a marker in the file, is the in-flight signal.
