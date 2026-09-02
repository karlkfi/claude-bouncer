# Working in claude-bouncer

Five Claude Code guard plugins in one marketplace. Each plugin under
`plugins/<name>/` is self-contained and has its own `CLAUDE.md` covering its
rules. Read that one for plugin work. This file covers what is true of the repo
as a whole. Releasing is one of those things: it moves the root manifest and
the root README, so the runbook is `docs/development/release-process.md` and
there is no per-plugin one. The backlog is another — one store for all five,
below.

## Never edit a vendored library copy

`lib/` holds the shared modules: `bouncer_parse.py`, the shell parser, and
`bouncer_grants.py`, the session-grant store. Every plugin carries a copy of
each at `plugins/<name>/lib/`, written by `scripts/sync-lib.py`, which syncs
every module named in its `MODULES` tuple. Adding a module there is what makes
it shared — a file dropped in `lib/` and not named is vendored nowhere.

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

`bouncer_grants.py` is the worked example of the split. Three guards now keep
session-scoped grants and they agree on the *mechanics* — a session-keyed file,
a first-grant timestamp that never slides, an atomic replace, every error
failing toward more prompts — and on nothing else. What a grant *means* stays in
each guard: prod-guard grants an exact target string, workspace-guard a decision
shape, and the worktree grant a path prefix. Migrating those semantics into
`lib/` would produce an abstraction wrong for all three.

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
make check    # drift, version, path filters, action pins, install refs,
              # backlog lint, parser tests, all five suites
```

Run it before proposing a change is done. A change under `lib/` reaches all
five guards, so say which ones you re-ran.

**The pull request body is gated separately, and `make check` cannot see it.**
`.github/workflows/release-note.yml` fails a body with no answered
`## Release note` block -- for every author but Dependabot, which writes its
own body and has no option to template one. No local target reaches a surface
that is not in the tree, so a green `make check` and a red pull request are
consistent here -- and `gh pr create --body-file` bypasses the template that
would have supplied the block, which is how an agent writes a body of any
length. Check it before you open:

```
python3 scripts/release-note.py < body.md
```

It exits 1 on `!! NO SECTION` and `!! UNANSWERED`, and echoes the note on 0.

CI does not run every job on every pull request. `.github/workflows/tests.yml`
classifies the diff in a `changes` job and each plugin's jobs skip when nothing
under `plugins/<name>/` -- or under `lib/` -- changed. **A skipped job is a
path-skip, not a pass**, and the checks list cannot tell you which, so read the
filters before reading a green run as coverage. `make path-filter-check` is the
recurrence guard: it fails when a plugin has no filter, when a filter omits its
own directory or the shared anchor, or when a job is gated on the wrong plugin.
Everything runs unfiltered on push to `main`, which is what makes "tag a green
one" in the release process mean what it says.

`make action-pin-check` is the second recurrence guard: every `uses:` under
`.github/workflows/` must name a 40-character commit SHA with the version in a
trailing comment. A tag is a pointer its owner can move, so an unpinned action
runs whatever it points at on the day. Pinning alone only freezes that, which
is why `.github/dependabot.yml` is the second half -- weekly, grouped into one
pull request, and it rewrites the trailing comment alongside the SHA. Neither
half is worth much without the other: a pin nobody bumps is a dependency
nobody looks at again.

`make install-ref-check` is the third: every install instruction in the root
README and in `plugins/*/README.md` must name the marketplace in
`.claude-plugin/marketplace.json`. The five retired repositories are still
served, so a stale badge, Desktop step or "latest release" link resolves and
renders correctly while sending the reader to a marketplace that will never
publish again -- a link checker passes on every one of them, which is why this
compares against a file in the tree instead. An `uninstall` or a
`marketplace remove` is exempt: it names what the reader is getting rid of
rather than where they are going. The references the repository keeps on
purpose are a table in the script, reconciled in both directions -- an
exemption that matches nothing fails, and the pass line says how many are held
back. It does not read `plugins/*/docs/releases/`, which record what shipped at
that version.

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
