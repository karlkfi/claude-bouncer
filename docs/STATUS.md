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

Last touched: 2026-06-01

---

## Queue

Specific actionable items in priority order. Pick from the top; skip 🚫 items until their blocker clears.

| ID | Item | Labels | St | Sz | Notes |
|---|---|---|---|---|---|
| <a id="Q12"></a>Q12 | Add release/version badge to README once a tag exists | `docs` | 🔲 | S | Cut a `v1.0.0` git tag / GitHub release matching `plugin.json`, then add `https://img.shields.io/github/v/release/karlkfi/claude-workspace-guard` alongside the existing license badge. Deferred from the SEO badge audit — a version badge with no underlying release is cosmetic. |
| <a id="Q15"></a>Q15 | Heredoc body content tokenizes as positional args | `parsing` | 💤 | M | Discovered during [Q4](#Q4): `cat <<EOF\n/etc/passwd\nEOF` flags `/etc/passwd` because stdlib `shlex` parses the body as positional tokens — bash slurps until the delimiter, we can't. Deferred: needs a real bash parser or heredoc-delimiter-aware splitter; Claude rarely emits multi-line heredocs to guarded commands. |
| <a id="Q16"></a>Q16 | Redirect targets don't track cd-shifts | `parsing` `security` | 💤 | M | Discovered during [Q7](#Q7): redirects (`> file`) are collected at the top level, not associated with their group, so a relative redirect target is always resolved against the original cwd. `cd /tmp && cat /dev/null > evil` would let `evil` resolve inside the workspace cwd even though bash writes `/tmp/evil`. Documented as a Limitation in README. Deferred: needs per-group redirect association — bigger refactor than the cd-tracking itself; narrow real-world impact (attacker needs an allowlisted read source). |
