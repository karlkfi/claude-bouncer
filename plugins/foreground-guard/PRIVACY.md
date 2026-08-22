# Privacy Policy — foreground-guard

_Last updated: 2026-07-18_

foreground-guard is a Claude Code plugin that runs entirely on your local
machine as a `PreToolUse` hook. Its only job is to block (or, where
configured, prompt before) certain Bash commands that waste the session's
main-thread time.

## Data we collect

None. The plugin has no analytics, no telemetry, and no network access. It
ships as a single Python script that uses only the standard library.

## How your data is handled

- The hook receives the Bash tool call Claude Code is about to run (command,
  timeout, run-in-background flag) via standard input, plus a few optional
  `FOREGROUND_GUARD_*` configuration values via environment variables.
- It may read your local `.claude/foreground-guard.json` config files (user
  and project) to load patterns and the slow-command registry.
- It processes these **in memory** to decide ask / deny / defer, then writes
  the decision to standard output. It writes nothing to disk, never runs the
  guarded commands, and never contacts any remote service.

## Third parties

The plugin makes no network connections and shares no data with any third
party.
