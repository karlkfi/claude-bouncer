# Project Status

Single source of truth for progress and priorities in workspace-guard. Pick the next task from the top of the Queue.

**Status:** 🔲 ready · 🚫 blocked
**Size:** S = one session/PR · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `security` `tests` `docs` `infra` `bug` `parsing`
**Next ID:** Q31

**Maintaining this file:** see [`docs/development/maintaining-backlog.md`](development/maintaining-backlog.md).

## Queue

Specific actionable items in priority order. Pick from the top; skip 🚫 items until their blocker clears.

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q27"></a>Q27 | Split glued operator runs (`);`, `((`, `));`) into separate tokens | `parsing` | 🔲 | S | shlex glues adjacent punctuation into one token, so `(cd x); cat $f` merges groups and the guarded command defers. Fix: longest-match split of punctuation runs against the operator vocabulary. |
| <a id="Q28"></a>Q28 | Guard commands prefixed by shell keywords (`until grep …`, `if grep …`) | `parsing` | 🔲 | S | A reserved word before a guarded command (`until grep …`) masks the `SPEC` lookup, so the whole group defers. `poison_vars` already skips `SH_KEYWORDS`; do the same before `files_in_command`. |

## Deferred

| ID | Item | Labels | Sz | Trigger to revive |
|---|---|---|---|---|
| <a id="Q23"></a>Q23 | Opt-in extra-roots for shared cross-worktree files | `security` | M | **Demand:** a session that legitimately needs cross-worktree shared files (mailbox files, the main checkout) and can't tolerate the prompts. Fix: an opt-in, empty-by-default extra-roots env var. |
| <a id="Q26"></a>Q26 | Extend host-temp `deny` to currently-unguarded shapes | `security` `parsing` | M | **Event:** a real session leaks host-temp writes through an unguarded shape (`cd /tmp`, `mktemp -p /tmp`, unguarded-command redirects). Needs new `SPEC` rows and standalone-`cd` handling. |
| <a id="Q30"></a>Q30 | friction-report: flag stale installed version | `infra` `docs` | S | **Demand:** friction a newer release already fixes ([#71](https://github.com/karlkfi/claude-workspace-guard/issues/71)). Compare installed version vs the marketplace clone's `plugin.json`; print "installed X, Y available" by the top friction row. |
