# Project Status

Single source of truth for progress and priorities. Pick the next task from
the top of the Queue.

**Status:** 🔲 ready · 🚫 blocked
**Size:**   S = one session/PR · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `bug` `parser` `config` `feature` `tests` `docs` `infra`
**Next ID:** Q11

## Queue

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q10"></a>Q10 | README: refresh the companion-plugin list | `docs` | 🔲 | S | Names three siblings, then says "All four compose". pr-sentinel and exit-status-guard have shipped since, and `--plugin all` already counts both. Six are enumerated in [cross-guard-deny-convention.md](development/cross-guard-deny-convention.md). |

## Deferred

| ID | Item | Labels | Sz | Trigger to revive |
|---|---|---|---|---|
| <a id="Q9"></a>Q9 | Stop-hook: detect stranded backgrounded wait | `feature` | M | **Decision:** foreground-guard grows completion-tracking — the agent backgrounds a wait per the guard's advice, then ends the turn without reading the task result. |
