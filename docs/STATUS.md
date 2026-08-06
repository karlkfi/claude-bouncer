# Project Status

Single source of truth for progress and priorities in workspace-guard. Pick the next task from the top of the Queue.

**Status:** 🔲 ready · 🚫 blocked
**Size:** S = one session/PR · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `security` `tests` `docs` `infra` `bug` `parsing` `retro`
**Next ID:** Q58

**Maintaining this file:** see [`docs/development/maintaining-backlog.md`](development/maintaining-backlog.md).

## Queue

Specific actionable items in priority order. Pick from the top; skip 🚫 items until their blocker clears.

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q56"></a>Q56 | Name the session scratchpad in the host-temp deny message | `docs` | 🔲 | S | `build_scratch_hint` steers only to the repo-local `./tmp/`, so an agent denied on `/tmp` infers the session scratchpad is off-limits too and litters the worktree instead. The hook knows the session's scratch dir; naming it as the second legitimate destination fixes the steering where it is actually read. Docs half done in issue 126. |
| <a id="Q55"></a>Q55 | Author release notes in `docs/releases/vX.Y.Z.md` | `docs` `retro` | 🔲 | S | Notes are typed straight into the GitHub Release today, so they never appear in a diff. Author each tag's body as a file, publish it with `gh release edit --notes-file`, and point the runbook at it. |
| <a id="Q57"></a>Q57 | Extend the unanchored-kill deny to PowerShell `Stop-Process` | `security` | 🔲 | S | The deny covers bash `pkill`/`killall` only ([plan](plan/unanchored-pkill-deny.md)). `Stop-Process -Name node` is the same host-wide kill, unchecked. Needs its own rule, not a `PS_SPEC` row. |

## Deferred

| ID | Item | Labels | Sz | Trigger to revive |
|---|---|---|---|---|
| <a id="Q23"></a>Q23 | Opt-in extra-roots for shared cross-worktree files | `security` | M | **Demand:** a session that legitimately needs cross-worktree shared files (mailbox files, the main checkout) and can't tolerate the prompts. Fix: an opt-in, empty-by-default extra-roots env var. |
| <a id="Q42"></a>Q42 | Catch a glob match that is itself a symlink out of the root | `security` | M | **Demand:** a glob-matched in-root name that points outside gets read silently. A glob resolves as the pattern, so `realpath` never sees the match. Closing it needs match enumeration. |
| <a id="Q47"></a>Q47 | Catch a `**` glob item that matches fewer segments than the pattern | `security` | M | **Demand:** a session runs `shopt -s globstar`. Verified: `docs/**` expands to `docs/` too, so a loop body's trailing `../` climbs above the root undetected. Issue 99's proxy needs fixed segments. |
| <a id="Q53"></a>Q53 | Grow the PowerShell coverage past the cmdlet table | `security` | M | **Demand:** a real outside read slips through. A .NET call, a native `.exe`, or an unlisted cmdlet is unchecked and silent; prompting on the unparsed tail was rejected as too noisy. See [Q51's known gaps](plan/q51-powershell-tool.md). |
| <a id="Q54"></a>Q54 | Quiet `$_` in a PowerShell `ForEach-Object` block | `parsing` | S | **Demand:** the friction report shows `$_` prompts accumulating. It reports as 'expand' like bash's `cat $f`, but carries far more of PowerShell's idiom. See [Q51's known gaps](plan/q51-powershell-tool.md). |
