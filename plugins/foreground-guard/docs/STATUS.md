# Project Status

Single source of truth for progress and priorities. Pick the next task from
the top of the Queue.

**Status:** 🔲 ready · 🚫 blocked
**Size:**   S = one session/PR · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `bug` `parser` `config` `feature` `tests` `docs` `infra`
**Next ID:** Q14

## Queue

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q11"></a>Q11 | Decide whether `auto` still belongs in `UNATTENDED_MODES` | `feature` | 🔲 | S | Now both classes deny by default, so the set only fires on a config-de-escalated `ask` — where a repo asked to be prompted. `dontAsk`/`bypassPermissions` are clear; `auto` is the question. |
| <a id="Q12"></a>Q12 | `extra_watch_patterns` denies carry a principle, not a rewrite | `config` | 🔲 | S | Built-in watch rows ship a pasteable alternative; a config pattern gets the generic "take one snapshot instead". Let a repo supply one, beside the current list form. |
| <a id="Q13"></a>Q13 | Nothing checks the README decision table against the hook | `tests` | 🔲 | S | The table is the plugin's main doc surface and drifts silently; verifying it means driving each row through the hook by hand. Parse the rows and assert the verdict. |
| <a id="Q10"></a>Q10 | README: refresh the companion-plugin list | `docs` | 🔲 | S | Names three siblings, then says "All four compose". pr-sentinel and exit-status-guard have shipped since, and `--plugin all` already counts both. Six are enumerated in [cross-guard-deny-convention.md](development/cross-guard-deny-convention.md). |

## Deferred

| ID | Item | Labels | Sz | Trigger to revive |
|---|---|---|---|---|
| <a id="Q9"></a>Q9 | Stop-hook: detect stranded backgrounded wait | `feature` | M | **Decision:** foreground-guard grows completion-tracking — the agent backgrounds a wait per the guard's advice, then ends the turn without reading the task result. |
