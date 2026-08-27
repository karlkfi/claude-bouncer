# Design

## The problem

An agent's report that a gate passed is a claim, and the evidence behind it is
an exit status. Three common shell shapes destroy that status while leaving the
command looking like it succeeded.

The shapes are not exotic. They are the shapes you reach for when you want to
*read* a gate's output — `| tail -30` to see the last lines, `> log; echo EXIT`
to background a long run, `;` to chain two steps. Each is a reasonable instinct
that silently deletes the answer.

| Shape | Status you get | Status you wanted |
|---|---|---|
| `gate \| tail -30` | `tail`'s | the gate's |
| `gate > log; echo "EXIT=$?"` backgrounded | `echo`'s | the gate's |
| `gate; git push` | the push's | the gate's, as a precondition |

The shell matters here. `PIPESTATUS` is a bash feature, and under a shell
without it — zsh among them — it expands to **empty**, so
`[ "${PIPESTATUS[0]}" -eq 0 ]` does not fail loudly, it reads as success. The
recovery people reach for can itself be a false green. Which shell the Bash
tool runs is a per-machine setting (`CLAUDE_CODE_SHELL`), and the guard reads
none of it, so every reason it prints has to hold in either shell.

## Why deny rather than ask

A PreToolUse hook can return `ask` or `deny`. Both block the call. They differ
in audience:

- `ask` renders a permission prompt **to the user**. The model sees an approval
  request, not a diagnosis. Approve it and the next command has the same bug.
- `deny` returns a reason **to the model**, which is the thing that will rewrite
  the command.

The whole value of this guard is the explanation, and the explanation is only
useful to whoever is holding the keyboard for the rewrite. That is the model.
So every verdict is a deny, regardless of severity.

It also makes the reason the only place attribution can live. An `ask` renders a
prompt that names the hook; a deny hands over the string and nothing else, so a
model running under several guards has only the advice to tell them apart by.
Hence the `exit-status-guard: ` opener, matching the sibling guards. The cost is
that anything parsing a reason from the front has to skip it —
`scripts/friction-report.py` reads the gate out of the backticks that follow,
and accepts denials recorded before the prefix shipped.

The escape hatch has to reach the model too, which is why it is a command
prefix (`EXIT_STATUS_GUARD_OVERRIDE=<reason>`) rather than a setting: the model can
apply it in the next turn without a round trip through the user.

## Why the segment head, and not the command string

The naive implementation matches gate patterns against the raw command string.
It works on the first test and then denies:

```bash
git show origin/main:CLAUDE.md | grep -n "make check"
git commit -m "fix(ci): make check | tail was reporting EXIT=0"
grep -rn "make check | tail" docs/
```

All three merely *name* a gate. A raw-string match cannot tell a command from a
mention, so it fires on documentation, commit messages, and searches — on every
Bash call, all day.

The fix is structural: parse the command, split it into segments, and match
against each segment's **head** — the command word and its arguments, after
peeling leading `VAR=val` assignments and wrapper words (`time`, `sudo`, `bash`,
`env`, …). Quoted text tokenizes as a word, never as a command in head position,
so a mention cannot match. Nothing special-cases commit messages; the tokenizer
makes them incapable of matching.

The same property covers heredocs for free. Bodies are stripped before
tokenization, so a piped gate inside one is data. A body whose delimiter is
*unquoted* is collected separately and scanned for `$PIPESTATUS`, because bash
expands those — the same text is a note about the bug under `<<'EOF'` and the
bug itself under `<<EOF`.

## Why probes are structural

`shellcheck --version | grep 0.11` prints and exits. There is no result for the
pipe to swallow, so denying it is pure friction.

The tempting fix is an exempt pattern for the tool that got reported. That fixes
`shellcheck` and leaves `make --version`, `cargo build --help`, `go vet -h`, and
every gate added later. So probe flags (`--version`, `--help`, `-V`, `-h`) are
recognized for **every** gate, read from the parsed words rather than the joined
head — `git commit -m "bump --version output"` is one word, not a flag, and
stays a gate.

`-v` is deliberately excluded. It is `--version` to `make` and *verbose* to
`go test`, so exempting it would exempt `go test -v ./... | tail`, which is the
canonical bug. `make -v | head` stays denied and `--version` is the way out.

## Why a read is screened out of the mutator list

The registry matches at subcommand granularity, and several subcommands carry a
read form and a write form under one name: `git tag -l` beside `git tag -a`,
`kubectl rollout status` beside `kubectl rollout restart`. A head match alone
cannot tell them apart, so rule 3 picked the write as the gate and the read as
the state change — exactly backwards — and denied a listing on the grounds that
a failed check still publishes. Reading a tag publishes nothing, so that denial
had no correct rewrite: `&&` gates a state change, and there was none.

The fix is that `is_mutator` runs the screens `is_gate` already ran — `exempt`
wins, and a probe is not a state change. Where the subcommand allows it, the
write forms are named directly, which is how `kubectl rollout` splits: only
`restart`, `undo`, `pause`, and `resume` are mutators. That leaves `rollout
status` a **gate**, which it should be — it waits for a condition, so a pipe
still swallows the answer even though the command changes nothing.

This does not widen what slips through. The property rule 3 protects is that a
gate's failure must not be ignored before a state change, and a read is not a
state change.

## Why the read/write split is not an enumeration of read forms

The first version of that split listed `git tag`'s read forms in `exempt`, one
flag at a time. The list was shorter than git's — `--sort`, `--column`, `-i`,
`--omit-empty`, and `--format` were missing — so a tag listing was still read as
a gate and denied for being piped, and the workaround was to add a redundant
`--list` to a command already in list mode. `git stash list` and `git worktree
list` had no row at all: the listing exemption next to them was `gh`-only.

Enumerating reads is the wrong side to enumerate, for the same reason a per-tool
`--version` row is: it fixes what got reported and leaves the class. Both halves
are inverted now.

- A **read verb in the subcommand path** — `list`, `ls`, `show`, `view`,
  `history` — is recognized for every gate, the way probe flags are. It is read
  from the third word on and the scan stops at the first flag, so `git commit -m
  show` is a commit and a `make` target named `show` is still a gate. The cost
  is a branch or file named `list` going unscreened after `git checkout`, which
  is a missed catch rather than a denial with no rewrite.
- `status` is deliberately not a read verb. `kubectl rollout status` waits for a
  condition and reports it as an exit code, so a pipe swallows a real answer.
- Where no verb splits the two — `git tag --sort` lists, `git tag -a` publishes
  — the **write forms** are what `gates` and `mutators` name, and everything
  else is a read by default. git grows listing flags every release and grows
  tag-creation flags never, so this is the side that does not go stale.

## Why stdlib Python, given the prior art

The Go implementation this ports from replaced a shell version that spent 175 of
its 257 lines hand-rolling a shell-grammar scanner — quote state, heredoc
bodies, nesting, matched delimiters — because regular expressions cannot count
brackets. It failed the way that predicts: silently, in both directions. The Go
rewrite sits on `mvdan.cc/sh`, a real parser.

Python's stdlib has no shell parser. The judgment call is that it does not need
one *here*, because the hard part is already written and tested:
`claude-workspace-guard`'s `bash-workspace-guard.py` is 4,600 lines of stdlib
Python doing comparable shell analysis — cd tracking, `$VAR` resolution, heredoc
bodies, redirects, loop expansion, command-substitution whitelisting — in
production across every repo on this workstation.

So the rule for this repo is: **borrow that segmentation layer, do not write a
new one.** The ported functions (`strip_heredoc_bodies`, `strip_comments`,
`command_substitutions`, `_scan_dollar_paren`, `split_operator_runs`, and the
shlex configuration) carry all the quote-state tracking and bracket counting,
and are kept structurally identical to their source so a fix there transfers by
inspection.

One divergence is deliberate. `strip_comments` also drops a `\`-newline pair,
because this guard makes the newline a punctuation char and workspace-guard does
not: unfolded, a continuation surfaces as a command boundary the shell never
produced, and `make check \` / `&& git push` gets denied as if it were `;`
([#8](https://github.com/karlkfi/claude-pipe-guard/issues/8)). POSIX removes
backslash-newline before tokenizing, so the fold belongs in the walk that is
already tracking quote state, not in a second one.

What is new here is flat list bookkeeping over an already-tokenized stream:
grouping tokens into segments, recording the operators on either side, and
tracking paren depth. That is not grammar scanning, and it is not where this
class of tool fails.

If a future change requires raw-text scanning that the ported layer does not
already do, that is the signal the judgment call was wrong — not a cue to write
the scanner.

## The status model

Each rule asks one question: *whose exit status does the caller actually see?*

**Parens are transparent.** Closing a subshell hands its last statement's status
outward unchanged, so in `(cd sub && go test ./...) | tail` the operator that
consumes `go test`'s status is the `|`, two tokens later. Segments therefore
carry the whole operator run on each side, and `next_op()` skips `(` and `)` to
find the one that decides.

**`&&` lets either side carry; `||` and `|` carry only the right.** A statement
is evaluated left to right, matching bash's association: in `a && b || c` the
`||` sees `(a && b)` as its left side. `mkdir -p tmp && make check > log` keeps
the gate's status; `make check > log || echo failed` throws it away.

**Anything unrecognized counts as carrying.** An unfamiliar shape gets silence
rather than a guess, in both this rule and the parser. A guard that fabricates
denials on shapes it does not understand gets uninstalled.

## What is deliberately out of scope

**Repo state: a moved base, and an overlapping open PR.** Two checks that read
the repository rather than a status -- a `git push` onto a base whose new commits
edit this branch's own line ranges, and a `gh pr create` where an open PR already
changes them. 1.x ruled both out of scope on the argument that the first belongs
with `claude-branch-guard` and the second with `claude-pr-sentinel`.
[#14](https://github.com/karlkfi/claude-pipe-guard/issues/14) reversed that and
[#16](https://github.com/karlkfi/claude-pipe-guard/issues/16) reversed it back,
before either check appeared in a release.

The reversal rested on parser reuse and hook count, and neither held. Both homes
already parse the command the check needs: `pr-sentinel-hook.py` classifies a
simple command as `pr_create`, `git_push`, or neither, and `branch-guard.py`
reads `git push` down to `--force-with-lease=<dst>[:<expect>]` with
`--no-force-with-lease` cancellation. And `Bash` already carries a `PreToolUse`
hook from each of the three plugins, so moving a check between them adds none.
What #14 actually established was that *duplicating* these checks is worse than
one home -- a repo-local hook denying the same commands needed both override
variables set at once, and every documented escape hatch named one. That is an
argument against two owners, not against the other owner.

Moving them costs something real, which is worth stating rather than
discovering: `base_ref` and `overlap_ignore` were shared between the two checks
and are now configured in two plugins. `overlap_ignore` is the one likelier to
drift, being inherently project-specific. The reasoning behind the parts that
were hard -- line ranges rather than path intersection, a hunk widened by its
context, a merge-driver path discounted conditionally, a release branch never
told to rebase, the root read from the session's `cwd` -- moved with the code to
[branch-guard#91](https://github.com/karlkfi/claude-branch-guard/issues/91) and
[pr-sentinel#71](https://github.com/karlkfi/claude-pr-sentinel/issues/71).

**Gate-then-gate with `;`.** `make lint; make test` really does lose
`make lint`'s status. Rule 3 is scoped to state-changing commands because that
is where the consequence is a publish rather than a misreported check, and
because gate-then-gate is common enough that denying it would train the
override reflex — which costs more than the rule is worth.

**`bash -c '…'` bodies.** The quoted string is one word to the tokenizer. Adding
recursion into it is tractable (workspace-guard does it) but has not been needed;
if it is, port that too rather than reimplementing.
