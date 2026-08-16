# Project Status

Single source of truth for progress and priorities in workspace-guard. Pick the next task from the top of the Queue.

**Status:** 🔲 ready · 🚫 blocked
**Size:** S = one session/PR · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `security` `tests` `docs` `infra` `bug` `parsing` `retro`
**Next ID:** Q76

**Maintaining this file:** see [`docs/development/maintaining-backlog.md`](development/maintaining-backlog.md).

## Queue

Specific actionable items in priority order. Pick from the top; skip 🚫 items until their blocker clears.

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q74"></a>Q74 | Escalate suppression in modes that pre-approve, so declining to vouch protects there | `security` | 🔲 | M | A withheld `allow` still runs under `auto`/`acceptEdits`/`bypassPermissions` ([matrix](permission-modes.md)). Escalating to `ask`/`deny` would block ~1,657 commands in `auto`, most routine heredocs — needs a narrower signal first. |
| <a id="Q75"></a>Q75 | Guard `cut` and `base64`, which read outside the root under an `allow` | `security` | 🔲 | S | Measured: `cat in.txt && cut -f1 /outside` returns `allow`, while `xxd`/`od`/`strings`/`wc` already ask. `cut` appears 1,090 times in the corpus. Add the two `SPEC` rows. |
| <a id="Q73"></a>Q73 | Extend the interpreter allow-suppression to the PowerShell tool | `security` | 🔲 | S | PowerShell still lets a clean guarded command vouch for interpreter code, unlike Bash since Q72: `Get-Content .\README.md; python3 -c '…'` returns `allow`. Port `interp_code_source` over. |

## Deferred

| ID | Item | Labels | Sz | Trigger to revive |
|---|---|---|---|---|
| <a id="Q23"></a>Q23 | Opt-in extra-roots for shared cross-worktree files | `security` | M | **Demand:** a session that legitimately needs cross-worktree shared files (mailbox files, the main checkout) and can't tolerate the prompts. Fix: an opt-in, empty-by-default extra-roots env var. |
| <a id="Q42"></a>Q42 | Catch a glob match that is itself a symlink out of the root | `security` | M | **Demand:** a glob-matched in-root name that points outside gets read silently. A glob resolves as the pattern, so `realpath` never sees the match. Closing it needs match enumeration. |
| <a id="Q47"></a>Q47 | Catch a `**` glob item that matches fewer segments than the pattern | `security` | M | **Demand:** a session runs `shopt -s globstar`. Verified: `docs/**` expands to `docs/` too, so a loop body's trailing `../` climbs above the root undetected. Issue 99's proxy needs fixed segments. |
| <a id="Q53"></a>Q53 | Grow the PowerShell coverage past the cmdlet table | `security` | M | **Demand:** a real outside read slips through. A .NET call, a native `.exe`, or an unlisted cmdlet is unchecked and silent; prompting on the unparsed tail was rejected as too noisy. See [Q51's known gaps](plan/q51-powershell-tool.md). |
| <a id="Q54"></a>Q54 | Quiet `$_` in a PowerShell `ForEach-Object` block | `parsing` | S | **Demand:** the friction report shows `$_` prompts accumulating. It reports as 'expand' like bash's `cat $f`, but carries far more of PowerShell's idiom. See [Q51's known gaps](plan/q51-powershell-tool.md). |
