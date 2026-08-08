# Project Status

Single source of truth for progress and priorities in workspace-guard. Pick the next task from the top of the Queue.

**Status:** 🔲 ready · 🚫 blocked
**Size:** S = one session/PR · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `security` `tests` `docs` `infra` `bug` `parsing` `retro`
**Next ID:** Q69

**Maintaining this file:** see [`docs/development/maintaining-backlog.md`](development/maintaining-backlog.md).

## Queue

Specific actionable items in priority order. Pick from the top; skip 🚫 items until their blocker clears.

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q65"></a>Q65 | Stop a plain `printf` invalidating a tracked variable | `parsing` `bug` | 🔲 | S | `printf` sits in `ARG_ASSIGNER_CMDS` for `printf -v f`, so `printf "%s" "$f"` also drops `f` and a later `docs/$f.md` prompts. Fix: treat `printf` as assigning only when `-v` is present. |
| <a id="Q66"></a>Q66 | Resolve tracked variables inside a command substitution | `parsing` `bug` | 🔲 | M | `f=docs/STATUS.md; echo "$(grep -c X "$f")"` prompts on `$f`; the same command without the `$( )` allows. Fix: pass the assignment map into substitution analysis, keeping Q63's recursion cap. |
| <a id="Q67"></a>Q67 | Keep variable tracking alive across a heredoc | `parsing` `bug` | 🔲 | S | A heredoc earlier in the command drops the variable map: `SP=<literal>`, a heredoc, then `$SP/x` prompts — literal paths still resolve. Fix: preserve the map across heredoc extraction. |
| <a id="Q68"></a>Q68 | Archive the shipped plan docs | `docs` | 🔲 | S | Twelve shipped plans still sit in `docs/plan/`; only `q51-powershell-tool.md` is still referenced from the Queue. Move the rest to `docs/plan/archive/` and repair the links between them. |

## Deferred

| ID | Item | Labels | Sz | Trigger to revive |
|---|---|---|---|---|
| <a id="Q23"></a>Q23 | Opt-in extra-roots for shared cross-worktree files | `security` | M | **Demand:** a session that legitimately needs cross-worktree shared files (mailbox files, the main checkout) and can't tolerate the prompts. Fix: an opt-in, empty-by-default extra-roots env var. |
| <a id="Q42"></a>Q42 | Catch a glob match that is itself a symlink out of the root | `security` | M | **Demand:** a glob-matched in-root name that points outside gets read silently. A glob resolves as the pattern, so `realpath` never sees the match. Closing it needs match enumeration. |
| <a id="Q47"></a>Q47 | Catch a `**` glob item that matches fewer segments than the pattern | `security` | M | **Demand:** a session runs `shopt -s globstar`. Verified: `docs/**` expands to `docs/` too, so a loop body's trailing `../` climbs above the root undetected. Issue 99's proxy needs fixed segments. |
| <a id="Q53"></a>Q53 | Grow the PowerShell coverage past the cmdlet table | `security` | M | **Demand:** a real outside read slips through. A .NET call, a native `.exe`, or an unlisted cmdlet is unchecked and silent; prompting on the unparsed tail was rejected as too noisy. See [Q51's known gaps](plan/q51-powershell-tool.md). |
| <a id="Q54"></a>Q54 | Quiet `$_` in a PowerShell `ForEach-Object` block | `parsing` | S | **Demand:** the friction report shows `$_` prompts accumulating. It reports as 'expand' like bash's `cat $f`, but carries far more of PowerShell's idiom. See [Q51's known gaps](plan/q51-powershell-tool.md). |
