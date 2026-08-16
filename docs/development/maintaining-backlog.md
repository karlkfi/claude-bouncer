# Agent reference: Maintaining the backlog

`docs/STATUS.md` is the single source of truth for project progress and priorities. Its format and maintenance process are defined by the globally-installed **[`session-backlog` skill](skills.md#session-backlog)** — invoke it for any change to the Queue or header rather than following rules copied here; a local copy would drift.

The load-bearing invariants, for sessions without the skill available:

1. **Isolate `docs/STATUS.md` edits in their own commit**, never mixed with code or plan-doc changes.
2. **Take new IDs from the `**Next ID:**` counter** in the file header and bump it in the same edit; IDs are stable and never reused or renumbered.
3. **Lint every edit** before committing: `scripts/lint-backlog.sh docs/STATUS.md`.

## Repo-local tooling

The skill's helper scripts are vendored in `scripts/`:

- `scripts/lint-backlog.sh` — format linter; `--staged` mode also rejects commits that stage `docs/STATUS.md` alongside other files.
- `scripts/next-task.sh` — emits the top ready Queue item as a session prompt (`--title` for a session name).
- `scripts/backlog-metrics.sh` — throughput/cycle-time/staleness report derived from git history (`--events` for a TSV stream).

The pre-commit gate lives at `.githooks/pre-commit`. It is enabled per-clone with:

```bash
git config core.hooksPath .githooks
```
