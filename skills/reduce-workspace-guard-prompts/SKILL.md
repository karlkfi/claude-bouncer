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

Look at the recent prompts (the hook's reason names the offending path and the
fix). Map each to a cause:

1. **A `$VAR`, `$(...)`, or leading `~` in a guarded file argument.** The hook
   can't expand these, so it treats them as outside the root and prompts — even
   when they'd resolve in-root. Reason starts with "Runtime-expanded arg(s)".
2. **A `cd` outside the root, or bare `cd` / `cd -` / `cd $HOME`.** These lose
   the hook's working-directory tracking, so every later relative path in the
   same command prompts. Reason starts with "Relative path(s) after an untracked
   cd".
3. **A path that genuinely resolves outside the root** (including `../`
   traversal, or temp files written to `/tmp`). Reason starts with
   "Outside-workspace path(s)".

## Fix

Tell the user the cause(s) you found, then apply the habits that prevent them:

- **Use the Read, Grep, and Glob tools instead of bash** `cat`/`grep`/`sed`/
  `head`/`tail`/`awk` for inspecting files. They don't go through this hook.
- **Keep guarded file arguments inside the project root** — write the literal
  in-root path (`cat ./config/app.json`), not a `$VAR`/`~`/`$(...)` form.
- **Stay in the project root** — don't `cd` outside it; avoid bare `cd`, `cd -`,
  and `cd $HOME`. `cd` into a subdirectory with a literal path if you must.
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
