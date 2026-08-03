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
   `expand`. The hook can't expand these, so it treats them as outside the root
   and prompts — even when they'd resolve in-root. Reason starts with
   "Runtime-expanded arg(s)".
2. **A bare `cd` / `cd -` / `cd $HOME`, a `popd`, or an unrecognized
   `$(...)` `cd` target** — category `untracked`. These lose the hook's
   working-directory tracking, so every later relative path in the same
   command prompts. Reason starts with "Relative path(s) after an untracked
   cd". (A *literal* `cd` target — even one outside the root — keeps
   tracking; relative paths after it land in category `outside` instead,
   with the resolved absolute path named in the reason.)
3. **A path that genuinely resolves outside the root** (including `../`
   traversal, or temp files written to `/tmp`) — category `outside`. Reason
   starts with "Outside-workspace path(s)".

The **top offending paths** and **top triggering commands** rankings tell you
*which* files and commands to target first — fix the highest-count rows for the
biggest reduction.

## Fix

Tell the user the cause(s) you found, then apply the habits that prevent them:

- **Use the Read, Grep, and Glob tools instead of bash** `cat`/`grep`/`sed`/
  `head`/`tail`/`awk` for inspecting files. Their literal single-path inputs
  can't trigger the `expand`/`untracked`/heredoc false positives. They are
  still guarded (since 1.5.0): a genuinely outside-workspace read prompts
  either way — that prompt is the boundary working as intended, so approve
  it rather than working around it.
- **Keep guarded file arguments inside the project root** — write the literal
  in-root path (`cat ./config/app.json`), not a `$VAR`/`~`/`$(...)` form. (A
  variable assigned a literal earlier in the same command is resolved, as is a
  `for` loop over a literal list, over an in-root glob (`for f in docs/*.md`),
  or over a nested list built from the outer variable (`for d in docs/*; do
  for f in "$d"/*.md`). Where the body sits — on the same line as `do` or the
  next — makes no difference. A `$(...)` list such as `for f in $(ls docs)`
  is not resolved and does prompt.)
- **Give `cd` a literal target, and stay in the project root** — avoid bare
  `cd`, `cd -`, `cd $HOME`, and `popd`; `cd` into a subdirectory with a
  literal path if you must.
  (`cd "$(git rev-parse --show-toplevel)"` and `cd "$(pwd)"` are fine — the
  hook resolves these two substitutions itself; other `$(...)` targets still
  drop tracking.) A literal `cd` outside the root keeps tracking, but every
  relative path after it is then genuinely outside the workspace and prompts
  deliberately.
- **Write temp files inside the root** (`./.tmp/out.txt`), not `/tmp`. Redirects
  to `/dev/null`, `/dev/stdout`, `/dev/stderr`, and `/dev/fd/N` are exempt.

## Make it stick

Offer to paste the **"Avoiding workspace-guard permission prompts"** playbook
from the project README's *Agent guidance* section into the user's `CLAUDE.md`
(or `AGENTS.md`) so future sessions follow these habits from the start. Only do
so with the user's go-ahead.

If a recurring command genuinely needs a file outside the root, that prompt is
working as intended — the user should approve it (or, for full-auto runs, see
the README's Configuration section). Don't suggest weakening the hook's default
to silence a legitimate boundary crossing.
