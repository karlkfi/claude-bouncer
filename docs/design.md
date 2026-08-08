# Design

The "why" behind workspace-guard. The [`README.md`](../README.md) covers *what* the plugin does; this doc covers *why this approach* and *why not the alternatives*. Read this before proposing a structural change to the parser, the `SPEC` table, or the decision semantics.

## Problem

Claude Code's built-in permission system matches commands as string patterns. `Bash(grep:*)` allows **every** invocation of `grep`. Users who pre-approve common file readers (`grep`, `cat`, `sed`, `head`, …) — the standard remedy for prompt fatigue — end up implicitly pre-approving `grep secret /etc/passwd` and `cat ~/.aws/credentials` along with the legitimate workspace reads.

The naive alternatives don't work:

- Leaving the rules empty and answering every prompt destroys agent throughput.
- Denying every outside-workspace read blocks legitimate cases (`cat /etc/os-release` for environment inspection, `man` pipelines, viewing system configs the user explicitly asked about).

The right granularity is **per file argument**, not per command. That's what this plugin adds.

## Approach

A `PreToolUse` hook on `Bash` that:

1. Tokenizes the command with `shlex` (a real POSIX lexer, not a regex).
2. Walks the tokens of each simple command against a static per-command spec (`SPEC` in [`../scripts/bash-workspace-guard.py`](../scripts/bash-workspace-guard.py)) that knows which positionals and flag-values are file arguments.
3. Resolves each file against `CLAUDE_PROJECT_DIR` with `realpath`.
4. Emits a decision:
   - All paths inside workspace → `allow`
   - Any path outside → `ask` (Claude Code prompts the user)
   - Command not in `SPEC`, or parsing fails → defer (no output, normal permissions apply)

Because `PreToolUse` hooks run before Claude Code's permission check, a user with `Bash(grep:*)` pre-approved still gets prompted when grep targets `/etc/passwd`.

## Why these specific design choices

### Why a hook, not deeper integration

Hooks are the only sanctioned extension point that sees structured tool input before the tool runs. A change to the permission-rule grammar in Claude Code itself would be the cleanest answer, but it requires an upstream change; this hook ships today and is per-user opt-in.

### Why a static `SPEC` table, not flag inference

The command being parsed is adversarial input. If the parser tries to *guess* whether an unknown flag takes a value, every guess is a potential bypass. The static `SPEC` table is the contract: a command is guarded **only** if we've explicitly written down which tokens are files. Adding coverage is a one-row change. Misclassification is impossible by construction for any command not in the table.

### Why `ask`, not `deny`

The hook is a guardrail, not a wall. False positives — legitimate reads of `/etc/os-release`, system configs, the user's `~/.zshrc` — are routine, and a `deny` default would erode trust until the user disabled the hook entirely. `ask` puts the human in the loop, which is the right cost for the rare outside-workspace read. Hard-blocking is available as a one-line local edit, documented in the README.

### Why defer on uncertainty

Unparseable commands, commands not in `SPEC`, empty input — all return no decision. Control hands back to Claude Code's normal permission rules, i.e. the same behavior as if the hook weren't installed. This is the only fail mode that doesn't surprise the user:

- Failing closed (deny on parse error) would block legitimate work and train users to disable the hook.
- Failing open with a silent `allow` would mask security regressions.
- Defer is net-neutral: the user is no worse off than without the hook.

This is the asymmetry behind the "secure by default" principle the plugin holds itself to: adding friction is cheap, removing it requires sign-off.

### Why only these seven commands

`grep`, `sed`, `awk`, `jq`, `cat`, `head`, `tail` are the high-frequency file readers users typically pre-approve. Pre-approval is what creates the gap this hook fills. Commands nobody pre-approves (`bash -c`, `eval`, `xargs`) still go through normal prompts and don't benefit from a hook layer on top.

The bar for adding a row to `SPEC`: **users pre-approve this command in permission settings, and it can read arbitrary files**. `cut`, `wc`, `xxd`, `od`, `strings` are reasonable candidates; `ls` is not (doesn't read file contents); `bash` is not (different threat model — see [`security-notes.md`](security-notes.md)).

### Why the hook extends beyond Bash to the native file tools

The filesystem boundary is really "don't touch files outside the workspace," and the native tools (`Read`/`Grep`/`Glob`, `Edit`/`Write`/`MultiEdit`/`NotebookEdit`) bypass Bash entirely. The first expansion targeted the narrowest hazard: a **write into a sibling checkout of the same repo** when the session runs in a git worktree — the edit silently lands on the wrong branch — so the write tools got a `PreToolUse` hook whose headline rule is the sibling-checkout deny. Since 1.5.0 coverage is symmetric with the Bash guard: the write tools get the full outside/host-temp/sibling classification of a bash write, and `Read`/`Grep`/`Glob` get the same check as a bash `cat` of the same path (`ask` on an outside path, with the same read exemptions). In-workspace targets defer to the builtin permission system.

Detection lives in the same script the Bash hook uses (dispatched on `tool_name`), not a separate plugin: a standalone "worktree-guard" would duplicate the root detection, path resolution, and cwd tracking this plugin already has, and both hooks would fire on the same Bash calls with competing messages. `branch-guard` (which already hooks `Edit`/`Write`) was considered too, but its axis is "protected branch," and the hazard here is "wrong checkout" — it exists even when the sibling has a feature branch checked out. The worktree-aware filesystem boundary is this plugin's mission.

The deny (rather than `ask`) is the secure default here specifically because the failure mode is an approvable-by-reflex prompt whose only correct answer was "reject and retype the path"; a deny self-heals in one agent round trip. `WORKSPACE_GUARD_OVERRIDE=<reason>` is the documented, reasoned escape hatch for deliberate cross-checkout work.

### Why a process kill is in scope, though it touches no file

`pkill -f` — and `Stop-Process` on the PowerShell side — reaches another session's work without naming a path. It signals by *pattern*, matched against the whole command line, so `pkill -f "make check"` hits every checkout on the host running one — the same wrong-branch mistake as a sibling-checkout write, addressed the one way a path check cannot see. It sits in this plugin rather than a new one for the same reason the write tools do: the workspace root, the tokenizer, and the override are already here.

The rule is an *anchor*, not a pattern allowlist: some operand must contain the project root's directory name as a whole path component with a separator on at least one side. That is a mechanical test with no judgment in it — a bare word is a substring match against a command line, and the hook has no basis for deciding whether `api` excludes a sibling. Both misparse directions of the flag table land on `deny`, so lagging an implementation's options costs friction, never a hole.

`deny` rather than `ask` here is measured, not assumed: of 38 `pkill` targets observed across one developer's session transcripts, 36 carried nothing identifying the worktree that started them. An `ask` on 36 of 38 kills trains the reflexive approval it exists to prevent. Unlike the file tiers, an *anchored* kill defers rather than emitting `allow` — this hook has nothing more to say about it, and an `allow` would strip the user's own permission settings from a destructive command.

PowerShell's `Stop-Process` is the same hazard behind a different grammar, and covering it changed one thing about the rule: the scope is the whole *statement*, not one command. The anchored rewrite there is `Get-Process | Where-Object { $_.Path -like '<root>\*' } | Stop-Process`, where the anchor sits two pipeline segments upstream of the kill, so a segment-local scan would deny the very rewrite the message recommends. Covering the pipeline form at all is not optional: guard only `-Name` and the deny teaches the agent to reach for `Get-Process node | Stop-Process` — the identical host-wide kill, one step further from the check.

Windows' `taskkill` then pushed the scope back the other way, which is the part worth recording. It reaches both frontends, so it is checked in both — but it is judged on its own arguments, not on its statement, because it reads no pipeline: its selection is entirely in its own flags. Inheriting `Stop-Process`' statement scope would have accepted `Get-Content <root>\list.txt | taskkill /IM node.exe` as anchored while it killed every checkout's `node.exe`. The scope of an anchor is therefore a property of *how the command selects its targets*, not of the shell it was typed into — one rule, two scopes, decided per command.

Bash has that same shape, one layer down: a rule that keys on the kill *command* is dodged by keying on the pid instead: `pgrep -f ginkgo | xargs kill` and `ps … | grep ginkgo | awk '{print $1}' | xargs kill` kill exactly what `pkill -f ginkgo` kills. The second shape was worse than a miss — `grep` and `awk` are clean guarded commands, so the hook emitted its blanket `allow` for the whole string and green-lit the kill. That is the general lesson: **`allow` speaks for the entire command string**, so any tier that answers `allow` on the strength of one command has to be suppressed by anything destructive elsewhere in the string. Signalling now clears the flag in the bash frontend regardless of whether the kill itself offends, and the pattern that produced the pids is run through the same anchor test the kill's own pattern gets. Both frontends clear it now: a `Stop-Process` or a `taskkill` suppresses the PowerShell `allow` on the same terms, including the kills the anchor rule cleared. That last part is the point rather than an edge case — a kill by literal pid or behind a `Where-Object` filter earns no offender, which is precisely what left it there for a clean `Get-Content` to speak for.

The narrow part is deciding which kills that applies to. A `kill` whose operands are all literal pids or job specs is *proof* of no pattern provenance, which is what lets the recommended rewrite — inspect with `pgrep -fl`, then kill the pid you meant — stay clear of the rule. Everything else is co-occurrence within one command string rather than real dataflow; tracking pids through a pipeline for real would buy precision the literal-pid rule already delivers.

Where that rule first put the pid source turned out to be wrong, and the correction generalizes. It read the *pattern* — `pgrep`'s operands, or a `grep`'s when a `ps` shared the pipeline — which made the grep load-bearing and every other filter a hole: swap in `awk '/ginkgo/ {print $1}'` and nothing was collected. The instinct is to widen the filter table, which loses, because the next filter is always `sed`, then `cut`, then `perl`, and a pipeline with *no* filter (`ps -eo pid= | xargs kill`) has nothing to widen to. The source was never the filter. It is `ps`, which produces the pids; the filter only decides which of its rows survive. Making `ps` the source and its selection *unreadable* catches every spelling at once, and reduces the grep's role to what it actually is — the one stage the hook can read well enough to let it **anchor** the pipeline. The lesson is that a rule keyed on the readable thing near the hazard will keep needing extensions; keying it on the thing that produces the hazard ends the sequence.

Reading an `awk` program instead would have been unsafe rather than merely imprecise, which is why the fix is a stand-in that never anchors rather than a wider pattern reader: an inverting program (`awk '!/<root>/ {print $1}'`) scans as anchored while killing every *other* checkout — the trap the `grep -v` rule already exists to close. The cost is a false deny on an anchored `awk`, paid deliberately.

The weaker signal needed the stronger provenance, though. A grep pattern is readable, so an unanchored one is evidence of intent wherever in the string it sits; a bare `ps` says nothing until it is wired to a kill, so it counts only where its pids can reach one — the same pipeline, or a substitution the kill consumes. Without that, the rule denies `run & p=$!; kill $p; ps -p $p`, where the `ps` *consumes* a pid the shell already knows. Two commands in the measured corpus have exactly that shape, and a guard that blocks the ordinary way to check whether your own child died is a guard people turn off.

`sh -c '<body>'` is the same `allow`-speaks-for-everything problem in its purest form: the body is one opaque token, so the hook knows nothing about it — and knowing nothing is precisely the case where vouching is indefensible. Measured, `cat in.txt; sh -c 'cat /outside'` returned `allow`. It now defers.

Reading the body came next, and the thing standing in the way was never the parsing — a body is an ordinary command string, and feeding it back through the same rules is four lines. It was that a path only means something if you know whose filesystem it names. `docker exec c sh -c 'cat /var/lib/…'` and `ssh h sh -c '…'` name paths that are not this disk's, so judging them against this workspace produces a block on a file the command never touches. The exclusion could have been written either way round, and which way round is the whole decision: a denylist of container runtimes has to be right about every runtime that exists, and is wrong by default about the next one. So the hook names the **local** wrappers instead — `timeout`, `env`, `xargs`, `find`, and the handful of others that really do exec a shell here — and everything unrecognized reads as remote and is left alone. `sudo` is unlisted for the same reason, since `sudo docker exec …` is a shape the head of the group cannot distinguish. Being wrong about a local wrapper costs a missed catch, which is where the hook already was; being wrong about a remote one costs a block on a path that does not exist.

What the recursion is *not* allowed to do is upgrade a decision. A body's offenders fold in and its `guarded` is discarded, exactly as a command substitution's is, so a body reading nothing but workspace files still leaves the string deferring. The suppression above and the analysis here answer different questions — "may the hook vouch for this string?" and "is there something in it to catch?" — and only the second one got a new answer. The measurement bears out that this is friction the guard was already charging elsewhere: across 37,474 corpus commands the change moved four decisions, and each of the four is precisely what the shipped hook already returns for that same body written unwrapped. `sh -c` was buying an exemption, and nothing else.

## Alternatives considered and rejected

- **Sandboxing (seccomp, App Sandbox, bind mounts, chroot).** Too heavyweight; platform-specific; breaks legitimate cross-workspace reads. A user who wants this level of isolation should run the whole agent in a container, not bolt on a partial sandbox.
- **Wrapping the agent's shell.** Invasive; introduces a forked environment that drifts from the real one; easily bypassed by invoking the binary at its real absolute path.
- **Per-invocation permission rules upstream in Claude Code.** Probably the right long-term answer. This hook is the bridge while that doesn't exist.
- **Denying outside-workspace reads outright.** Too noisy; erodes trust; the agent and user will route around it (file a workaround as a Queue item, set a wider PROJECT_DIR, uninstall the hook).
- **A dynamic learning parser.** Inferring flag semantics from observed commands would let coverage grow without code changes, but the misclassification surface — and the bypass surface — grows with it. The static table is boring on purpose.

## Non-goals

- **Defending against an attacker with arbitrary shell execution.** The agent can write a script and `bash` it; the hook does not try to model every wrapper command. The "wrapper commands" section of [`security-notes.md`](security-notes.md) covers this.
- **Sandboxing the workspace from itself.** Workspace-local reads are explicitly allowed, including reads of sensitive files (`.env`, `.git/config`) inside the project. Protecting those is the user's choice via other means.
- **Replacing Claude Code's permission system.** The hook augments it for one specific gap. If the gap closes upstream, this plugin retires.
- **Modeling every shell construct.** Variable expansion, command substitution, `cd` semantics, etc. are out of scope for the lexer but covered as Queue items where the gap has security impact (`STATUS.md` Q5–Q8). Constructs without security impact stay unmodeled.

## Open questions

These are intentionally unresolved; if you have a strong opinion, propose a Queue item:

- **Should the spec be data, not code?** The `SPEC` dict could move to a JSON/TOML file shipped with the plugin, making contributions less Python-centric. Today it's code because it's tiny and stdlib-only matters more than ergonomics.
- **Should denials be configurable per command?** Some users may want `ask` for `cat` but `deny` for `jq --rawfile`. Today it's a global one-line edit.
- **Should the hook log decisions?** A local audit log would help debug false positives and false negatives, but it's also a new surface (write target, PII).
