# Working in claude-bouncer

Five Claude Code guard plugins in one marketplace. Each plugin under
`plugins/<name>/` is self-contained and has its own `CLAUDE.md` covering its
rules. Read that one for plugin work. This file covers what is true of the repo
as a whole. Releasing is one of those things: it moves the root manifest and
the root README, so the runbook is `docs/development/release-process.md` and
there is no per-plugin one. The backlog is another — one store for all five,
below.

## Never edit a vendored parser copy

`lib/bouncer_parse.py` is the shared shell parser. Every plugin carries a copy
at `plugins/<name>/lib/bouncer_parse.py`, written by `scripts/sync-lib.py`.

Edit the root copy, then run `make sync`. A vendored copy is overwritten
without warning, so an edit made there is lost at the next sync, and until then
the guards disagree about what a command means. `make sync-check` is a CI gate
and runs before the tests: a plugin suite passing against a stale copy is the
failure it exists to catch.

The copies exist because Claude Code copies a plugin into
`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` at install time and
a path climbing out of the plugin root does not resolve there. Symlinks to a
marketplace sibling are dereferenced for a git-hosted install but skipped for
`--plugin-dir` and local-path installs, which is how this repo gets tested, so
vendoring is the only form that behaves the same everywhere.

## What belongs in the shared parser

A primitive belongs in `lib/` when the guards must agree on it. Tokenizing a
command is one. Segmentation is not: foreground-guard needs to know which
segment was backgrounded, exit-status-guard needs the operator that joined two
commands, and prod-guard judges an unterminated heredoc that bash treats as
data. Forcing one answer onto another guard's question is how five copies of
one lexer drifted apart in the first place.

Add to the shared surface only when a second guard needs the same behaviour.
Extend additively when a guard needs more: `strip_heredoc_bodies` grew an
`unterminated` out-parameter that way, and the guards that do not pass one are
unaffected.

## One backlog, five plugins

All work is tracked in [`docs/queue/`](docs/queue/README.md) — one file per
item, priority in each item's `rank` key. A plugin has no backlog of its own.

Two rules the store cannot enforce on its own:

- **Every item carries a label for the plugin whose tree the work lands in** — a
  guard's name, or `repo` for work on the repository.
  `make backlog ARGS='--label prod-guard'` is how the owner of one plugin finds
  their work in a store of five. An item whose fix lands in `lib/` also carries
  `lib`, beside the plugin label rather than instead of it, because that change
  reaches all five guards and the row is the only place that is visible before
  the diff exists.
- **Never hand-pick an ID or hand-type a `rank`.** Both are computed; see
  [`docs/development/maintaining-backlog.md`](docs/development/maintaining-backlog.md),
  which also records why the workspace-guard IDs kept their numbers and the
  others did not.

Invoke the `session-backlog` skill for any change to the store.

## Checks

```
make check    # drift, version, path filters, backlog lint, parser tests, doc links, all five suites
```

Run it before proposing a change is done. A change under `lib/` reaches all
five guards, so say which ones you re-ran.

CI does not run every job on every pull request. `.github/workflows/tests.yml`
classifies the diff in a `changes` job and each plugin's jobs skip when nothing
under `plugins/<name>/` -- or under `lib/` -- changed. **A skipped job is a
path-skip, not a pass**, and the checks list cannot tell you which, so read the
filters before reading a green run as coverage. `make path-filter-check` is the
recurrence guard: it fails when a plugin has no filter, when a filter omits its
own directory or the shared anchor, or when a job is gated on the wrong plugin.
Everything runs unfiltered on push to `main`, which is what makes "tag a green
one" in the release process mean what it says.

Python 3.9 is the floor. exit-status-guard supports it and CI runs the shared
parser against it, so 3.10+ syntax in `lib/` breaks that job and nothing else,
which makes it easy to miss locally.

## Releases

One plugin at a time. Each guard keeps its own version line, the tag names it
(`workspace-guard/v1.10.1`, never a bare `vX.Y.Z`), and the version string
lives in three places, gated by `make version-check`: the plugin's `plugin.json`, its entry
in the root `marketplace.json`, and the version column of the root `README.md`.
A `lib/` change reaches five guards and releases none of them.

`/release` drives a cut end to end, over `scripts/release.py` (`status`,
`bump`, `tag`). The steps, the version levels, and the notes harvest are in
`docs/development/release-process.md`.

## Links to the retired repositories

The plugins came from five separate repos, and 279 links still point at them:
issue and pull-request permalinks, tag-pinned `blob` and `compare` paths, and
Actions runs. Those resolve where they are and must stay. Only live
instructions moved: `/plugin marketplace add`, `/plugin install`, and the
"report it at" URL a guard prints in a denial.

Dated notes under `plugins/*/docs/releases/` keep the old instructions. They
record what shipped at that version.
