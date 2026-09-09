# Agent reference: Maintaining the backlog

[`docs/queue/`](../queue/README.md) is the single source of truth for progress
and priorities across all five plugins. Its format and maintenance process are
defined by the globally installed **`session-backlog` skill** — invoke it for
any change to the store rather than following rules copied here; a local copy
would drift.

The load-bearing invariants, for sessions without the skill available:

1. **Claim the ID on the remote. Never read a number and add one.**

   ```bash
   make backlog-claim ARGS='The row title'
   ```

   Creating `refs/queue-ids/QN` is a compare-and-swap, so two sessions asking
   at the same instant get different numbers with no lock and nothing to
   release. IDs are stable and never reused or renumbered. `make
   backlog-claims` is the other half and runs in `make check` and in CI, so an
   ID a branch adds that holds no claim fails there rather than at the rebase
   it collides with.

   **Incrementing the highest number is the fallback, for a remote that
   refuses a custom ref namespace** — which GitHub does not. It is what this
   invariant used to prescribe, and it collides: claims are not fetched by the
   default refspec, so they are invisible to a clone. Measured 2026-09-09,
   before that day's own rows landed: the highest ID ever added on
   `origin/main` was Q181, and Q182 through Q193 were already claimed. Both
   figures moved again within the same day, which is the argument rather than a
   caveat on it — the number a clone reads is a snapshot, and what it cannot
   see is what makes incrementing it collide.
   `git ls-remote origin 'refs/queue-ids/*'` is how you see them.
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

`scripts/queue.py` and `scripts/alloc-queue-id.sh` are vendored from the skill
so the checks work whether or not it is installed — the same reason
`lib/bouncer_parse.py` is vendored into each plugin, and with the same hazard:
fix them upstream in `karlkfi/claude-skills` first, or the next vendor drop
reverts the change.

The allocator is vendored because the gate creates the need: `backlog-claims`
tells a failing session to allocate an ID with `alloc-queue-id.sh`, so a clone
with nothing installed has to be able to run it.

**Drift in the vendored parser is guarded here; drift in these two is not.**
`make sync-check` catches a drifted `lib/bouncer_parse.py` because both sides
are in this tree. The skill's copies are not, and are absent from a CI
checkout, so no target here can compare against them. The watch that exists
runs from the other side:
`make vendor-check` in `karlkfi/claude-skills` hashes this repository's copy
through the GitHub API and names the commit it was taken at. It reports rather
than fails, deliberately — a stale copy is not a defect in whatever pull
request is under review there — and it runs in that checkout, never in this one
and never in this repository's CI. So a refresh lands when somebody runs it and
reads the report, not when the copy drifts.

| | |
|---|---|
| `make backlog` | the ordered queue, deferred items included |
| `make backlog ARGS='--label prod-guard'` | one plugin's items |
| `make backlog-next` | the top ready item, as a session prompt |
| `make backlog-claim ARGS='The row title'` | claim an ID — one title, one ID |
| `make backlog-lint` | the store gate, also run by `make check` |
| `make backlog-claims` | the claim gate, run by `make check` and by CI |

`ARGS` is quoted for `backlog-claim` and not for the others, because a title is
free text where the rest take flags. Give it one title per invocation.

`make backlog-lint` promotes three of the linter's advisory classes to errors:
`blocked-opener`, `deferred-trigger`, and `empty-store`. `dangling-link` stays
advisory on purpose — a link across a live batch is legitimately in flight, and
a gate that failed on it would redden the store for the hours after every merge.
No CI step runs this target; what covers it there is `tests/test_backlog.py`,
which restates those three flags in a constant of its own. Q183 is that gap.

`make backlog-claims` passes `--strict` when `$CI` is set, so a remote it
cannot read is a skip in an offline clone and a failure in CI. Its job in
`tests.yml` checks out with `fetch-depth: 0`: the check needs a merge base with
`origin/main`, and the default shallow checkout fetches neither.

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
