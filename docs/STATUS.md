# Project Status

Single source of truth for progress and priorities in workspace-guard. Pick the next task from the top of the Queue.

**Status:** 🔲 ready · 🚫 blocked
**Size:** S = one session/PR · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `security` `tests` `docs` `infra` `bug` `parsing`
**Next ID:** Q35

**Maintaining this file:** see [`docs/development/maintaining-backlog.md`](development/maintaining-backlog.md).

## Queue

Specific actionable items in priority order. Pick from the top; skip 🚫 items until their blocker clears.

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q32"></a>Q32 | Decode `mktemp` combined short flags (`-dp DIR`) | `parsing` | 🔲 | S | `classify_mktemp` treats `-dp` as one unknown flag, so `-p`'s DIR leaks and `mktemp -dp ./scratch x` false-denies. Decode short-flag clusters (`-p`/BSD `-t` take a value). Safe today. From Q26. |
| <a id="Q33"></a>Q33 | Guard commands inside quoted `"$(…)"` / backtick substitution bodies | `security` `parsing` | 🔲 | L | Quoted `"$(mktemp)"`/backtick bodies aren't parsed, so a host-temp write created inside them isn't flagged (unquoted `$(…)` already is). Low severity. Needs a plan doc. From Q26. |
| <a id="Q34"></a>Q34 | Honor inline `TMPDIR=<literal> mktemp` in default-location resolution | `parsing` | 🔲 | S | `classify_mktemp` ignores a command-prefix `TMPDIR=`, so `TMPDIR=./scratch mktemp` false-denies. Capture it and feed `default_temp_dir`. Marginal; other-tool case unguardable. From Q26. |

## Deferred

| ID | Item | Labels | Sz | Trigger to revive |
|---|---|---|---|---|
| <a id="Q23"></a>Q23 | Opt-in extra-roots for shared cross-worktree files | `security` | M | **Demand:** a session that legitimately needs cross-worktree shared files (mailbox files, the main checkout) and can't tolerate the prompts. Fix: an opt-in, empty-by-default extra-roots env var. |
