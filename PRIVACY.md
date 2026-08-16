# Privacy

## The hook

`scripts/bash-pipe-guard.py` reads a JSON payload on stdin and writes a decision
on stdout. That is all it does.

It makes no network calls, writes no files, and keeps no state between
invocations. It reads:

- the Bash command string and `run_in_background` flag from the hook payload
- the payload's `cwd`, to locate an optional project registry
- `pipe-guard.json` shipped with the plugin
- `.claude/pipe-guard.json` under the project root, when it exists
- the `PIPE_GUARD_REGISTRY` and `CLAUDE_PROJECT_DIR` environment variables

Nothing about the command leaves the process except the deny reason, which
Claude Code shows to the model.

## The friction report

`scripts/friction-report.py` and the `/friction-report` command are read-only
analyzers over data Claude Code has **already** written to disk: your session
transcripts under `~/.claude/projects/**/*.jsonl`. They add no telemetry and
send nothing anywhere.

Those transcripts contain your commands and working directories. The report
prints excerpts of them to your terminal. If you paste that output somewhere,
you are pasting your own command history — check it first, and use `--repo` to
narrow the scope.
