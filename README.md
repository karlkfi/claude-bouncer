# pipe-guard

A Claude Code hook that denies shell commands whose exit status **is** the
answer and gets thrown away.

```
make check 2>&1 | tail -30; echo "EXIT=$?"
```

That prints `EXIT=0` for a failing gate. A pipeline reports its last stage's
status, so what you read is `tail`'s. zsh — the shell Claude Code's Bash tool
runs — has no `PIPESTATUS` to recover it. The run looks green, the agent reports
green, and the failure ships.

pipe-guard catches that shape before the command runs, and tells the model why.

## What it denies

Three ways a status disappears, all of which turn a failure into a false green.

### 1. Piped into a filter

```bash
make check 2>&1 | tail -30      # denied — you get tail's status
go test ./... | grep FAIL       # denied
(cd sub && cargo test) | head   # denied — the subshell's status is cargo's
```

The fix is to redirect and reconcile status against output separately:

```bash
make check > tmp/check.log 2>&1; echo "EXIT=$?"; grep -E 'FAILED|Error' tmp/check.log
```

`set -o pipefail` earlier in the same command suppresses this, and so does
reading zsh's `$pipestatus` (lowercase, 1-indexed). Reading `$PIPESTATUS`
(uppercase) is denied on its own, gate or no gate — it does not exist in zsh, so
it expands to empty and every test against it reads as success.

### 2. Backgrounded behind something that cannot carry failure

```bash
# with run_in_background: true
make check > tmp/c.log 2>&1; echo "EXIT=$?"    # denied
```

A `;`-list yields its **last** statement's status. The log says `EXIT=1`, and
Claude Code's task notification says `completed (exit code 0)`, because the
chain ended with an `echo`. Capture the status and re-raise it:

```bash
make check > tmp/c.log 2>&1; rc=$?; echo "EXIT=$rc"; exit $rc
```

A literal `exit 0` at the end is treated as a deliberate discard, and is the way
out when a background call's status genuinely does not matter. The same command
in the **foreground** is fine and is not denied: there the trailing echo prints
the real status where it can be read.

### 3. Sequenced before a state change with `;`

```bash
make check; git push            # denied — the push runs either way
npm test; npm publish           # denied
git add .; git commit -m wip    # denied — a failed add commits nothing new
```

Here the status is read correctly and then ignored. `&&` is the fix, and is
never denied.

## What it does not deny

The registry is deliberately not "every command". A guard that denies every
pipeline gets overridden reflexively and then means nothing.

```bash
git log --oneline | head -5             # informational, not a gate
cat tmp/check.log | tail -30
make help | grep check                  # exempt
make --version | head -1                # a capability probe
shellcheck --version | grep 0.11        # probes are structural, not per-tool
git show origin/main:CLAUDE.md | grep -n "make check"
git commit -m "fix: make check | tail was reporting EXIT=0"
```

The last two matter most. Gate patterns are matched against the **head of a
shell segment** — its command word and arguments, after leading `VAR=val`
assignments and `bash`/`sudo`/`time`-style wrappers are peeled — never against
the raw command string. A pattern matched against the raw string also fires on
every `git show`, `grep`, and commit message that merely *names* the command.
A heredoc body is data, so a piped gate quoted in one is text; no rule handles
that case, the parser does.

Capability probes (`--version`, `--help`, `-V`, `-h`) are recognized
structurally for every gate rather than by per-tool exemptions — a per-tool
pattern fixes the tool it was reported against and leaves the whole class.
`-v` is deliberately **not** a probe flag: it means `--version` to `make` but
verbose to `go test`, and exempting it would exempt `go test -v ./... | tail`,
which is the bug this guard exists to catch.

## Install

```bash
/plugin marketplace add karlkfi/claude-pipe-guard
```

```bash
/plugin install pipe-guard@pipe-guard
```

Requires Python 3 on `PATH`. No pip packages — the guard is stdlib only.

## Deny, never ask

Every verdict is a `deny`. That is a deliberate choice, not a severity call.

A deny's reason is shown to **the model**, so the fix lands where the command
gets rewritten. An `ask` goes to the **user**, and Claude never learns why the
command was wrong — you approve or reject, and the next command has the same
bug. Both are equally blocking; only one teaches.

## Break-glass

```bash
PIPE_GUARD_OVERRIDE=<reason> <command>
```

An environment prefix, because that is the only form a PreToolUse hook can see:
it reads the command string, and the session cannot set a variable in the hook's
own environment. The reason is required — a bare `PIPE_GUARD_OVERRIDE=` is the
switch-it-off form and is ignored.

The name only counts as an override in command position. Quoted in a commit
message or echoed into a pipe it is an argument, and disables nothing.

**A rule that needs an override routinely is a defect to fix in the registry,
not to override.** [File it](https://github.com/karlkfi/claude-pipe-guard/issues).

## Configuring the registry

Shipped defaults live in [`pipe-guard.json`](pipe-guard.json), covering make,
npm/pnpm/yarn/bun, pytest/ruff/mypy, go, cargo, gradle/maven, dotnet, swift,
bazel, cmake/ninja, rake/rspec, shellcheck, terraform, docker, kubectl, helm,
`scripts/*.sh`-style repo scripts, and the `git`/`gh` verbs whose status matters.

A project extends them with its own `.claude/pipe-guard.json`:

```json
{
  "gates": ["^bazelisk(\\s|$)"],
  "exempt": ["^make\\s+print-config(\\s|$)"],
  "mutators": ["^./deploy.sh(\\s|$)"]
}
```

Project entries are **added** to the defaults, so naming one extra gate does not
silently drop the rest. Set `"replace": true` to take full control instead.

Patterns are Python regular expressions, anchored with `^` (enforced by the test
suite). POSIX bracket classes (`[[:space:]]`) are translated, so a pattern
copied from an ERE-based registry works unchanged.

Point `PIPE_GUARD_REGISTRY` at a file to override the defaults wholesale.

## Reporting

```
/friction-report
```

Ranks the guard's denials by rule and by the gate whose status was being lost,
read from session transcripts Claude Code already wrote. No telemetry — see
[PRIVACY.md](PRIVACY.md). The counts are denials caught, not a rate: the guard
emits nothing when it has no objection, so there is no denominator.

## Limitations

- **A `bash -c '…'` body is not analyzed.** The quoted string is one word to the
  tokenizer, so a gate inside it is invisible. Backtick and `$(…)` bodies *are*
  analyzed.
- **Gate-then-gate with `;` is not denied.** `make lint; make test` does lose
  `make lint`'s status, but rule 3 is scoped to state-changing commands, where
  the consequence is a publish rather than a misreported check. Widening it
  would deny a very common and mostly harmless shape.
- **A command the tokenizer cannot parse gets silence, not a guess.** Unbalanced
  quotes, `| |`, and similar defer to normal permissions.
- **The registry is the detector.** Nothing is denied on shape alone; an
  unregistered command piped into `tail` is fine by this guard.

## Prior art

The decision logic is ported from the `pipedgate` Go tool in a private
repository, where it runs over `mvdan.cc/sh`. This is a from-scratch Python
implementation of the same rules; the shell analysis reuses
[claude-workspace-guard](https://github.com/karlkfi/claude-workspace-guard)'s
tokenizer rather than hand-rolling a shell-grammar scanner, which is the
documented way this class of tool fails — silently, in both directions.

Siblings: [workspace-guard](https://github.com/karlkfi/claude-workspace-guard),
[branch-guard](https://github.com/karlkfi/claude-branch-guard),
[prod-guard](https://github.com/karlkfi/claude-prod-guard),
[foreground-guard](https://github.com/karlkfi/claude-foreground-guard),
[pr-sentinel](https://github.com/karlkfi/claude-pr-sentinel).

## License

MIT
