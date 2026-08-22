# Plan: close the pattern→pid→kill laundering gap (issue 125 follow-up)

**Goal:** stop a blind cross-workspace kill from dodging the unanchored-kill deny
by deriving pids from a pattern instead of naming one, and stop a clean guarded
command from laundering any kill into a blanket `allow`.

**Approach:** two separable changes in `analyze_command`. (1) A signalling
command anywhere in the string suppresses the `guarded` flag, so the string
defers instead of emitting `allow`. (2) Pattern operands of the pid *sources*
(`pgrep`, and grep-family segments of a pipeline containing `ps`) run through the
existing `kill_operand_anchored` check; when a launderable kill is present and no
collected pattern anchors, emit the existing `'kill'` offender.

Follow-up to [the unanchored-pkill deny](unanchored-pkill-deny.md); tracking
issue: <https://github.com/karlkfi/claude-workspace-guard/issues/125>.

## The gap

Measured with the hook at 13bb4d1, project root basename `wt-a`:

```
deny     pkill -f ginkgo
defer    kill $(pgrep -f ginkgo)
defer    pgrep -f ginkgo | xargs -r kill
allow    ps -eo pid,command | grep ginkgo | grep -v grep | awk '{print $1}' | xargs -r kill
defer    for p in $(pgrep -f ginkgo); do kill $p; done
```

Every one of these kills the same processes as the denied first line. The fourth
is the worst: `grep` and `awk` are clean guarded commands, so `handle_bash`
emits its blanket `allow` ("Guarded commands target workspace/pipe only") for the
whole string *including the kill* — the guard actively green-lights the laundered
kill rather than merely missing it.

Demand: across 427 local session transcripts / 35,289 Bash commands, 3 of 24
kill-bearing commands use a pattern-fed form; two are the same real
`ps … | grep … | awk … | xargs -r kill`. The forms that must stay untouched are
`kill <literal pid>` (8 occurrences), `kill $pid` for the command's own
backgrounded child (6), and `kill -0` liveness probes (1).

## Part 1 — a clean guarded command never launders a kill

`handle_bash` emits `allow` when `guarded` is set and nothing offended. A
signalling command in the same string makes that `allow` a lie about the string
as a whole, because `allow` short-circuits the user's own permission settings.

So `analyze_command` tracks whether any group signals a process, and returns
`guarded and not signal`. With no offenders that yields a **defer** — normal
permissions apply to the kill — which is the same posture an *anchored* `pkill`
already gets, and for the same stated reason.

This half is worth shipping on its own: it removes an active green-light without
depending on any provenance judgement.

## Part 2 — pattern provenance into the kill

### What counts as a pid source

* **`pgrep`** — its pattern operands, extracted by the same flag table `pkill`
  uses (procps-ng documents the two together, and `KILL_CONSUME['pkill']` is
  already the union of the procps and BSD option sets). Counted string-wide: the
  `kill $(pgrep …)` form puts the source in its own command group, not in the
  kill's pipeline.
* **grep-family** (`grep`, `egrep`, `fgrep`, `rg`) — its *pattern* operands, but
  only in a pipeline that also contains a `ps` segment. A `grep` reading
  ordinary files has nothing to do with pids; a `grep` filtering `ps` output is
  the second half of `pgrep`.

Patterns are extracted from the same `SPEC` row `files_in_command` uses, so
`-e`/`--regexp` values are collected and the leading positional counts as a
pattern only when no `prog_suppressed_by` flag fired. That precision is
load-bearing in the *unsafe* direction: mistaking a file operand for a pattern
would let `grep foo wt-a/list.txt` anchor a pipeline it has no business
anchoring.

Each pattern is anchor-tested at collection time, against the group's own tracked
cwd, by the existing `kill_operand_anchored` — so `~`, `$(pwd)` and
`$(git rev-parse --show-toplevel)` resolve exactly as they do for a `pkill`
pattern, and an operand still carrying a `$VAR` never anchors.

### What counts as a launderable kill

`kill` (directly, or as the command word of an `xargs` group) whose operands are
**not** provably literal. An operand list that is entirely `<digits>` or `%job`
was not derived from a pattern, so:

```
kill 1234        kill -0 4321        kill %1        # never launderable
kill $p          kill $(…)           xargs -r kill  # launderable
```

That single rule is what keeps the 15 measured safe kills untouched even when
they share a command string with a `pgrep`. `pkill`/`killall` are deliberately
**excluded** from Part 2 — they have their own anchor rule, and folding them in
would let an unrelated unanchored `grep` pattern turn a correctly anchored
`pkill` into a deny.

### The rule

A launderable kill **plus** at least one collected pattern **and** no collected
pattern anchoring ⇒ one `'kill'` offender, reusing `build_kill_hint` and the
`decide` upgrade to `deny`. "Any pattern anchors ⇒ defer" mirrors the `pkill`
rule and is what keeps `ps … | grep "wt-a/x" | grep -v grep | …` (whose second
`grep`'s pattern is the bare word `grep`) from denying.

Substitution bodies contribute their sources and signals upward, so
`kill "$(pgrep -f ginkgo)"` — where the quoting hides the body from the outer
tokenizer — is caught with the source and the kill on opposite sides of the
recursion.

### Provenance is co-occurrence, not dataflow

The hook does not prove that the pids the kill receives came from the pattern it
found. Within one command string that distinction is not worth its complexity,
and the literal-pid rule already removes the case it would matter for. The cost
is a false deny on a string that both searches by pattern and kills an unrelated
pid it derived some other way; `WORKSPACE_GUARD_OVERRIDE=<reason>` covers it.

## Deliberate limitations

* **A filter that isn't grep is invisible.** `ps … | awk '/ginkgo/ {print $1}' |
  xargs kill` collects no pattern, so it defers rather than denies. Treating an
  `awk` program as a pattern would make the common `awk '{print $1}'` an
  unanchorable pattern and deny every correctly anchored pipeline. Closed since,
  as [Q60](q60-ps-pid-source.md) — by moving the pid source off the filter and
  onto `ps`, so no filter has to be read. The reasoning here understated the
  case against reading `awk`: it is also *unsafe*, since an inverting program
  scans as anchored.
* **A kill inside a quoted `sh -c` string** (`xargs -I{} sh -c 'kill {}'`) is one
  token to the tokenizer, so it reads as neither a signal nor a launderable kill.
  Half-closed by [Q60](q60-ps-pid-source.md): such a string no longer earns the
  blanket `allow`. The body is still unparsed — that is Q61.
* **Bash only.** PowerShell's `Stop-Process` got its own anchor rule in PR 130,
  but not the `allow` suppression of Part 1 — a clean `Get-Content` in the same
  statement still spoke for the kill there. Closed since, as Q59.

## One rule the smoke tests forced

An **inverting** `grep -v` must not contribute an anchorable pattern. `-v`
excludes rather than selects, so `ps … | grep ginkgo | grep -v wt-a/skip |
xargs kill` would otherwise read as anchored by the exclusion while killing
every OTHER checkout's ginkgo. An inverting grep, and one whose patterns live in
a `-f` file, both report an `UNREADABLE_PATTERN` stand-in instead: it can never
anchor, and reporting nothing there would have read as "not a pid source".

## Deliverables

- [x] `scripts/bash-workspace-guard.py` — `SIGNAL_CMDS`, `LITERAL_PID_RE`,
      `signal_command`, `pgrep_operands`, `grep_pattern_operands`, per-pipeline
      grouping, the `KillFacts` plumbing through `_analyze_command`, the
      `guarded` suppression, the launder offender.
- [x] Tests — unit (signal classification, literal-pid rule, pattern
      extraction, the invert rule) + e2e (the five measured shapes, the safe
      shapes, anchored pipelines, exclusions, override downgrade, bypass mode,
      substitution boundary). 28 new cases; suite at 1032.
- [x] `README.md` — decision-table rows, a "Kills fed by a pattern" subsection,
      What-it-does, How-it-works step 13, Limitations, agent-guidance bullet.
- [x] `docs/design.md` — why `allow` speaking for a whole string is the general
      lesson, and why the literal-pid rule is the narrow part.
- [x] `docs/STATUS.md` — Q60 for the non-grep filter gap; Q59 narrowed to the
      PowerShell half of the `allow` suppression, which this change leaves open.
- [x] `.claude-plugin/plugin.json` — nothing; `pkill` and `process` already
      cover this.
