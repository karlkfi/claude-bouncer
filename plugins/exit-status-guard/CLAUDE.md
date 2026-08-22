# Working in this repo

exit-status-guard is a Claude Code PreToolUse hook. It denies Bash commands
whose exit status is the answer and gets discarded — piped into a filter,
backgrounded behind an `echo`, or sequenced before a state change with `;`.

Work is tracked in the repo-root backlog, [`docs/queue/`](https://github.com/karlkfi/claude-bouncer/blob/main/docs/queue/README.md),
shared with the other four guards. File items with the `exit-status-guard`
label; the issue links below point at the retired `claude-pipe-guard`
repository and are history, not somewhere to file.

## Layout

| Path | What it is |
|---|---|
| `scripts/bash-exit-status-guard.py` | The whole guard. Stdlib only. |
| `exit-status-guard.json` | Shipped gate registry. Data, not code. |
| `scripts/run-python-hook.cmd` | Cross-platform launcher. Do not "simplify" it. |
| `scripts/friction-report.py` | Read-only transcript analyzer. |
| `tests/test_exit_status_guard.py` | Table-driven suite. Both directions asserted. |
| `tests/test_packaging.py` | Asserts the wired-up files ship runnable. |
| `tests/launcher_check.py` | Drives the hook through the launcher. Not in `discover`. |
| `hooks/hooks.json` | Wires the PreToolUse matcher on `Bash`. |

## Rules that are not negotiable

**Stdlib only, and no subprocesses.** The guard imports `sys, os, json, re,
shlex, collections` and nothing else, and it shells out nowhere. It runs on every
Bash call in every session on every machine; a pip dependency is a machine where
the guard silently does not run, and a subprocess is latency on a path nobody
chose to be on.

**Never `ask`, always `deny`.** An `ask` goes to the user; a `deny` goes to the
model. Both block. Only the deny puts the explanation where the command gets
rewritten. Any change that introduces an `ask` needs an argument for why the
user, not the model, is the right audience.

**Fail silent, never fail loud.** Every unparseable input, missing file, and bad
pattern returns "no opinion". A hook that errors is a hook that makes ordinary
work fail. The tests pin this: unbalanced quotes, `| |`, malformed JSON on
stdin, and a registry pattern that does not compile all have to stay quiet.

**Exit status is the whole scope.** Two checks that read the repository
instead of a status -- a `git push` onto a moved base, a `gh pr create`
overlapping an open PR -- were added in
[#14](https://github.com/karlkfi/claude-pipe-guard/issues/14) and removed again
in [#16](https://github.com/karlkfi/claude-pipe-guard/issues/16) without ever
shipping in a release. They live in `claude-branch-guard` and
`claude-pr-sentinel`, which already parse those two commands to the depth the
checks need. Adding a rule here that is not about a lost exit status means
reopening that argument, not extending the guard.

**The 1.x names stay readable.** 2.0.0 renamed the plugin from `pipe-guard`, and
`OVERRIDE_VARS`, `PROJECT_REGISTRY_NAMES`, and `REGISTRY_ENV_VARS` each carry the
old spelling behind the current one. They are undocumented, not deprecated: the
rename reaches this repo's docs and nothing in a downstream repo's `.claude/`
directory or CLAUDE.md, and those name the 1.x forms. A project registry is
resolved to one file rather than merged, so the current name wins outright and
the fallback cannot shadow it -- `TestLegacyNames` asserts both halves, and the
friction report reads both labels for the same reason.

**Do not hand-roll shell parsing.** The segmentation layer in
`bash-exit-status-guard.py` is ported from `claude-workspace-guard` and is kept
structurally identical to its source so a fix there transfers by inspection. If
a change has you writing quote-state tracking or bracket counting from scratch,
stop — that is the documented failure mode for this class of tool, and it fails
silently in both directions. Port from workspace-guard or change the question.

**A suggested rewrite has to run.** The reason strings are copied verbatim by a
session that has just been denied, so a path one names must exist by the time
the command does. `tmp/` does not: it is a build-output name, commonly
gitignored and so absent from a fresh checkout. `with_log_path` resolves the
path per call — the session scratchpad when it is confirmed on disk, otherwise a
form carrying its own `mkdir`. Never name a path not known to exist.

**Match against the segment head, never the raw string.** A pattern matched
against the whole command also fires on every `git show`, `grep`, and commit
message that names the command. This is the single most important design
property here and the easiest one to regress.

**Every deny reason opens with `exit-status-guard: `.** A deny hands the model a
string and no hook name, so the opener is the only attribution it gets, and
`<name>-guard: ` is the form the sibling guards' friction reports key on. The
cost lands on anything parsing a reason from the front: `GATE_RE` in
`scripts/friction-report.py` reads the gate from the backticks that follow, and
keeps the prefix optional because transcripts hold denials from before it
shipped. Change the opener and that reader breaks silently — the report keeps
running and stops naming gates.

**Anything `hooks.json` invokes ships executable.** 1.0.0 shipped the launcher
at mode 644: the shell refused it, the hook exited 126 with nothing on stdout,
and Claude Code read that as no objection, so the guard never fired once
([#3](https://github.com/karlkfi/claude-pipe-guard/issues/3)). Modes live in
the git index — set them with `git update-index --chmod=+x`, not `chmod` alone,
and never invoke a launcher as `sh <file>` in a test, which reads the file as a
script and cannot see the bit at all.

## Testing

```bash
python3 -m unittest discover -s tests
```

The suite is table-driven and asserts **both** directions, because both fail
silently: a rule that stops matching lets the original bug back in, and a rule
that matches too much denies ordinary work on every Bash call.

`TestPositiveControl` is the harness check. It pins the canonical false green
and asserts it denies, so an all-green run cannot mean "the guard never loaded".
Its companion asserts the same command goes silent with an empty registry — the
suite has to be able to tell a real deny from a rule that fires on everything.

The suite invokes the guard with `sys.executable`, which skips
`run-python-hook.cmd` entirely. That launcher gets its own check, run by CI on
all three OSes and locally with:

```bash
python3 tests/launcher_check.py
```

**Before trusting a green run, break something and confirm it goes red.** Copy
the tree to a scratch dir, disable one rule, and check the count of failures.
Each of the three rules and the registry itself were verified this way; a suite
that has never failed is a suite with no evidence behind it.

## Adding a gate

Add the pattern to `exit-status-guard.json`, anchored with `^`, matched against the
head. Then add table cases in **both** directions: the gate piped (must deny)
and something that merely names it (must stay silent). A gate with only a
positive case is how a rule starts denying `git log`.

Do not add per-tool `--version`/`--help` exemptions, or a per-tool row for a
`list`/`show` read. Both shapes are recognized structurally for every gate; a
per-tool pattern fixes one tool and leaves the class.

## Adding a mutator

Mutators drive rule 3 only. Keep the list to genuinely state-changing,
outward-facing operations — publish, push, apply, deploy. The rule's value is
that `make check; git push` is unambiguously wrong; widening `mutators` to
ordinary local commands turns it into noise.

A mutator is screened the same way a gate is: `exempt` wins over it, a
capability probe is not a state change, and neither is a read. Getting this
wrong denies a pure read as a publish, and that denial has no correct rewrite:
there is nothing for `&&` to gate ([#11](https://github.com/karlkfi/claude-pipe-guard/issues/11)).

So when a subcommand has both forms — `git tag -l` beside `git tag -a`,
`kubectl rollout status` beside `kubectl rollout restart` — name the **write**
forms and let the rest fall through as reads. Never enumerate the read forms:
that list is the one git lengthens every release, and the missing entries are
denials with no rewrite ([#19](https://github.com/karlkfi/claude-pipe-guard/issues/19)).
A read verb in the subcommand path (`list`, `ls`, `show`, `view`, `history`) is
already screened structurally for every gate, so `git stash list` needs no row;
`status` is deliberately not one of them, because `kubectl rollout status`
reports its answer as an exit code and a pipe really does swallow it.

