# Agent reference: Maintaining the backlog

[`docs/queue/`](../queue/README.md) is the single source of truth for progress
and priorities across all five plugins. Its format and maintenance process are
defined by the globally installed **`session-backlog` skill** — invoke it for
any change to the store rather than following rules copied here; a local copy
would drift.

The load-bearing invariants, for sessions without the skill available:

1. **Take the next number above the highest the store has ever held** — and read
   that from git, not from the directory, which understates it by every item
   that has shipped. IDs are stable and never reused or renumbered. This is the
   part of the process that does not survive two sessions filing at once, which
   is what Q92 is for; until it lands, a second concurrent filer collides and
   git reports it as an add/add conflict on one path rather than silently.
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
readable across five plugins. An item without one is not findable by the person
who owns that plugin, and `tests/test_backlog.py` fails a row that has none.

Label the plugin **whose tree the work lands in**, which for almost every item
is also the plugin it was found in. Where the two differ, the second wins: Q94
carried both `prod-guard` and `workspace-guard` because the shipped plan docs it
archived sat in both trees.

`lib` is the second label this repo adds, and the reason the rule above needs
stating at all. An item whose fix lands in `lib/bouncer_parse.py` reaches all
five guards, so it carries `lib` **beside** its plugin label rather than instead
of it — the plugin is still where the measurement and the fixture live. Q81 is
the worked example: it was filed against workspace-guard, and
`_scan_dollar_paren` is shared, so the fix is a five-guard change and the row
says so. Nothing lints this; it is a reading of where the fix goes, made when the
row is filed and re-checked when it is picked.

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
