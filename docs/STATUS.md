# Project Status

Single source of truth for progress and priorities in workspace-guard. Pick the next task from the top of the Queue.

**Status:** 🔲 ready · 🚫 blocked
**Size:** S = one session/PR · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `security` `tests` `docs` `infra` `bug` `parsing` `retro`
**Next ID:** Q53

**Maintaining this file:** see [`docs/development/maintaining-backlog.md`](development/maintaining-backlog.md).

## Queue

Specific actionable items in priority order. Pick from the top; skip 🚫 items until their blocker clears.

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q51"></a>Q51 | [Guard the PowerShell tool on Windows](plan/q51-powershell-tool.md) | `security` | 🔲 | M | Without Git for Windows there is no Bash tool: Claude Code runs shell commands through `PowerShell`, which `hooks.json` doesn't match, so nothing is checked. `shlex` is the wrong parser for it. |
| <a id="Q52"></a>Q52 | Read Git Bash path forms the way Git Bash does | `bug` `parsing` | 🔲 | S | `/c/Users/x` and `/etc/passwd` resolve against the current drive, so prompts name a path the command never touches and MSYS-form config entries match nothing. No silent allow — see [Q44's findings](plan/q44-windows-validation.md). |

## Deferred

| ID | Item | Labels | Sz | Trigger to revive |
|---|---|---|---|---|
| <a id="Q23"></a>Q23 | Opt-in extra-roots for shared cross-worktree files | `security` | M | **Demand:** a session that legitimately needs cross-worktree shared files (mailbox files, the main checkout) and can't tolerate the prompts. Fix: an opt-in, empty-by-default extra-roots env var. |
| <a id="Q42"></a>Q42 | Catch a glob match that is itself a symlink out of the root | `security` | M | **Demand:** a glob-matched in-root name that points outside gets read silently. A glob resolves as the pattern, so `realpath` never sees the match. Closing it needs match enumeration. |
| <a id="Q47"></a>Q47 | Catch a `**` glob item that matches fewer segments than the pattern | `security` | M | **Demand:** a session runs `shopt -s globstar`. Verified: `docs/**` expands to `docs/` too, so a loop body's trailing `../` climbs above the root undetected. Issue 99's proxy needs fixed segments. |
| <a id="Q50"></a>Q50 | Stop an odd quote in an expanded heredoc body hiding a later `$(…)` | `security` `parsing` | S | **Demand:** a real outside read slips through. Verified: `<<EOF` + `don't` + `$(cat /etc/x)` gets `allow` — the apostrophe mis-colors the scan's quote state. Fix: scan a body on its own. |
