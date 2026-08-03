# Project Status

Single source of truth for progress and priorities in workspace-guard. Pick the next task from the top of the Queue.

**Status:** 🔲 ready · 🚫 blocked
**Size:** S = one session/PR · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `security` `tests` `docs` `infra` `bug` `parsing`
**Next ID:** Q44

**Maintaining this file:** see [`docs/development/maintaining-backlog.md`](development/maintaining-backlog.md).

## Queue

Specific actionable items in priority order. Pick from the top; skip 🚫 items until their blocker clears.

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q41"></a>Q41 | Bind a `for` list glob built from an outer loop variable | `parsing` | 🔲 | S | `for d in docs/*; do for f in "$d"/*.md` — the inner item holds a `$` and poisons. Expand `loopmap` over the list before binding; the cross product is sound by issue 99's proxy argument. |
| <a id="Q43"></a>Q43 | Expand a leading `~` from the resolved home, not just `$HOME` | `parsing` `bug` | 🔲 | S | Sibling of Q40. `expand_tilde()` reads only `$HOME`, unset under cmd.exe, so `cat ~/x` keeps its `~` and the hook defers while bash expands it. Fix: reuse Q40's `expanduser`; strictly tightens. |
| <a id="Q39"></a>Q39 | Make the remaining Windows test failures pass | `tests` `bug` | 🔲 | M | 78 failures left after the `os.getuid()` fix, one distinct root cause: fixtures assume POSIX path semantics. `/outside/x` is drive-relative on Windows so it resolves *inside* the workspace's drive, `:` is not the PATH separator (`_split_pathlist` splits `/a:/b`), and a few assertions compare POSIX-shaped strings. Needs a Windows box to verify; decide per case whether the fixture or the parser is wrong — some are real parser bugs, not just fixture noise. |

## Deferred

| ID | Item | Labels | Sz | Trigger to revive |
|---|---|---|---|---|
| <a id="Q23"></a>Q23 | Opt-in extra-roots for shared cross-worktree files | `security` | M | **Demand:** a session that legitimately needs cross-worktree shared files (mailbox files, the main checkout) and can't tolerate the prompts. Fix: an opt-in, empty-by-default extra-roots env var. |
| <a id="Q42"></a>Q42 | Catch a glob match that is itself a symlink out of the root | `security` | M | **Demand:** a glob-matched in-root name that points outside gets read silently. A glob resolves as the pattern, so `realpath` never sees the match. Closing it needs match enumeration. |
| <a id="Q35"></a>Q35 | Don't scan `$(…)` inside a quoted-delimiter heredoc body | `parsing` | S | **Demand:** spurious `ask` on a `cat <<'EOF'` body with a literal `$(…)`. `command_substitutions()` scans it though a quoted delimiter stops bash expanding it. Fix: skip quoted-delimiter bodies. |
