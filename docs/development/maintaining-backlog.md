# Agent reference: Maintaining the backlog

`docs/STATUS.md` is the single source of truth for project progress and priorities. It is high-contention — almost every session edits it — so keeping churn low matters as much as keeping it accurate. This doc captures the rules that keep merge conflicts trivial and the file readable.

## The non-negotiables

1. **Isolate `docs/STATUS.md` edits in their own commit**, separate from code and plan-doc changes. Rebase conflicts on STATUS.md should always be resolvable by `git checkout --theirs` or `--ours` on a single file. This is the highest-leverage rule in this doc.
2. **Run `gh pr list` before picking a task.** A Queue row already covered by an open PR should be skipped, not re-started. The open PR — not a `▶ Started` marker — is the real "in-flight" signal.
3. **Verify 🚫 blockers are still real before treating an item as blocked.** A previous session may have silently completed the dependency without flipping the row. Grep for the deliverables (test names, env vars, code paths) before skipping.

## Format rules that exist to reduce churn

### `Last touched:` is one line, date only

The header line under "Conventions" is for the date of the most recent edit and nothing else:

```
Last touched: 2026-05-31
```

Do **not** append a session narrative, do **not** preserve prior entries with "Earlier: …", do **not** describe what changed. That information lives in:

- the commit message (for the most recent change),
- `git log docs/STATUS.md` (for the full history),
- the linked plan doc under `docs/plan/` (for design context, when one exists).

A multi-paragraph `Last touched:` block is the single largest source of merge conflicts in this file. Every concurrent branch edits it. Resist.

### Queue `Notes` column: ≤2 sentences

`Notes` answers two questions only:

- **What is this item, in one sentence?** (often just a pointer: "→ kind end-to-end" for blocked items.)
- **What unblocks it or what's the next concrete step?**

Anything longer — root-cause analyses, design rationale, dry-run write-ups — belongs in a linked plan doc under `docs/plan/`, not the row.

If a row's Notes is growing past two sentences, that's the signal to move the content to `docs/plan/<plan>.md` and replace the row Notes with a link.

### Don't use `▶ Started` markers for solo work

- The open PR (visible to `gh pr list`) is the started signal, and the `gh pr list` check before picking already prevents double-starting.
- Marking ▶ Started adds one wasted isolated commit per task (one to mark started, one to delete the row on completion).
- The marker rots if a session is abandoned, requiring cleanup churn later.

Only set `▶ Started` if you have a specific reason to broadcast in-progress state beyond the open PR (e.g. an exploratory task with no PR yet, an item you've reserved but won't start for several days). Default to not setting it.

### Use `Blocked by [QN](#QN)` for cross-item blockers

When a 🚫 Queue row is blocked by another Queue item, start its Notes with `Blocked by [QN](#QN)` (or comma-separated for multiple). External dependencies that have no Queue ID — "needs a Python 3.12 test matrix", a third-party sign-off — stay as plain prose.

The structured form is machine-readable: when the dependency lands, `grep "Blocked by \[Q12\]" docs/STATUS.md` enumerates dependents to clear in a single isolated commit. Free-text "→ later" notes are not.

### Stable IDs; do not reuse

Each Queue row has a `Q`-prefixed ID (e.g. `Q4`). Once assigned, it stays — even if the row is deleted. New rows take the next unused integer (continuing the same sequence). This makes cross-references in commit messages and PR descriptions stable.

The `Q` prefix exists so that references like `Q4` in a commit message or PR body are **not** auto-linked by GitHub to PR/issue 4 — `#NN` would be, `Q<N>` is not. Use the bare ID (`Q4`) in commits, PRs, and prose. Inside `docs/STATUS.md`, each row carries an inline anchor (`<a id="Q4"></a>Q4`), so cross-references between rows render as Markdown links: `[Q4](#Q4)`.

**Do not introduce sub-IDs (`4a`, `4b`)** to track derivative work under a parent item. If a child task is discrete enough to track, give it its own top-level ID.

### Batch audit-discovery items in one commit

When a single review pass surfaces many new items, add them all in one commit. One commit moving a contiguous block of rows is far easier to rebase than N commits each inserting one row.

The same applies to bulk completions: if a session verifies that a stale Queue entry was actually finished weeks ago, fold the deletion into the same commit as the verification work rather than splitting.

## Plan docs

For M/L items, create a plan doc under `docs/plan/<slug>.md` capturing the design intent, scope, and acceptance criteria. The Queue row links to it; the plan doc holds the detail.

When a plan's work fully lands and `docs/STATUS.md` no longer references it, move it under `docs/plan/archive/` rather than deleting — the rationale is usually more valuable than the diff. `git mv` preserves history. Do not edit STATUS.md in the same commit as the archive move (see non-negotiable §1).

A partially-complete plan stays in `docs/plan/`. Archive is for "everything in this doc has shipped," not "most of it has."

## Anti-patterns to watch for

- **Narrating recent session work in the conventions header.** That's what commit messages are for.
- **Carrying root-cause writeups in Queue Notes.** That's what plan docs are for.
- **Splitting bulk discovery into many one-row commits.** That maximizes rebase pain.
- **Renumbering existing IDs to "tidy up".** IDs are pointers; renumbering invalidates every external reference.
- **Editing STATUS.md alongside a code change.** Conflicts on the code commit cascade into the STATUS.md edit. Always a separate commit.
