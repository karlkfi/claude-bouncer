# Agent reference: Maintaining the backlog

[`docs/queue/`](../queue/README.md) is the single source of truth for progress
and priorities across all five plugins. Its format and maintenance process are
defined by the globally installed **`session-backlog` skill** — invoke it for
any change to the store rather than following rules copied here; a local copy
would drift.

The load-bearing invariants, for sessions without the skill available:

1. **Never hand-pick an ID.** Claim it, or you will collide with a session you
   cannot see. IDs are stable and never reused or renumbered.
2. **Never hand-type a `rank`.** `scripts/queue.py rank --head` / `--tail` /
   `--after` / `--before` computes one.
3. **Isolate backlog edits in their own commit.** Under a per-item store this
   buys no conflict relief — nothing else edits an item's file — so it stands on
   its second reason: the item is the *why* and the code is the *what*, and a
   reviewer should not have to separate them by hand.
4. **Completing an item deletes its file.** Git is the archive. Name the reason
   with a fixed verb in the commit subject — `complete QN`, `prune QN`,
   `merge QN into QM`, `defer QN` — or `queue.py metrics` cannot tell throughput
   from garbage collection.
5. **Lint every edit**: `make backlog-lint`.

## Repo-local tooling

`scripts/queue.py` is vendored from the skill so the checks work whether or not
it is installed — the same reason `lib/bouncer_parse.py` is vendored into each
plugin, and with the same hazard: fix it upstream in `karlkfi/claude-skills`
first, or the next vendor drop reverts it.

| | |
|---|---|
| `make backlog` | the ordered queue, deferred items included |
| `make backlog ARGS='--label prod-guard'` | one plugin's items |
| `make backlog-next` | the top ready item, as a session prompt |
| `make backlog-lint` | the gate, also run by `make check` and by CI |

`make backlog-lint` promotes three of the linter's advisory classes to errors:
`blocked-opener`, `deferred-trigger`, and `empty-store`. `dangling-link` stays
advisory on purpose — a link across a live batch is legitimately in flight, and
a gate that failed on it would redden the store for the hours after every merge.

The pre-commit gate lives at `.githooks/pre-commit`. It is enabled per-clone:

```bash
git config core.hooksPath .githooks
```

## Every item names its plugin

The plugin label — `workspace-guard`, `branch-guard`, `prod-guard`,
`exit-status-guard`, `foreground-guard`, or `repo` — is what makes one store
readable across five plugins, and it is the only label this repo adds to the
topic vocabularies the plugins already used. An item without one is not
findable by the person who owns that plugin.

## Where the IDs came from

The store was consolidated on 2026-08-22 from three per-plugin `docs/STATUS.md`
tables. Each ran its own `Q` sequence from `Q1`, so the same number meant three
different things — `plugins/prod-guard/tests/test_prod_guard.py` still cites a
`Q11` that is prod-guard's, not the live one.

workspace-guard's seventeen items kept their IDs, because plan-doc filenames and
note text point at them. The six from foreground-guard and prod-guard were
renumbered into `Q86`–`Q91`, above every number any of the three had ever
issued; nothing in the tree referenced them. New items start at `Q92`.

A bare `Q`-ID in a **commit message or PR body predating that date** belongs to
whichever plugin the change touched. Everything since is repo-wide and
unambiguous.
