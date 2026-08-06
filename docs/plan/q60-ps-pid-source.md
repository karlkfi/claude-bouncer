# Plan: treat `ps` as the pid source, and stop `sh -c` speaking for a string (Q60)

**Goal:** close the two laundering shapes [the pattern-fed kill deny](pattern-fed-kill-deny.md)
left open — a pid filter that isn't `grep`, and a kill wrapped in `sh -c '…'`.

**Approach:** two independent changes in `_analyze_command`. (1) Stop treating the
*filter* as the pid source and treat **`ps`** as the source, contributing an
`UNREADABLE_PATTERN` when pids can actually reach a launderable kill — same
pipeline, or anywhere inside a substitution body. (2) A shell `-c` body is a
command string the hook cannot read, so it clears `guarded` and the string
defers instead of emitting `allow`.

## The gap

Measured with the hook at d076eb4, project root basename `wt-a`:

```
deny     ps -eo pid,command | grep ginkgo | grep -v grep | awk '{print $1}' | xargs -r kill
defer    ps -eo pid,command | awk '/ginkgo/ {print $1}' | xargs -r kill
defer    ps -eo pid,command | sed -n '/ginkgo/s/^ *\([0-9]*\).*/\1/p' | xargs kill
defer    ps -eo pid,command | cut -d' ' -f1 | xargs kill
defer    ps -eo pid= | xargs kill
defer    kill $(ps -eo pid= | head -1)
allow    cat in.txt; sh -c 'pkill -f ginkgo'
allow    cat in.txt | xargs -I{} sh -c 'kill {}'
```

Every one kills the same processes as the denied first line. Swapping `grep` for
`awk` is enough, and dropping the filter entirely works too — `ps -eo pid= |
xargs kill` reaches every process on the host and collects no pattern at all.

`sh -c` is worse than the Queue row recorded, and worse than a missed deny. The
body is one token to the tokenizer, so **nothing** in it is checked — not just
kills:

```
ask      cat /q60-fake-target
defer    sh -c 'cat /q60-fake-target'
allow    cat in.txt; sh -c 'cat /q60-fake-target'
allow    cat in.txt; sh -c 'echo x > /q60-fake-target'
```

The last two are the Part 1 failure again: a clean guarded command emits the
blanket `allow`, which short-circuits the user's own permission settings, for a
string containing an arbitrary unreadable outside read or write.

## Part 1 — `ps` is the pid source, not the filter

The shipped rule collects a **grep pattern** and promotes it to a pid source when
a `ps` sits in the same pipeline. That framing is what makes every non-grep filter
a hole: it puts the source in the wrong place. `awk`, `sed`, `cut`, `perl`,
`python3` and `head` are not pid sources, and neither is `grep` — **`ps` is**. The
filter only decides which of `ps`'s rows survive.

So `ps` contributes a pid source of its own. Its selection criteria are
unreadable — the hook is not going to parse `-eo` format strings — so it
contributes the existing `UNREADABLE_PATTERN`, which can never anchor. A readable
grep pattern in the same pipeline still *anchors* it under the unchanged
"any collected pattern anchors ⇒ no offender" rule.

Enumerating filters is then unnecessary: the problem dissolves rather than
growing a table that the next filter walks past.

### Provenance: pids must be able to reach the kill

A bare `ps` is a much weaker signal than a grep pattern, so it needs stronger
provenance. The ps-derived source counts only when pids can actually flow to a
launderable kill:

* **the same pipeline** — `ps … | awk … | xargs kill`; or
* **anywhere inside a command-substitution body**, whose output the enclosing
  command consumes by definition — `kill $(ps -eo pid= | head -1)`.

Grep patterns keep their existing string-wide promotion. The asymmetry is
deliberate: a grep pattern is *readable*, so an unanchored one is evidence of
intent to select processes by name wherever it sits, whereas a bare `ps` says
nothing until it is wired to a kill.

That requirement is load-bearing, not a refinement. Without it the rule denies
the commonest debugging idiom there is — background a child, kill it, confirm it
died:

```
./run.sh & pid=$!; sleep 2; kill -TERM $pid; ps -p $pid >/dev/null && echo alive
```

Two real corpus commands have exactly this shape. `ps -p $pid` is a *consumer* of
an already-known pid, not a source; it is in its own group, so no pids flow.

### What it costs

An anchored `awk` program denies:

```
ps -eo pid,command | awk '/wt-a\/ginkgo/ {print $1}' | xargs -r kill    # deny
```

Deliberate. The hook cannot read an awk program, and reading one would be unsafe
rather than merely imprecise: an **inverting** program (`awk '!/wt-a/ {print
$1}'`) would read as anchored while killing every OTHER checkout's processes —
the same trap the shipped `grep -v` rule closes by refusing to anchor on an
exclusion. Anchor the pipeline with a grep, or set `WORKSPACE_GUARD_OVERRIDE`.

## Part 2 — an opaque `sh -c` body must not be spoken for

`sh -c '<body>'`, and its `bash`/`zsh`/`dash`/`ksh` spellings, put a whole command
string inside one token. The hook cannot see what it does, so it must not vouch
for it: any group carrying a shell `-c` clears `guarded`, and the string defers.

This adds no new blocking decision — it removes an `allow` the hook had no basis
to emit. It is Part 1's principle ("`allow` speaks for the WHOLE string") applied
to a second unreadable construct, and like Part 1 it is worth shipping on its own.

The `-c` flag is found by scanning every token of the group, not just the command
word, so the wrappers that actually appear are covered: `timeout 5 bash -c …`,
`xargs -I{} sh -c …`, `find . -exec sh -c … \;`. Only a short-option cluster
counts, so `bash --version` and `sh --help` do not fire.

This does **not** analyze the body — an outside read inside one still goes
unseen, it just no longer comes back `allow`. See Deliberate limitations.

## Measured cost

Both parts, run against every local transcript — 36,077 Bash commands, 1,018 of
them matching `ps`/`kill`/`pgrep`/a shell `-c`:

| Change | Count |
|---|---|
| `allow` → `defer` | 34 |
| new `ask` or `deny` | **0** |

Nothing that runs today starts prompting or blocking. The 34 are strings carrying
a shell `-c` that the guard used to vouch for and now declines to.

## Deliberate limitations

* **A `sh -c` body is still unanalyzed.** Only the `allow` goes away. Analyzing
  it properly is queued separately (Q61): of 113 corpus bodies, recursing would
  newly block 13, but 6 of those are container-internal paths reached through
  `docker exec` / `kubectl exec` wrappers and 4 are ordinary `$VAR` friction —
  3 genuine catches against 10 false ones. It needs a container-exec exclusion
  first, which is its own design problem.
* **Provenance is still co-occurrence within a pipeline**, not dataflow. A
  pipeline that both runs `ps` and kills an unrelated pid derived some other way
  denies; `WORKSPACE_GUARD_OVERRIDE=<reason>` covers it.
* **`ps` is matched by command word.** A wrapper (`busybox ps`, a shell function)
  is not a `ps` to the hook.
* **Bash only.** PowerShell's `Get-Process`/`Stop-Process` pipeline is untouched;
  Q58 and Q59 own that frontend.

## Deliverables

- [x] `scripts/bash-workspace-guard.py` — `SHELL_C_CMDS`, `shell_c_group`,
      `kill_pipes` tracking, the ps-source contribution, the `guarded` clear.
- [x] Tests — unit (shell `-c` detection incl. the `--version` negative, the
      pipeline-flow rule) + e2e (the measured shapes, the background-child
      idiom, anchored pipelines, the substitution boundary, override, bypass).
- [x] `README.md` — decision-table rows, the "Kills fed by a pattern" subsection,
      How-it-works, Limitations.
- [x] `docs/design.md` — why the source is `ps` and not the filter.
- [x] `docs/STATUS.md` — drop Q60; queue the `sh -c` body analysis (Q61), the
      `kill -0` false positive (Q62), and the dead `MAX_SUBST_DEPTH` (Q63).
