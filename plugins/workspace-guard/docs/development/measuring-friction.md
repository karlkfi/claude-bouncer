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

**A deny is not in that stream.** Claude Code persists hook stdout only for a
call it then runs, so a blocked call records no decision at all. Measured
2026-08-21 over 1,034 local transcripts: 60,858 workspace-guard decisions, every
one of them `allow` or `ask`, while 80 denials sat in plain sight as tool
results. The report reads those back instead — the error a blocked call hands
the agent is verbatim the reason the hook printed, joined to the command by
`tool_use_id` like any other tool result. Two keys find it:

- the `<name>-guard: ` opener every reason now carries, a cross-guard
  convention, so a companion guard's denies are recovered under `--plugin all`
  too;
- this guard's own reason wording, which is the only key a deny recorded before
  the opener has. The opener arrived after v1.10.0, so all 80 above are of that
  vintage.

Neither key alone says *deny*, because an `ask` is worded identically and a
prompt the operator declines comes back as an error carrying the same text
(measured on one `~/.zshrc` ask that did exactly that). What separates them is
the attachment: the ask left one, the deny left none. So a result whose
`(guard, toolUseID)` already appears in the decision stream is dropped rather
than counted twice.

The report only **reads** that already-persisted local data. It adds no logging
and no telemetry — keep it that way (hook-side logging would be a privacy
regression for no added signal).

## What the report cannot see

Claude Code records an attachment **only when the hook writes to stdout**. A
hook run that stays silent leaves no record at all — not an empty one. Measured
on one workstation: of 20,708 `PreToolUse:Bash` attachments, 37 had empty
stdout, and all 37 came from a shell hook the report skips anyway.

So the scan cannot see:

- **Silent defers.** workspace-guard defers on every command it doesn't guard,
  which is most of them. Those runs are absent, not counted as `defer`.
- **Unreached code paths.** A guard that returns before analysis on some class
  of payload records nothing for that class. foreground-guard returns early when
  the payload carries `run_in_background: true`, so backgrounded polls are
  invisible ([foreground-guard
  #15](https://github.com/karlkfi/claude-foreground-guard/issues/15)).
- **A guard that never ran** — misconfigured, crashed, or not installed.
- **A block of a native tool.** The scan is scoped to `PreToolUse:Bash` on both
  passes, so a guard's deny of an `Edit`, `Write`, or `Read` is out of frame.
- **A deny neither key finds.** Under `--plugin all`, a companion guard whose
  block text opens with something other than `<name>-guard: ` under-counts its
  denies — pr-sentinel leads with `pr-sentinel: `, for instance. The coverage
  line says so rather than showing a zero.

Every total is therefore a **floor**, and each case above is indistinguishable
from a guard that saw the traffic and stayed quiet. The report
prints a `coverage:` line under the header saying so, and `--json` carries the
same sentences in a `coverage` field.

The consequence worth remembering: because each guard emits on its own terms —
some emit an explicit `allow` for traffic they've cleared, others emit only when
they need to prompt — the per-guard totals on the `plugins:` line are **not
comparable to each other**. Only ask/deny counts are, and only as floors.

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
`cwd`), `--top`, `--raw` (don't collapse volatile path segments), `--json`,
`--plugins-dir` (Claude Code plugins dir, default `~/.claude/plugins`; used only
for the stale-install check below).

A `--plugin` value is a **guard label**, derived from the hook script's filename
minus a `bash-` prefix — not the plugin name. They coincide for the guards whose
hook is `bash-<name>.py`, but not in general: pr-sentinel's hook is
`pr-sentinel-guard.py`, so its label is `pr-sentinel-guard`. Labels you never
installed can also appear, since any `PreToolUse:Bash` hook running a `.py`
script gets one.

## When nothing matches

An empty result names the filter that emptied it, so a typo can't read like a
guard with zero friction (issue 97):

```
$ python3 scripts/friction-report.py --plugin pr-sentinel --since all
No guard decisions found for the given filters.
--plugin 'pr-sentinel' matched no guard in the scanned transcripts.
  Guards found: workspace-guard (16563), branch-guard (3045), pr-sentinel-guard (535), ...
```

A label can be missing for a second reason the message also names: the guard is
installed but emitted nothing in the scanned transcripts, which leaves no record
to derive a label from.

The filters are checked in order — `--plugin`, then `--repo`, then `--since` —
and the first one that drops everything is the one reported; the `--since` case
dates the most recent matching decision so you know how far to widen. `--json`
carries the same signal in `guards_seen` and `empty_because` (`null` when the
report is non-empty).

**Exit codes.** A `--plugin` or `--repo` value nothing in the transcripts can
match exits **2** — the question is unanswerable as asked. A filter that does
match the data but over an empty window exits **0**, as does a fresh setup with
no recorded decisions at all: those are real answers of zero.

## What it reports

- **Outcome mix** — allow / ask / deny counts and the ask+deny share (the
  friction ratio), over the decisions a hook actually emitted (see [What the
  report cannot see](#what-the-report-cannot-see)). A `defer` bucket exists for
  an attachment with empty stdout, but silent runs aren't recorded at all, so in
  practice it stays at zero.
- **By category** — `outside` / `expand` / `untracked` / `hosttemp` /
  `sibling` / `crosssession` / `kill`, the buckets `build_reason()` emits in
  `scripts/bash-workspace-guard.py`, plus `other` for any prompt whose reason
  matches none of them. The last four are denies in the default configuration,
  so they appear only once the denies above are counted; `sibling`,
  `crosssession` and `kill` carry their targets inline rather than as a token
  list, so they add no rows to the path ranking. Under `--plugin all` `other` is where the companion guards'
  prompts land, so the table still sums to the friction count in the header (a
  reason can match more than one bucket, so the sum can exceed it).
- **Top offending paths** — only `outside`/`expand`/`untracked` reasons carry
  path tokens, so this ranking is workspace-guard-only even under
  `--plugin all`, where its heading says so (`--json` carries the same signal in
  `paths_scope`). Normalized by default so per-session temp paths (e.g.
  `/private/tmp/claude-NNN/...`) collapse into one row; `--raw` to see exact
  tokens.
- **Top triggering commands** — via the `toolUseID` join, so you see what the
  agent was doing when it got prompted.
- **Stale-install banner** — when the installed plugin version
  (`~/.claude/plugins/installed_plugins.json`) is behind the local marketplace
  clone's `plugin.json`, a line above the rankings reads
  `installed X, Y available` with the update command. Third-party git
  marketplaces don't auto-update by default, so friction a newer release already
  fixes can linger; this instruments that trap. It reads only already-persisted
  local plugin state (no network) and stays silent when the install is current
  or the state is unreadable. The `--json` output carries the same signal in a
  `stale` field (`null` when current).

## Interpreting the output

A token that surfaces in **top paths** but is not a real file (e.g. a bare `2`
from `2>/dev/null`) is a tokenizer false positive worth fixing. A high `expand`
count points at `~`/`$VAR` arguments; a high `untracked` count points at `cd`
into untracked directories. A large `other` under `--plugin all` means most of
your friction comes from a companion guard — read the `plugins:` line to see
which, then run that guard's own copy of this report to break it down.
Cross-check candidate fixes against the
secure-by-default rules before changing the hook — a fix that cuts prompts by
loosening the boundary is a regression, not a win.
