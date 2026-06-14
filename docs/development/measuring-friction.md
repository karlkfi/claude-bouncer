# Agent reference: Measuring prompt friction

`scripts/friction-report.py` ranks where workspace-guard's prompts accumulate,
so a "which prompts could we remove without weakening security?" review takes
one command instead of hand-mining transcripts.

## Where the data comes from

The hook writes nothing to disk (see [`PRIVACY.md`](../../PRIVACY.md)) — it only
emits a decision on stdout. But Claude Code records that stdout, along with the
triggering command, `cwd`, branch, and timestamp, in the session transcripts
under `~/.claude/projects/**/*.jsonl`. Each `PreToolUse:Bash` invocation lands
as an `attachment` record (`type: hook_success`) whose `command` names the guard
script and whose `stdout` carries the decision JSON; the triggering Bash command
is joined back via `toolUseID`.

The report only **reads** that already-persisted local data. It adds no logging
and no telemetry — keep it that way (hook-side logging would be a privacy
regression for no added signal).

## Usage

```
python3 scripts/friction-report.py                      # last 7 days, workspace-guard
python3 scripts/friction-report.py --since 24h
python3 scripts/friction-report.py --since 2026-06-01 --repo gateway
python3 scripts/friction-report.py --plugin all --raw --top 20
python3 scripts/friction-report.py --json               # machine-readable
```

Flags: `--transcripts` (root, default `~/.claude/projects`), `--plugin`
(`workspace-guard` default, or `all`, or another guard's basename), `--since`
(`Nd`/`Nh`/`Nm`, a `YYYY-MM-DD` date, or `all`), `--repo` (substring match on
`cwd`), `--top`, `--raw` (don't collapse volatile path segments), `--json`.

## What it reports

- **Outcome mix** — allow / ask / deny / defer counts and the ask+deny share
  (the friction ratio). `defer` is inferred from a silent hook (empty stdout).
- **By category** — `outside` / `expand` / `untracked`, the buckets
  `build_reason()` emits in `scripts/bash-workspace-guard.py`.
- **Top offending paths** — normalized by default so per-session temp paths
  (e.g. `/private/tmp/claude-NNN/...`) collapse into one row; `--raw` to see
  exact tokens.
- **Top triggering commands** — via the `toolUseID` join, so you see what the
  agent was doing when it got prompted.

## Interpreting the output

A token that surfaces in **top paths** but is not a real file (e.g. a bare `2`
from `2>/dev/null`) is a tokenizer false positive worth fixing. A high `expand`
count points at `~`/`$VAR` arguments; a high `untracked` count points at `cd`
into untracked directories. Cross-check candidate fixes against the
secure-by-default rules before changing the hook — a fix that cuts prompts by
loosening the boundary is a regression, not a win.
