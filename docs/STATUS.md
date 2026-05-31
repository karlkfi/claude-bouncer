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

Last touched: 2026-05-31

---

## Queue

Specific actionable items in priority order. Pick from the top; skip 🚫 items until their blocker clears.

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q1"></a>Q1 | Add tests for `bash-workspace-guard.py` SPEC/parsing | `tests` | 🔲 | S | Fixture-based unit tests covering each `SPEC` row, `prog_suppressed_by`, `--opt=val` inline values, redirect capture, `realpath` traversal. The SPEC table is load-bearing and currently untested. |
| <a id="Q2"></a>Q2 | Allowlist common safe non-workspace paths | `parsing` | 🔲 | S | `/dev/null`, `/dev/stdin`, `/dev/stdout`, `/dev/fd/N`, `/dev/zero` currently trigger `ask`. Add a small allowlist of well-known device/FD paths that bypass the outside-workspace check. |
| <a id="Q3"></a>Q3 | Reconcile `rg` alias to `grep` | `parsing` `bug` | 🔲 | S | `ripgrep` has a different flag set (`-g`, `-t`, `--type`, `--type-add`, etc.). Treating it as `grep` means unknown flags are zero-arg, so `rg -g '*.py' PAT path` mis-parses `'*.py'` as a positional. Either add a dedicated `rg` SPEC or drop the alias. |
| <a id="Q4"></a>Q4 | Heredoc / here-string false positives | `parsing` | 🔲 | S | `<<` / `<<<` capture the next shlex token as a "file" candidate. Lexical resolution usually lands inside the workspace, but `<<<"/etc/foo"` would falsely flag. Skip the token after `<<`/`<<<` from path checks. |
