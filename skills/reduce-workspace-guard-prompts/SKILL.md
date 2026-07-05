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
project over the last 7 days. Useful adjustments:

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

Either way, map what you find to a cause. The report's category names line up
one-to-one with these:

1. **A `$VAR`, `$(...)`, or leading `~` in a guarded file argument** — category
   `expand`. The hook can't expand these, so it treats them as outside the root
   and prompts — even when they'd resolve in-root. Reason starts with
   "Runtime-expanded arg(s)".
2. **A `cd` outside the root, or bare `cd` / `cd -` / `cd $HOME`** — category
   `untracked`. These lose the hook's working-directory tracking, so every later
   relative path in the same command prompts. Reason starts with "Relative
   path(s) after an untracked cd".
3. **A path that genuinely resolves outside the root** (including `../`
   traversal, or temp files written to `/tmp`) — category `outside`. Reason
   starts with "Outside-workspace path(s)".

The **top offending paths** and **top triggering commands** rankings tell you
*which* files and commands to target first — fix the highest-count rows for the
biggest reduction.

## Fix

Tell the user the cause(s) you found, then apply the habits that prevent them:

- **Use the Read, Grep, and Glob tools instead of bash** `cat`/`grep`/`sed`/
  `head`/`tail`/`awk` for inspecting files. They don't go through this hook.
- **Keep guarded file arguments inside the project root** — write the literal
  in-root path (`cat ./config/app.json`), not a `$VAR`/`~`/`$(...)` form.
- **Stay in the project root** — don't `cd` outside it; avoid bare `cd`, `cd -`,
  and `cd $HOME`. `cd` into a subdirectory with a literal path if you must.
  (`cd "$(git rev-parse --show-toplevel)"` and `cd "$(pwd)"` are fine — the
  hook resolves these two substitutions itself; other `$(...)` targets still
  drop tracking.)
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
