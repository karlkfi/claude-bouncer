# The backlog

One backlog for all five guards. Each item is a file — `QNNN.md` — holding its
own frontmatter and note, and priority lives in the `rank` key rather than in a
position in a table. Two sessions working different items never touch the same
file, which is the whole reason for the layout.

**A directory listing is not the backlog.** It sorts `Q10` before `Q2`, which is
neither priority nor number. Read the ordered queue instead:

```bash
make backlog
```

That prints every item in priority order, deferred ones included. `make
backlog-next` prints the top ready item as a session prompt.

## What an item looks like

```yaml
---
id: Q76
rank: a5
labels:
    - parsing
    - workspace-guard
status: ready          # ready | blocked | deferred
size: S                # S = one session/PR · M = 2–3 sessions · L = needs a plan doc
---

# Extend entry-operand resolution to the PowerShell tool

The note, as prose. No length cap — the item has a page of its own. The title
is capped at 72 characters, because it has nowhere to overflow to.
```

An `L`-sized item adds `target:` naming the plan doc it works from.

**Every item carries the plugin it belongs to as a label**: `workspace-guard`,
`branch-guard`, `prod-guard`, `exit-status-guard`, `foreground-guard`, or `repo`
for work on the repository itself. It is the one label this repo adds beyond the
topic vocabulary each plugin already used, and it is what makes a single store
readable across five plugins:

```bash
make backlog ARGS='--label workspace-guard'
```

**An item whose fix lands in `lib/bouncer_parse.py` also carries `lib`.** The
shared parser reaches all five guards, so that label is what separates a change
one guard has to re-run from one every guard does. It is added beside the plugin
label, not instead of it — the plugin is still where the measurement and the
fixture live.

```bash
make backlog ARGS='--label lib'
```

A link out of an item is written relative to this directory, so a plugin's own
docs are reached as `../../plugins/<name>/docs/…`.

## Filing, picking, completing

The process is the globally installed **`session-backlog` skill** — invoke it
rather than working from a copy of its rules. This repo's wiring, and the
invariants that still hold when the skill is not available, are in
[`../development/maintaining-backlog.md`](../development/maintaining-backlog.md).

The short version: take the next unused ID, compute a rank with
`scripts/queue.py rank`, write the file, and lint. Completing an item deletes
its file — git is the archive.

## What the next release admits, and what it is for

These are two questions and only one of them lives in a file.

**The ceiling is computed, never written down.** Each plugin carries its own
SemVer line, so "the next release" is five of them, and
`python3 scripts/release.py status` reports each plugin's current version, its
verdict, and the commits since its last tag. Read it rather than recording the
answer here — a written list of pending cuts is stale the moment one is tagged,
and this one is not. Where every verdict is a patch, no `feature` row is in
scope for any of them until somebody opens a minor.

**The emphasis is not recorded, deliberately.** What a release is *for* within
that ceiling — a parity push, the cleanup tail, one plugin's turn — is a
maintainer's call, and nothing here should assert one. A coordinating session
dispatching a batch asks it and holds the answer for that run only; see
`session-orchestrator`, which reads a scope record and is forbidden to write
one, because a run ends and so can never retire what it wrote.

So a batch selected against this store gets its ceiling from `release.py status`
and its emphasis from whoever is running the batch.
