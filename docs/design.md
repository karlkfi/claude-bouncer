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

zsh matters here. Claude Code's Bash tool runs zsh, and `PIPESTATUS` is a
bash-ism: in zsh it expands to **empty**, so `[ "${PIPESTATUS[0]}" -eq 0 ]`
does not fail loudly, it reads as success. The recovery people reach for is
itself a false green.

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

The escape hatch has to reach the model too, which is why it is a command
prefix (`PIPE_GUARD_OVERRIDE=<reason>`) rather than a setting: the model can
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

The fix is that `is_mutator` runs the two screens `is_gate` already ran —
`exempt` wins, and a probe is not a state change — so a read form is expressed
as a registry row rather than as code. Where the subcommand allows it, the write
forms are named directly instead, which is how `kubectl rollout` splits: only
`restart`, `undo`, `pause`, and `resume` are mutators. That leaves `rollout
status` a **gate**, which it should be — it waits for a condition, so a pipe
still swallows the answer even though the command changes nothing.

This does not widen what slips through. The property rule 3 protects is that a
gate's failure must not be ignored before a state change, and a read is not a
state change.

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

**Repo-state checks.** The Go original also warns on a `git push` onto a base
that moved into the branch's own files, and a `gh pr create` overlapping an open
PR. Those are unrelated questions that happen to share an entry point — they
belong with `claude-branch-guard` and `claude-pr-sentinel` respectively, and are
not ported here.

**Gate-then-gate with `;`.** `make lint; make test` really does lose
`make lint`'s status. Rule 3 is scoped to state-changing commands because that
is where the consequence is a publish rather than a misreported check, and
because gate-then-gate is common enough that denying it would train the
override reflex — which costs more than the rule is worth.

**`bash -c '…'` bodies.** The quoted string is one word to the tokenizer. Adding
recursion into it is tractable (workspace-guard does it) but has not been needed;
if it is, port that too rather than reimplementing.
