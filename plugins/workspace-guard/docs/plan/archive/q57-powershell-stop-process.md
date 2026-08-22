# Plan: extend the unanchored-kill deny to PowerShell `Stop-Process` (Q57)

**Goal:** deny `Stop-Process` when nothing ties it to this workspace, so the
PowerShell frontend can't reach a sibling worktree's processes through the door
the bash `pkill`/`killall` deny already closed.

**Approach:** a PowerShell-side rule alongside the existing one, sharing the
`'kill'` offender category, the `ctx.kill_anchor` regex, `decide`, and
`WORKSPACE_GUARD_OVERRIDE`. Selection is classified per `Stop-Process` segment;
the anchor is looked for across the whole **statement**, because the anchored
rewrite is a pipeline.

Queue row: Q57, filed by [the bash deny's plan](unanchored-pkill-deny.md), whose
"Bash only" limitation this closes.

## Why the rule differs from `pkill`'s

`pkill` has one selection mode — a pattern that may or may not contain a path.
`Stop-Process` has three, and they need different verdicts:

| Selection | Verdict | Why |
|---|---|---|
| `-Id 1234` (or positional `1234`) | defer | The bash side leaves `kill <pid>` untouched for the same reason: killing by pid is the rewrite the deny recommends, not a hazard. |
| `-Name node` | **deny**, always | This is `killall`. A process name carries no path, so nothing anywhere in the statement can scope it to this workspace. |
| `-InputObject`, a non-pid positional, or nothing at all (pipeline input) | **deny** unless the statement anchors | The processes come from somewhere the hook can see — `Get-Process`, a `Where-Object` filter — so the statement is where an anchor can appear. |

An `-Id` value carrying a `$` is *not* the pid case: `Stop-Process -Id $p.Id`
after `$p = Get-Process -Name node` is host-wide, and the hook can't see the
difference. Only literal digits (or a comma-joined list of them) count.

## Why the scope is the statement, not the segment

The rewrite the deny message recommends has to actually pass, or the message
lies. In PowerShell the anchored form is a pipeline:

```
Get-Process | Where-Object { $_.Path -like 'C:\ws\repo\*' } | Stop-Process
```

The `Stop-Process` segment holds no anchor at all — the filter two segments
upstream does. So the anchor scan covers every word token in the statement,
where a statement ends at `;`, a newline, `&&`, `||` or `&`. `|`, `(`/`)` and
`{`/`}` all stay inside it, which is what keeps the pipeline and any script-block
body in view.

The same reasoning forces the pipeline form to be covered at all: leaving
`Get-Process node | Stop-Process` alone would make the denial of
`Stop-Process -Name node` train the agent straight into the idiomatic bypass.

Cost of the wider scope: a statement that names a workspace path for an
unrelated reason (`Get-Content <root>\list.txt | Stop-Process`) reads as
anchored. That direction is the same one the bash rule already accepts — an
operand naming this workspace is treated as scoping the kill to it.

## Reused, not rebuilt

- `ctx.kill_anchor` / `workspace_anchor_re` — the identical component-with-a-
  separator rule, so a bash and a PowerShell kill can't reach different verdicts
  about the same path.
- The `'kill'` offender category, `decide`'s `cross_hit` deny, and
  `WORKSPACE_GUARD_OVERRIDE`. One category, one override, one message shape.
- `ps_resolve_param` for PowerShell's prefix-matching parameter names.

New: `ps_kill_operand_anchored`, which replaces `kill_operand_anchored`'s bash
resolution with PowerShell's — `~`/`~\…` expands via `ps_expand_tilde`, and the
tokenizer's `expandable` flag stands in for `EXPANSION_RE` (`ps_subexpressions`
has already reduced a `$(…)` to a bare `$`, so a subexpression can never anchor).
`resolve_subst_prefix` has no PowerShell counterpart and is not used.

`build_kill_hint` gains a `shell` key on the detail dict, which selects the fix
sentence — `pgrep -fl` for bash, `Get-Process`/`Stop-Process -Id` and the
`Where-Object` filter for PowerShell. Everything else about the message is shared.

## Deliberate limitations

- **`taskkill` is not covered**, on either frontend. It is the same host-wide
  kill and reachable from both shells, but it needs a row in each and is
  orthogonal to this change; filed as a Queue row.
- **`Get-Process -Id 1234 | Stop-Process` denies.** The pid is literal but it is
  bound on the wrong cmdlet, and reading `-Id` off arbitrary upstream cmdlets
  means a spec row for each. The rewrite (`Stop-Process -Id 1234`) is the first
  thing the message names.
- **A process started with a relative command line can't be anchored**, because
  its `Path` is what the filter matches. Same as the bash side: the answer there
  is `Get-Process` and then kill by pid.
- **Nested worktrees under the project root count as in-workspace**, as
  everywhere else in the guard.

## Deliverables

- [x] `scripts/bash-workspace-guard.py` — `PS_KILL_CMDS` / `PS_KILL_SPEC` /
      `PS_KILL_SELECTORS`, `ps_pid_list`, `ps_classify_kill`,
      `ps_kill_operand_anchored`, `ps_statement_kills`, `ps_strip_head`
      extracted from `ps_analyze_segment`, statement tracking in
      `ps_analyze_command`, `shell` branch in `build_kill_hint`.
- [x] Tests — unit (selection classification, pid literals, alias resolution,
      anchor expansion handling) + e2e (`-Name` deny, pipeline deny, anchored
      pipeline defer, `-Id` defer, override downgrade, bypass deny,
      statement-boundary isolation).
- [x] `README.md` — decision-table rows, the kill section, How it works step 13,
      Limitations, agent-guidance bullet, PowerShell coverage.
- [x] `.claude-plugin/plugin.json` — nothing; `powershell`, `pkill` and
      `process` are all already in the keyword list.
- [x] `docs/design.md` — why the scope had to widen from the command to the
      statement.
- [x] `docs/STATUS.md` — drop Q57, add Q58 (`taskkill`) and Q59.
