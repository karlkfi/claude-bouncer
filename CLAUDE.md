# Working in this repo

pipe-guard is a Claude Code PreToolUse hook. It denies Bash commands whose exit
status is the answer and gets discarded — piped into a filter, backgrounded
behind an `echo`, or sequenced before a state change with `;`.

## Layout

| Path | What it is |
|---|---|
| `scripts/bash-pipe-guard.py` | The whole guard. Stdlib only. |
| `pipe-guard.json` | Shipped gate registry. Data, not code. |
| `scripts/run-python-hook.cmd` | Cross-platform launcher. Do not "simplify" it. |
| `scripts/friction-report.py` | Read-only transcript analyzer. |
| `tests/test_pipe_guard.py` | Table-driven suite. Both directions asserted. |
| `tests/test_packaging.py` | Asserts the wired-up files ship runnable. |
| `tests/launcher_check.py` | Drives the hook through the launcher. Not in `discover`. |
| `hooks/hooks.json` | Wires the PreToolUse matcher on `Bash`. |

## Rules that are not negotiable

**Stdlib only.** The guard imports `sys, os, json, re, shlex, collections` and
nothing else. It runs on every Bash call in every session on every machine; a
pip dependency is a machine where the guard silently does not run.

**Never `ask`, always `deny`.** An `ask` goes to the user; a `deny` goes to the
model. Both block. Only the deny puts the explanation where the command gets
rewritten. Any change that introduces an `ask` needs an argument for why the
user, not the model, is the right audience.

**Fail silent, never fail loud.** Every unparseable input, missing file, and bad
pattern returns "no opinion". A hook that errors is a hook that makes ordinary
work fail. The tests pin this: unbalanced quotes, `| |`, malformed JSON on
stdin, and a registry pattern that does not compile all have to stay quiet.

**Do not hand-roll shell parsing.** The segmentation layer in
`bash-pipe-guard.py` is ported from `claude-workspace-guard` and is kept
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

Add the pattern to `pipe-guard.json`, anchored with `^`, matched against the
head. Then add table cases in **both** directions: the gate piped (must deny)
and something that merely names it (must stay silent). A gate with only a
positive case is how a rule starts denying `git log`.

Do not add per-tool `--version`/`--help` exemptions. Probes are recognized
structurally for every gate; a per-tool pattern fixes one tool and leaves the
class.

## Adding a mutator

Mutators drive rule 3 only. Keep the list to genuinely state-changing,
outward-facing operations — publish, push, apply, deploy. The rule's value is
that `make check; git push` is unambiguously wrong; widening `mutators` to
ordinary local commands turns it into noise.

A mutator is screened the same way a gate is: `exempt` wins over it, and a
capability probe is not a state change. So when a subcommand has both a read
form and a write form — `git tag -l` beside `git tag -a`, `kubectl rollout
status` beside `kubectl rollout restart` — name the write forms in `mutators`
where the subcommand allows it, and put the read forms in `exempt` where it does
not. Getting this wrong denies a pure read as a publish, and that denial has no
correct rewrite: there is nothing for `&&` to gate ([#11](https://github.com/karlkfi/claude-pipe-guard/issues/11)).

