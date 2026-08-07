# Project Status

Single source of truth for progress and priorities in workspace-guard. Pick the next task from the top of the Queue.

**Status:** 🔲 ready · 🚫 blocked
**Size:** S = one session/PR · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `security` `tests` `docs` `infra` `bug` `parsing` `retro`
**Next ID:** Q65

**Maintaining this file:** see [`docs/development/maintaining-backlog.md`](development/maintaining-backlog.md).

## Queue

Specific actionable items in priority order. Pick from the top; skip 🚫 items until their blocker clears.

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q61"></a>Q61 | Analyze the body of a shell `-c` command | `security` | 🔲 | M | The body is one opaque token, so no read, write or kill inside it is checked; Q60 only stopped the hook vouching for it. Recursing needs a container-exec exclusion first — see [plan](plan/q60-ps-pid-source.md). |
| <a id="Q64"></a>Q64 | Stop PowerShell subexpression analysis going exponential | `bug` `security` | 🔲 | S | 2^n: `ps_subexpressions` flattens every descendant, then `_ps_analyze_command` recurses into each and re-flattens. 20 nested `$(…)` takes 11s, 25 over 20s — a stalled hook enforces nothing. |

## Deferred

| ID | Item | Labels | Sz | Trigger to revive |
|---|---|---|---|---|
| <a id="Q23"></a>Q23 | Opt-in extra-roots for shared cross-worktree files | `security` | M | **Demand:** a session that legitimately needs cross-worktree shared files (mailbox files, the main checkout) and can't tolerate the prompts. Fix: an opt-in, empty-by-default extra-roots env var. |
| <a id="Q42"></a>Q42 | Catch a glob match that is itself a symlink out of the root | `security` | M | **Demand:** a glob-matched in-root name that points outside gets read silently. A glob resolves as the pattern, so `realpath` never sees the match. Closing it needs match enumeration. |
| <a id="Q47"></a>Q47 | Catch a `**` glob item that matches fewer segments than the pattern | `security` | M | **Demand:** a session runs `shopt -s globstar`. Verified: `docs/**` expands to `docs/` too, so a loop body's trailing `../` climbs above the root undetected. Issue 99's proxy needs fixed segments. |
| <a id="Q53"></a>Q53 | Grow the PowerShell coverage past the cmdlet table | `security` | M | **Demand:** a real outside read slips through. A .NET call, a native `.exe`, or an unlisted cmdlet is unchecked and silent; prompting on the unparsed tail was rejected as too noisy. See [Q51's known gaps](plan/q51-powershell-tool.md). |
| <a id="Q54"></a>Q54 | Quiet `$_` in a PowerShell `ForEach-Object` block | `parsing` | S | **Demand:** the friction report shows `$_` prompts accumulating. It reports as 'expand' like bash's `cat $f`, but carries far more of PowerShell's idiom. See [Q51's known gaps](plan/q51-powershell-tool.md). |
