# Privacy Policy — branch-guard

_Last updated: 2026-11-25_

branch-guard is a Claude Code plugin that runs entirely on your local machine as a `PreToolUse` hook for `Bash`, `Edit`, `Write`, `MultiEdit` and `NotebookEdit` (plus a `PostToolUse` hook that records session-scoped worktree grants).
Its only job is to auto-approve routine git operations and add a confirmation prompt before protected-branch or destructive ones.

## Data we collect

None.
The plugin has no analytics, no telemetry, and no network access.
It ships as Python scripts that use only the standard library.

## How your data is handled

- The hook receives the command (or edit target) Claude Code is about to run (via standard input), the working directory, and a few optional `BRANCH_GUARD_*` configuration values (via environment variables).
- It shells out to local `git` to answer questions about your repository — the current branch, whether a ref is reachable, whether a path is ignored.
  These read your repository the same way any `git` command you ran yourself would.
- It processes all of this **in memory** to decide allow / ask / deny, then writes the decision to standard output.
- The plugin writes one kind of file, and only when you turn worktree grants on with `BRANCH_GUARD_WORKTREE_GRANTS=1`: `~/.claude/bouncer/session-grants/<session-id>.json` — created only when you approve a `git worktree add`.
  It holds the path of the checkout you approved, a fixed reason string, and a timestamp; grants expire after 8 hours, stale files are cleaned up automatically, and you can delete the directory at any time. workspace-guard reads the same file, which is why it is not under a guard-specific directory.
  With the setting off — the default — the plugin writes nothing to disk at all.
- Nothing leaves your machine.

## The friction report

`scripts/friction-report.py` and the `/branch-guard:friction-report` command are read-only analyzers over data Claude Code has **already** written to disk: your session transcripts under `~/.claude/projects/**/*.jsonl`.
To flag a stale install the report also reads `~/.claude/plugins/installed_plugins.json`, `known_marketplaces.json` and the marketplace clone's `marketplace.json`, taking version strings and the clone path.
It runs only when you run it, writes nothing to disk, and makes no network connections.

Those transcripts contain your commands and working directories, and the report prints excerpts of the commands to your terminal.
If you paste that output somewhere, you are pasting your own command history, so check it first and use `--repo` to narrow the scope.

## Third parties

The plugin makes no network connections and shares no data with any third party.

## Changes to this policy

Updates will be published in this file in the project repository, with the date above revised accordingly.

## Contact

Questions or concerns: <https://github.com/karlkfi/claude-bouncer/issues>
