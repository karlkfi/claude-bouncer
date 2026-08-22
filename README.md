# claude-bouncer

🛡️ Five guard plugins for Claude Code, in one marketplace.

Each one sits on `PreToolUse` and answers a different question about the command
Claude is about to run: does it reach outside the workspace, does it push to a
protected branch, does it point at production, does its exit status survive, is
it about to block the session for ten minutes. They deny what is clearly wrong,
ask about what is ambiguous, and stay quiet otherwise.

They used to live in five repositories. This is where they live now.

## Install

```
/plugin marketplace add karlkfi/claude-bouncer
```

Then install whichever you want. They are independent and can be installed in
any combination:

```
/plugin install workspace-guard@claude-bouncer
/plugin install branch-guard@claude-bouncer
/plugin install prod-guard@claude-bouncer
/plugin install exit-status-guard@claude-bouncer
/plugin install foreground-guard@claude-bouncer
```

## The guards

| Plugin | Version | What it stops |
| --- | --- | --- |
| [workspace-guard](plugins/workspace-guard) | 1.10.0 | `grep`/`sed`/`jq`/`cat` reading or writing outside the workspace, and blind process kills |
| [branch-guard](plugins/branch-guard) | 1.9.0 | Commits and pushes to a protected branch, and destructive `git`/`gh` commands. Auto-approves the safe ones |
| [prod-guard](plugins/prod-guard) | 2.5.1 | Mutating `kubectl`/`helm`/`terraform`/`gcloud`/`aws` aimed at production, or relying on ambient context that can change under it |
| [exit-status-guard](plugins/exit-status-guard) | 2.0.0 | A gate whose failure reads as success: piped into a filter, backgrounded behind an `echo`, or sequenced before a state change with `;` |
| [foreground-guard](plugins/foreground-guard) | 0.5.1 | Polling and watching on the main thread, and slow commands about to be killed by too short a timeout |

Each plugin's README covers its rules, its config file, its override variable,
and its `/friction-report` command. Start there. This page is the map.

## The shared parser

All five have to read a shell command before they can judge it, and all five
used to carry their own lexer to do it. The copies drifted. On 2026-08-20
workspace-guard and exit-status-guard fixed the same heredoc bug on the same
day, with different mechanisms, and each fix left a gap the other had already
closed. Neither gap was visible from inside the repository that had it, because
both test suites were green.

`lib/bouncer_parse.py` is the single copy they now share: comment stripping,
heredoc-body removal, command-substitution scanning, `shlex` tokenizing,
operator repair, and command-head normalizing.

What is deliberately not shared is segmentation. foreground-guard needs to know
which segment was backgrounded, exit-status-guard needs the operator that joined
two commands, and prod-guard judges an unterminated heredoc that bash would
treat as data. Forcing one answer onto another guard's question is how the
copies diverged in the first place, so each guard keeps its own segmenter.

### Why every plugin has a copy of it

Claude Code copies a plugin into `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`
at install time, and a path that climbs out of the plugin root does not resolve
there. A symlink to a sibling is dereferenced for a git-hosted marketplace, but
skipped for `--plugin-dir` and local-path installs, which are how this repo gets
tested before release. So each plugin carries a vendored copy under its own
`lib/`, and CI fails if one has drifted:

```
make sync         # copy lib/bouncer_parse.py into each plugin
make sync-check   # fail if a copy is stale
```

Edit `lib/bouncer_parse.py`. Never a vendored copy.

## Layout

```
.claude-plugin/marketplace.json   the five entries, sourced from ./plugins/<name>
lib/bouncer_parse.py              the shared parser
scripts/sync-lib.py               vendors it, and the CI drift gate
tests/                            parser tests, gate tests, release-note tests
plugins/<name>/                   one plugin, self-contained
```

## Development

```
make check    # what CI runs: drift gate, parser tests, all five suites
make help     # the rest of the targets
```

2536 tests: 1326 workspace-guard, 477 branch-guard, 427 prod-guard, 203
foreground-guard, 55 exit-status-guard, 48 shared. branch-guard drives its hook
through a shell harness. The others use `unittest`. Python 3.9 is the floor,
because exit-status-guard supports it.

CI keeps the per-plugin jobs separate rather than collapsing them into one
matrix. They differ in Python floor, in OS coverage, and in the mutation-control
jobs that break a rule and require the suite to go red. A matrix that averaged
those would weaken every one of them to the weakest.

## Migrating from the old repositories

The five plugins were published from `karlkfi/claude-workspace-guard` and its
four siblings. Those repositories are being retired. Their history came with the
move, and commit SHAs are unchanged, so `git blame` still reaches the original
commits and old links still resolve.

If you installed a guard from its own marketplace, remove that marketplace and
add this one. The plugin names did not change, only the marketplace half of
`name@marketplace`:

```
/plugin marketplace remove workspace-guard
/plugin marketplace add karlkfi/claude-bouncer
/plugin install workspace-guard@claude-bouncer
```

## Privacy

The guards read the command they are asked about, plus local config and context
that command already reaches (kubeconfig, git remotes, environment). Nothing
leaves the machine. Each plugin has a `PRIVACY.md` with the specifics.

## License

MIT. See [LICENSE](LICENSE).
