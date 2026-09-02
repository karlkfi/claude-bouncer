# Privacy Policy — workspace-guard

_Last updated: 2026-06-02_

workspace-guard is a Claude Code plugin that runs entirely on your local
machine as a `PreToolUse` hook. Its only job is to add a confirmation prompt (or
a block) before certain Bash commands — and the file-editing tools (`Edit`,
`Write`, `MultiEdit`, `NotebookEdit`) — read or write files outside your project
directory or in a sibling checkout of the same git repo.

## Data we collect

None. The plugin has no analytics, no telemetry, and no network access. It
ships as a single Python script that uses only the standard library.

## How your data is handled

- The hook receives the command (or edit target) Claude Code is about to run
  (via standard input) and your `CLAUDE_PROJECT_DIR` path, plus a few optional
  `WORKSPACE_GUARD_*` configuration values and `TMPDIR` (via environment
  variables).
- It processes these **in memory** to decide allow / ask / deny, then writes
  the decision to standard output.
- It resolves file paths with `realpath` to catch symlink and `../` traversal.
  It does **not open or read the contents** of any version-controlled or
  user file.
- To detect sibling worktree checkouts of the same repo, it reads a few small
  **git metadata** files locally (`.git`, `commondir`, `HEAD`) under the target
  path and the session root. These are git's own bookkeeping, not your source.
- Nothing is logged or sent anywhere. The plugin writes exactly one kind of
  file, and only when you turn session grants on with
  `WORKSPACE_GUARD_SESSION_GRANTS=1`:
  `~/.claude/workspace-guard/session-grants/<session-id>.json` — created only
  when you approve an outside-workspace prompt. Each file holds the directories
  you approved, a fixed reason string, and a timestamp; grants expire after 8
  hours, stale files are cleaned up automatically, and you can delete the
  directory at any time. With the setting off — the default — the plugin writes
  nothing to disk at all. Nothing leaves your machine.
- It also *reads* `~/.claude/bouncer/session-grants/<session-id>.json` when that
  setting is on. branch-guard writes it when you approve a `git worktree add`,
  and it holds the paths of checkouts you approved this session.

## Third parties

The plugin makes no network connections and shares no data with any third
party.

## Changes to this policy

Updates will be published in this file in the project repository, with the
date above revised accordingly.

## Contact

Questions or concerns:
<https://github.com/karlkfi/claude-bouncer/issues>
