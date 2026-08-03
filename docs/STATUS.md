# Project Status

Single source of truth for progress and priorities in workspace-guard. Pick the next task from the top of the Queue.

**Status:** 🔲 ready · 🚫 blocked
**Size:** S = one session/PR · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `security` `tests` `docs` `infra` `bug` `parsing` `retro`
**Next ID:** Q48

**Maintaining this file:** see [`docs/development/maintaining-backlog.md`](development/maintaining-backlog.md).

## Queue

Specific actionable items in priority order. Pick from the top; skip 🚫 items until their blocker clears.

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q46"></a>Q46 | Bound the loop-variable cross product so the hook can't hang | `security` `parsing` | 🔲 | S | Three nested `for`s over 256 literals each, then `cat $a/$b/$c`, hangs past 2 min. Predates Q41. A hung hook is non-blocking, so the guard enforces nothing. Over-cap must poison, not truncate. |
| <a id="Q43"></a>Q43 | Expand a leading `~` from the resolved home, not just `$HOME` | `parsing` `bug` | 🔲 | S | Sibling of Q40. `expand_tilde()` reads only `$HOME`, unset under cmd.exe, so `cat ~/x` keeps its `~` and the hook defers while bash expands it. Fix: reuse Q40's `expanduser`; strictly tightens. |
| <a id="Q45"></a>Q45 | Assert a skip ceiling in the Windows CI job | `tests` `infra` `retro` | 🔲 | S | Q39 left the Windows job a plain `unittest discover`, so a test that quietly stops running there goes unseen. Some already skip for want of `$HOME` (Q43). Assert a ceiling on the skip count. |

## Deferred

| ID | Item | Labels | Sz | Trigger to revive |
|---|---|---|---|---|
| <a id="Q23"></a>Q23 | Opt-in extra-roots for shared cross-worktree files | `security` | M | **Demand:** a session that legitimately needs cross-worktree shared files (mailbox files, the main checkout) and can't tolerate the prompts. Fix: an opt-in, empty-by-default extra-roots env var. |
| <a id="Q42"></a>Q42 | Catch a glob match that is itself a symlink out of the root | `security` | M | **Demand:** a glob-matched in-root name that points outside gets read silently. A glob resolves as the pattern, so `realpath` never sees the match. Closing it needs match enumeration. |
| <a id="Q47"></a>Q47 | Catch a `**` glob item that matches fewer segments than the pattern | `security` | M | **Demand:** a session runs `shopt -s globstar`. Verified: `docs/**` expands to `docs/` too, so a loop body's trailing `../` climbs above the root undetected. Issue 99's proxy needs fixed segments. |
| <a id="Q35"></a>Q35 | Don't scan `$(…)` inside a quoted-delimiter heredoc body | `parsing` | S | **Demand:** spurious `ask` on a `cat <<'EOF'` body with a literal `$(…)`. `command_substitutions()` scans it though a quoted delimiter stops bash expanding it. Fix: skip quoted-delimiter bodies. |
| <a id="Q44"></a>Q44 | Validate the guard against a real Windows install | `bug` `parsing` | M | **Demand:** a Windows user adopts the plugin or reports mis-parsing. Q39 made the suite pass against fixtures only — see [its plan](plan/q39-windows-tests.md). Unverified: that the `Bash` tool is Git Bash, and MSYS paths, which `ntpath` misreads. |
