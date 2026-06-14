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
| <a id="Q19"></a>Q19 | Expand `~`/`~/…` instead of flagging it as unresolvable | `parsing` `security` | 🔲 | S | Bare `~`/`~/…` resolve deterministically to `$HOME`, but the hook flags them as runtime-expanded → `ask` even when they land in-workspace (verified: `cat ~/…/<this-worktree>/README.md`, and `cd ~/…/<worktree> && grep` cascades untracked-cd prompts). Expand `~`/`~/` to `$HOME` in `resolve_token` + `classify_cd` (keep `~user`/`~+`/`~-`/unset-`$HOME` deferring); outside paths still resolve outside, so no security loss. |
| <a id="Q20"></a>Q20 | fd-prefixed redirects leak a phantom file token | `parsing` `bug` | 🔲 | S | `2>/dev/null`, `2>&1`, `>out 2>&1` leak the fd digit (and the `>&` dup form) as positional file tokens — verified `cd … && grep foo test/run.sh 2>/dev/null` reports `2` as an offender; harmless in-root but spurious after a cd-shift. Drop an integer token immediately preceding a REDIR operator and skip fd-duplication targets — pure false-positive reduction, never removes a real file check. |
| <a id="Q21"></a>Q21 | Investigate dominant `/tmp/claude-*` prompt source | `parsing` `security` | 🔲 | S | One path — `/private/tmp/claude-501/-Users-karl-workspace…` — was ~86 of ~120 workspace-guard `ask`s in a week of real usage (Claude's own per-workspace temp/scratch dir). Investigate what targets it and whether allowlisting Claude's managed temp root is secure-by-default; this is a security decision, not a blind allow. |
| <a id="Q22"></a>Q22 | Promote vendored-source-over-global-cache guidance to README | `docs` | 🔲 | S | Reading dependency source from an out-of-tree global cache (Go `~/go/pkg/mod`, npm/pip/cargo caches) prompts on every guarded read; the fix ("prefer in-workspace vendored/pinned source") currently lives only in consuming repos' CLAUDE.md. Promote the generalized guidance into the shipped Agent-guidance block in `README.md` — doc-only, no behavior change. |
| <a id="Q24"></a>Q24 | Wire friction-report into the prompt-reduction skill | `docs` | 🔲 | S | The `reduce-workspace-guard-prompts` skill is blind to historical prompts (session-search doesn't index the hook's reason text), so it guesses on past friction. Wire it to the friction-report analyzer (`scripts/friction-report.py`) so it diagnoses from real data, and use that report to drive the [Q21](#Q21) investigation. |
| <a id="Q15"></a>Q15 | Heredoc body content tokenizes as positional args | `parsing` | 💤 | M | Discovered during [Q4](#Q4): `cat <<EOF\n/etc/passwd\nEOF` flags `/etc/passwd` because stdlib `shlex` parses the body as positional tokens — bash slurps until the delimiter, we can't. Deferred: needs a real bash parser or heredoc-delimiter-aware splitter; Claude rarely emits multi-line heredocs to guarded commands. |
| <a id="Q16"></a>Q16 | Redirect targets don't track cd-shifts | `parsing` `security` | 💤 | M | Discovered during [Q7](#Q7): redirects (`> file`) are collected at the top level, not associated with their group, so a relative redirect target is always resolved against the original cwd. `cd /tmp && cat /dev/null > evil` would let `evil` resolve inside the workspace cwd even though bash writes `/tmp/evil`. Documented as a Limitation in README. Deferred: needs per-group redirect association — bigger refactor than the cd-tracking itself; narrow real-world impact (attacker needs an allowlisted read source). |
| <a id="Q23"></a>Q23 | Opt-in extra-roots for shared cross-worktree files | `security` | 💤 | M | Files legitimately shared across worktrees (coordination/mailbox files, the main checkout above `.claude/worktrees/`, sibling worktrees) are by definition outside `$CLAUDE_PROJECT_DIR`, so every Bash read/write prompts. Deferred: add an opt-in, empty-by-default extra-roots env var (analogous to `additionalDirectories`) — secure-by-default, but real demand is unproven (the one session that hit it abandoned file-mailboxes). |
