# Privacy Policy — prod-guard

_Last updated: 2026-07-19_

prod-guard is a Claude Code plugin that runs entirely on your local machine
as a `PreToolUse` hook (plus a `PostToolUse` hook that records session-scoped
override grants). Its only job is to block or add a confirmation prompt
before certain Bash commands mutate infrastructure targets.

## Data we collect

None. The plugin has no analytics, no telemetry, and no network access. It
ships as a single Python script that uses only the standard library.

## How your data is handled

- The hook receives the Bash command Claude Code is about to run (via
  standard input), the working directory, plus a few optional `PROD_GUARD_*`
  configuration values (via environment variables).
- To resolve a command's ambient target it may read single values from local
  CLI config files you already have: the `current-context` line of your
  kubeconfig, the project of your active gcloud configuration, the
  `currentContext` of `~/.docker/config.json`, the default subscription in
  `~/.azure/azureProfile.json`, the `current-context` of
  `~/.config/argocd/config`, the `.terraform/environment` workspace file,
  and (for `gh` commands) the repository's `origin` remote URL via a local
  `git config` read.
- It processes these **in memory** to decide deny / ask / defer, then writes
  the decision to standard output. It does **not** read credentials, tokens,
  or any other content from those files.
- It never runs the guarded tools and never contacts a cluster or cloud API.
- The only thing it writes to disk is the local session-override grant store,
  `~/.claude/prod-guard/session-grants/<session-id>.json` — created only when
  you approve a `PROD_GUARD_SESSION_OVERRIDE=<reason>` command. Each file
  holds the target names you approved, the reason you gave, and a timestamp;
  grants expire after 8 hours, stale files are cleaned up automatically, and
  you can delete the directory at any time. Nothing leaves your machine.

## The friction-report command

prod-guard ships an optional `/prod-guard:friction-report` command (and the
`scripts/friction-report.py` script behind it). It is a **read-only** analyzer:
it re-reads the hook decisions Claude Code already recorded in your local session
transcripts under `~/.claude/projects/**/*.jsonl` and prints a summary. To flag
a stale install it also reads two plugin-manifest files Claude Code maintains —
`~/.claude/plugins/installed_plugins.json` and `known_marketplaces.json` — plus
the marketplace clone's `plugin.json`, taking only version strings and the clone
path. It adds no telemetry, makes no network connections, writes nothing to
disk, and never runs any guarded tool. Nothing leaves your machine.

## Third parties

The plugin makes no network connections and shares no data with any third
party.
