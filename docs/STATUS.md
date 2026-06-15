# Project Status

Single source of truth for progress and priorities in workspace-guard. Pick the next task from the top of the Queue.

## Conventions

**Status:** ✅ done · ▶ started · 🔲 ready · 🚫 blocked · 💤 deferred
**Size:** S = one session · M = 2–3 sessions · L = needs a plan doc under `docs/plan/`
**Labels:** `security` `tests` `docs` `infra` `bug` `parsing`

**Maintaining this file:** see [`docs/development/maintaining-backlog.md`](development/maintaining-backlog.md) for the full rules. Short version:
- **Starting an S item:** complete it, delete the row.
- **Starting an M/L item:** create or update a plan doc under `docs/plan/`; delete the row here when done. (Skip the `▶ Started` marker unless you have a specific reason — the open PR is the in-flight signal.)
- **New item identified:** append it to the Queue with the next unused ID. Batch audit-discovery items in one commit.
- **`Last touched:` is one line, date only.** Do not append session narrative.

Last touched: 2026-06-14

---

## Queue

Specific actionable items in priority order. Pick from the top; skip 🚫 items until their blocker clears.

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q15"></a>Q15 | Heredoc body content tokenizes as positional args | `parsing` | 💤 | M | Discovered during [Q4](#Q4): `cat <<EOF\n/etc/passwd\nEOF` flags `/etc/passwd` because stdlib `shlex` parses the body as positional tokens — bash slurps until the delimiter, we can't. Deferred: needs a real bash parser or heredoc-delimiter-aware splitter; Claude rarely emits multi-line heredocs to guarded commands. |
| <a id="Q23"></a>Q23 | Opt-in extra-roots for shared cross-worktree files | `security` | 💤 | M | Files legitimately shared across worktrees (coordination/mailbox files, the main checkout above `.claude/worktrees/`, sibling worktrees) are by definition outside `$CLAUDE_PROJECT_DIR`, so every Bash read/write prompts. Deferred: add an opt-in, empty-by-default extra-roots env var (analogous to `additionalDirectories`) — secure-by-default, but real demand is unproven (the one session that hit it abandoned file-mailboxes). |
