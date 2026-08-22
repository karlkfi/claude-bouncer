# Working in claude-bouncer

Five Claude Code guard plugins in one marketplace. Each plugin under
`plugins/<name>/` is self-contained and has its own `CLAUDE.md` covering its
rules, its backlog, and its release process. Read that one for plugin work.
This file covers what is true of the repo as a whole.

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

## Checks

```
make check    # drift gate, parser tests, all five plugin suites
```

Run it before proposing a change is done. A change under `lib/` reaches all
five guards, so say which ones you re-ran.

Python 3.9 is the floor. exit-status-guard supports it and CI runs the shared
parser against it, so 3.10+ syntax in `lib/` breaks that job and nothing else,
which makes it easy to miss locally.

## Links to the retired repositories

The plugins came from five separate repos, and 279 links still point at them:
issue and pull-request permalinks, tag-pinned `blob` and `compare` paths, and
Actions runs. Those resolve where they are and must stay. Only live
instructions moved: `/plugin marketplace add`, `/plugin install`, and the
"report it at" URL a guard prints in a denial.

Dated notes under `plugins/*/docs/releases/` keep the old instructions. They
record what shipped at that version.
