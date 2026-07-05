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

Last touched: 2026-07-05

---

## Queue

Specific actionable items in priority order. Pick from the top; skip 🚫 items until their blocker clears.

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q23"></a>Q23 | Opt-in extra-roots for shared cross-worktree files | `security` | 💤 | M | Files legitimately shared across worktrees (coordination/mailbox files, the main checkout above `.claude/worktrees/`, sibling worktrees) are by definition outside `$CLAUDE_PROJECT_DIR`, so every Bash read/write prompts. Deferred: add an opt-in, empty-by-default extra-roots env var (analogous to `additionalDirectories`) — secure-by-default, but real demand is unproven (the one session that hit it abandoned file-mailboxes). |
| <a id="Q26"></a>Q26 | Extend host-temp `deny` to currently-unguarded shapes | `security` `parsing` | 💤 | M | The host-temp deny only fires on file args the hook already extracts; a standalone `cd /tmp`, `mktemp -p /tmp`, and redirects from unguarded commands (`go test > /tmp/log`) still defer. Deferred: would need guarding those shapes (new `SPEC` rows / standalone-cd handling) — adds parsing surface for marginal coverage. |
| <a id="Q27"></a>Q27 | Split glued operator runs (`);`, `((`, `));`) into separate tokens | `parsing` | 🔲 | S | shlex groups adjacent punctuation into one token, so `(cd x); cat $f` and `f=$(mktemp); cat $f` merge groups and the guarded command defers (pre-existing; issue-58 propagation only poisons on these, never sets). Fix: longest-match split of pure-punctuation runs against the known operator vocabulary, generalizing `split_newline_separators`; note it converts some current defers into hook decisions. |
| <a id="Q28"></a>Q28 | Guard commands prefixed by shell keywords (`until grep …`, `if grep …`) | `parsing` | 🔲 | S | A reserved word before a guarded command (`until grep -q PAT $f; do …`) masks the `SPEC` lookup, so the whole group defers. `poison_vars` already skips `SH_KEYWORDS`; do the same before `files_in_command` so the guarded command is classified — more coverage, secure direction. |
