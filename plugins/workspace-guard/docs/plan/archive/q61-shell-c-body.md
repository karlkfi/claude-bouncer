# Plan: analyze the body of a shell `-c` command (Q61)

**Goal:** close the hole [Q60](q60-ps-pid-source.md) named and left open — the body
of `sh -c '<body>'` is one token, so no read, write or kill inside it is checked.

**Approach:** re-analyze the body as its own command string, but only when this
host is what runs it. The gate is an allowlist of local wrappers, not a denylist
of container runtimes, and the body's `guarded` is discarded so the recursion can
only add friction.

## The gap

Measured with the hook at 8ebdc10, project root basename `wt-a`:

```
ask      cat /q61-fake-target
defer    sh -c 'cat /q61-fake-target'
defer    cat in.txt; sh -c 'echo x > /q61-fake-target'
defer    sh -c 'pkill -f ginkgo'
defer    bash -c 'kill $(pgrep -f ginkgo)'
```

Q60 stopped the last three coming back `allow`. It did not make them checked:
every one is a decision the hook would make confidently about the same string
written unwrapped, declined only because of where the string sits.

## Part 1 — the body is a command string, so analyze it as one

`_analyze_command` already recurses into command-substitution bodies. A shell
`-c` body gets the same treatment: offenders and `KillFacts` fold into the
enclosing string, `guarded` is dropped. Dropping `guarded` is what keeps Q60
intact — a body reading only workspace files still leaves the string deferring,
so the hook never vouches for a string on the strength of a construct it reads at
one remove.

Two details the substitution recursion doesn't need:

* **Extraction has to be strict.** `shell_c_group` scans every token and
  over-reports on purpose — for suppressing `allow`, a false hit costs a defer.
  A false *body* is fed to the tokenizer, and `bash run.sh | grep -c '^FAIL'`
  would hand it a grep pattern to find offenders in. So `shell_c_bodies` requires
  the `-c` to sit in the unbroken option run that follows the shell word.
* **The body resolves against its group's cwd**, not the string's, because a
  preceding `cd` has already run: `cd /etc && sh -c 'cat passwd'` reads
  `/etc/passwd`. Once cwd tracking is lost the body is skipped — a relative path
  would otherwise resolve against a stale directory and read as in-workspace,
  and a wrong clean answer is worse than no answer.

## Part 2 — a path only means something on the filesystem that owns it

`docker exec c sh -c 'cat /var/lib/…'` and `ssh h sh -c '…'` name paths that are
not this disk's. Judging them against this workspace blocks a file the command
never touches, which is why Q60 recorded a container-exec exclusion as this
item's prerequisite.

The exclusion is an **allowlist of local wrappers** — the group's command word
must be a shell or one of `timeout`, `env`, `xargs`, `find`, `nohup`, `nice`,
`ionice`, `stdbuf`, `setsid`, `time`. A denylist of container runtimes would have
to be right about every runtime that exists and is wrong by default about the
next one; an allowlist is wrong by default in the direction the hook can afford,
because an unrecognized wrapper leaves the body unanalyzed, which is where it
started. `sudo` stays off the list: `sudo docker exec …` is a shape the head of
the group cannot tell from `sudo sh -c …`.

### `in_subst`, not `subst_depth`

The ps-provenance rule counted a bare `ps` as a pid source "anywhere inside a
substitution body", testing `subst_depth > 0`. A shell `-c` body recurses through
the same counter but is not a substitution — running a command string does not
pipe its output anywhere — so the test becomes an explicit `in_subst` flag that
the substitution recursion sets and the body recursion inherits. Without the
split, `run & p=$!; kill $p; sh -c 'ps -p $p'` denies: exactly the background-child
idiom Q60 called load-bearing, resurrected one level down.

## Measured cost

Both parts, against every local transcript — 37,474 Bash commands:

| Change | Count |
|---|---|
| `defer` → `ask` | 3 |
| `defer` → `deny` | 1 |
| anything → `allow`, or `allow` → anything | **0** |

Four decisions moved, and **each is what the shipped hook already returns for
that same body written unwrapped** (two `$VAR` redirect targets it can't resolve,
one `$NOTES_TMP` in an `rm`, one `mktemp -d` landing in host temp). The change
does not invent friction; it removes an exemption `sh -c` was buying.

## Deliberate limitations

* **Container and remote bodies stay unparsed.** Under `docker`/`kubectl`/`ssh`/
  `sudo`, or any unrecognized wrapper. The Q60 suppression still applies, so the
  string defers rather than allowing.
* **A body under an untracked cwd is skipped**, for the reason in Part 1.
* **A `$VAR` in a body is substituted by the outer pass** if the hook tracked a
  literal for it, which is right for `sh -c "cat $f"` and over-eager for the
  single-quoted spelling bash would leave alone. Erring toward finding a path is
  the safe direction, and it matches how every other token is handled. An
  unresolved `$VAR` is `ask`ed about — the same answer the top level gives.
* **Bash only.** PowerShell has its own frontend and no `-c` equivalent in scope.

## Deliverables

- [x] `scripts/bash-workspace-guard.py` — `LOCAL_SHELL_WRAPPERS`,
      `shell_c_bodies`, the `in_subst` parameter, the body recursion.
- [x] Tests — unit (extraction, local wrappers, the remote-wrapper and
      `grep -c` negatives) + e2e (outside read/write, kill, nesting, the
      substitution boundary, group cwd, untracked cwd, the ps-consumer idiom).
- [x] `README.md` — decision-table rows, the `sh -c` bodies subsection,
      How-it-works step 15, agent guidance, Limitations.
- [x] `docs/design.md` — why the exclusion is an allowlist.
- [x] `docs/STATUS.md` — drop Q61.
