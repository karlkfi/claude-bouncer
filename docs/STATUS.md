# Project Status

Single source of truth for progress and priorities in workspace-guard. Pick the next task from the top of the Queue.

**Status:** 🔲 ready · 🚫 blocked
**Size:** S = one session/PR · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `security` `tests` `docs` `infra` `bug` `parsing`
**Next ID:** Q37

**Maintaining this file:** see [`docs/development/maintaining-backlog.md`](development/maintaining-backlog.md).

## Queue

Specific actionable items in priority order. Pick from the top; skip 🚫 items until their blocker clears.

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q36"></a>Q36 | Treat in-place mutation flags as writes in read-exempt dirs | `security` `bug` | 🔲 | S | `sed -i` / `awk -i inplace` are not in WRITE_COMMANDS, so the `~/.claude/projects/` read exemption silently allows them to mutate project memory. Make in-place flags disqualify the exemption. |

## Deferred

| ID | Item | Labels | Sz | Trigger to revive |
|---|---|---|---|---|
| <a id="Q23"></a>Q23 | Opt-in extra-roots for shared cross-worktree files | `security` | M | **Demand:** a session that legitimately needs cross-worktree shared files (mailbox files, the main checkout) and can't tolerate the prompts. Fix: an opt-in, empty-by-default extra-roots env var. |
| <a id="Q35"></a>Q35 | Don't scan `$(…)` inside a quoted-delimiter heredoc body | `parsing` | S | **Demand:** spurious `ask` on a `cat <<'EOF'` body with a literal `$(…)`. `command_substitutions()` scans it though a quoted delimiter stops bash expanding it. Fix: skip quoted-delimiter bodies. |
