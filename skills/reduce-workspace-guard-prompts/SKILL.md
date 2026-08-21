---
name: reduce-workspace-guard-prompts
description: Explain why workspace-guard is prompting on Bash file commands and how to stop the avoidable prompts. Use when the user asks "why am I getting so many permission prompts", "reduce workspace-guard prompts", "stop the grep/cat permission prompts", or otherwise wants fewer confirmation prompts from this hook.
---

# Reducing workspace-guard prompts

workspace-guard is a `PreToolUse` hook that prompts before a guarded bash file
command (`grep`, `sed`, `awk`, `jq`, `cat`, `head`, `tail`, `cp`, `mv`, `rm`,
`tee`, `dd`, and friends) reads or writes a path **outside the project root**
(`$CLAUDE_PROJECT_DIR`). In-root reads and pure pipelines run silently. So a
flood of prompts means commands keep resolving paths outside the root — usually
for one of the avoidable reasons below, not because the work genuinely needs
outside files.

## Diagnose

Don't guess about past friction — measure it. The plugin ships an analyzer,
`scripts/friction-report.py`, that re-reads the hook decisions Claude Code
already recorded in the local session transcripts and ranks them by category,
offending path, and triggering command. Run it first so the diagnosis is
grounded in the user's real prompt history:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/friction-report.py" --repo "$(basename "$CLAUDE_PROJECT_DIR")"
```

This reports the friction ratio (ask+deny share), a **By category** breakdown,
the **top offending paths**, and the **top triggering commands** for the current
project over the last 7 days. The ratio's denominator counts only decisions the
hook emitted — a silent defer on an unguarded command leaves no record — so it
is a share of the hook's own decisions, not of every Bash call. Useful
adjustments:

- `--since 24h` / `--since 2026-06-01` / `--since all` — widen or narrow the
  window (default `7d`).
- `--repo ''` — drop the project filter to see friction across every repo.
- `--raw` — show exact path tokens instead of collapsing per-session temp paths.
- `--json` — machine-readable, if you'd rather parse it than read the table.

**Fall back gracefully.** If the script can't be found (`$CLAUDE_PLUGIN_ROOT`
unset — try the in-repo path `scripts/friction-report.py`), exits with "No
transcripts …", or prints "No guard decisions found" (a fresh setup with no
recorded prompts yet), skip the data step and diagnose from the **most recent
prompts in this session** instead — the hook's reason text names the offending
path and the fix for each.

An empty report exiting **2** means a filter matched nothing that exists — the
lines below "No guard decisions found" name which one and, for `--plugin`, list
the guard labels the transcripts actually contain. Fix the flag and re-run
rather than treating it as zero friction.

Either way, map what you find to a cause. The report's category names line up
one-to-one with these:

1. **A `$VAR`, `$(...)`, or leading `~` in a guarded file argument** — category
   `expand`. The hook can't expand these, so it can't tell where the command
   lands — and neither could you at a prompt. It **denies** with the
   literal-path rewrite instead of asking, so this class costs the agent a
   retry rather than costing you a decision. Reason carries
   "Runtime-expanded arg(s)".
2. **A bare `cd` / `cd -` / `cd $HOME`, a `popd`, or an unrecognized
   `$(...)` `cd` target** — category `untracked`. These lose the hook's
   working-directory tracking, so every later relative path in the same
   command is **denied**, with the literal-target rewrite in the reason, for
   the same reason `expand` is. Reason carries "Relative path(s) after an
   untracked cd". (A *literal* `cd` target — even one outside the root — keeps
   tracking; relative paths after it land in category `outside` instead,
   with the resolved absolute path named in the reason.)
3. **A path that genuinely resolves outside the root** (including `../`
   traversal, or temp files written to `/tmp`) — category `outside`. This is
   the one that still **prompts**: only you can say whether the file is yours
   to read. Reason carries "Outside-workspace path(s)".

Every blocking reason leads with `workspace-guard: ` — a **prompt** and a
**deny** alike — and the category name follows it. So match the category name
anywhere in the reason rather than at the start.

The **top offending paths** and **top triggering commands** rankings tell you
*which* files and commands to target first — fix the highest-count rows for the
biggest reduction.

## Fix

Tell the user the cause(s) you found, then walk them through the habits that
prevent them. Those habits are specified **once**, in the README's *Agent
guidance* section — read the playbook there and work from it rather than
reciting from memory:

```
${CLAUDE_PLUGIN_ROOT}/README.md
```

`~/.claude/plugins/` is exempt from the workspace check for reads, so opening
that file costs no prompt. (In a checkout of the plugin itself it's just
`README.md`. If neither resolves — `$CLAUDE_PLUGIN_ROOT` unset and no local
copy — give the causes and their one-line fixes from the Diagnose section
above, and tell the user the specifics are unverified.)

Which bullets to walk through, by category:

- `expand` — the bullets on `$VAR`/`$(...)`/`~` in a guarded file argument, and
  on preferring the Read, Grep, and Glob tools to bash `cat`/`grep`/`sed`.
- `untracked` — the bullet on giving `cd` a literal target.
- `outside` — the bullets on temp files, on out-of-tree dependency caches, and
  on editing through this session's own worktree checkout.

Quote those bullets rather than summarizing them. They carry exact paths,
preconditions, and exemption lists that a paraphrase drops — which is how this
file's own copy of the temp-file advice drifted into naming a scratch dir that
matched neither the README nor the hook's deny message (issue 160).

## Make it stick

Offer to paste that same **"Avoiding workspace-guard permission prompts"**
playbook into the user's `CLAUDE.md` (or `AGENTS.md`) so future sessions follow
these habits from the start. Only do so with the user's go-ahead.

If a recurring command genuinely needs a file outside the root, that prompt is
working as intended — the user should approve it (or, for full-auto runs, see
the README's Configuration section). Don't suggest weakening the hook's default
to silence a legitimate boundary crossing.
