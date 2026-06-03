# Privacy Policy — workspace-guard

_Last updated: 2026-06-02_

workspace-guard is a Claude Code plugin that runs entirely on your local
machine as a `PreToolUse` hook. Its only job is to add a confirmation prompt
before certain Bash commands read or write files outside your project
directory.

## Data we collect

None. The plugin has no analytics, no telemetry, and no network access. It
ships as a single Python script that uses only the standard library.

## How your data is handled

- The hook receives the Bash command Claude Code is about to run (via standard
  input) and your `CLAUDE_PROJECT_DIR` path (via an environment variable).
- It processes these **in memory** to decide allow / ask / deny, then writes
  the decision to standard output.
- It resolves file paths with `realpath` to catch symlink and `../` traversal.
  It does **not open or read the contents** of any file.
- Nothing is logged, stored, or written to disk by the plugin.

## Third parties

The plugin makes no network connections and shares no data with any third
party.

## Changes to this policy

Updates will be published in this file in the project repository, with the
date above revised accordingly.

## Contact

Questions or concerns:
<https://github.com/karlkfi/claude-workspace-guard/issues>
