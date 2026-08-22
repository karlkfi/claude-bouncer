# Agent reference: Skills this repo names

Parts of this repo's process are defined by skills installed globally, outside any repo. This
page gives each one a local anchor, so a doc can link an explainer instead of a private URL most
readers cannot open.

It is an index, not a copy. A skill is written to work in any repo, and this repo's own rules
already live in the page that invokes it. Each entry says what the skill is for, where it fires
here, and which page holds the local rules.

## Why not link the source

Where a skill lives decides whether a doc can link it:

| Source | Linkable from `docs/`? |
|---|---|
| Globally-installed (`~/.claude/skills/`) | No — outside every repo, and private |
| Plugin (`~/.claude/plugins/**/skills/`) | No — same reason |
| Repo-local (`skills/`) | Yes — in-tree, so a relative link resolves |

This repo ships one skill of its own, `reduce-workspace-guard-prompts`, under `skills/`. That one
is in-tree and needs no entry here, so link it directly.

## Skills

### session-backlog

Maintains the repository-root backlog, `docs/queue/` — one file per item, with priority in each
item's `rank` key. Invoke it for any change to the store: adding an item, picking the next task,
marking one done, deferring one, or a full grooming pass.

It fires on every backlog edit. The repo's rules, including the invariants that still hold when
the skill is unavailable, are in [`maintaining-backlog.md`](../../../../docs/development/maintaining-backlog.md) at the
repository root, which also vendors the skill's `queue.py` — so a fix to it belongs upstream as well.

## Names drift, and nothing here goes red

This skill was called `backlog` until upstream renamed it, and no gate in this repo noticed.
None of them read the skill set, and a session told to invoke a skill that is not installed gets
no error — the name simply resolves to nothing and the process goes unfollowed.

The tell is a name in these entries with no matching directory under `~/.claude/skills/`. Check
for it when a documented process appears to have been skipped for no visible reason.
