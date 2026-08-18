---
name: reduce-foreground-guard-prompts
description: Explain why foreground-guard is prompting on Bash commands and how to stop the avoidable prompts. Use when the user asks "why am I getting so many foreground-guard prompts", "reduce foreground-guard prompts", "stop the watch/sleep/gh run watch permission prompts", or otherwise wants fewer confirmation prompts from this hook.
---

# Reducing foreground-guard prompts

foreground-guard is a `PreToolUse` hook for `Bash` that guards the session's
**main-thread time**. It prompts (`ask`, or `deny` in an unattended permission
mode — `auto`, `dontAsk`, `bypassPermissions` — and where config escalates) in
two classes and defers silently on everything else:

- **Class A — foreground poll/watch**: a command that parks the main thread on a
  live view — watch/follow modes (`gh run watch`, `gh pr checks --watch`,
  `kubectl logs -f`, `kubectl get -w`, `tail -f`, `journalctl -f`,
  `docker logs -f`, `watch ...`), a `while`/`until`/`for` loop that polls with
  `sleep`, a chained repeat-with-sleep (`cmd; sleep N; cmd`), or a bare `sleep N`
  at/above the floor (default 10s). `run_in_background: true` does **not** exempt
  Class A: a detached poll still holds a task slot for the whole wait and hands
  back output whose freshness the agent can't judge, so it still prompts.
- **Class B — slow command with an inadequate timeout**: a command the repo
  registered as slow that is about to run in the foreground with the Bash call's
  `timeout` below the registered minimum — it would be killed mid-run.
  `run_in_background: true` **is** the fix here, and exempts it.

So a flood of prompts almost always means the agent keeps waiting on a poll
instead of snapshotting and re-checking next turn, or keeps under-timing a
known-slow command — both fixable habits — not that the work genuinely needs to
block.

## Diagnose

Don't guess about past friction — measure it. The plugin ships an analyzer,
`scripts/friction-report.py`, that re-reads the hook decisions Claude Code
already recorded in the local session transcripts and ranks them by category,
flagged tool, and triggering command (no telemetry — see PRIVACY.md). Run it
first so the diagnosis is grounded in the user's real prompt history:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/friction-report.py" --repo "$(basename "$CLAUDE_PROJECT_DIR")"
```

This reports the prompt count and the share of the window's Bash calls it
represents (plus how many sessions saw a prompt), a **By category** breakdown,
**By flagged tool** and **Top flagged targets** rankings (the watch commands and
slow patterns being hit), and the **Top triggering commands**. A **Hook
failures** section appears only when a hook crashed or timed out; that is a guard
not running at all rather than friction, so report it as-is instead of tuning
config around it. Useful adjustments:

- `--since 24h` / `--since 2026-06-01` / `--since all` — widen or narrow the
  window (default `7d`).
- `--repo ''` — drop the project filter to see friction across every repo.
- `--plugin all` — include the sibling guards' decisions too (prod-guard,
  workspace-guard, branch-guard), if the user wants the whole picture. The
  override-downgrade count is foreground-guard's own, so it is omitted in this
  mode; read it from the default single-guard report instead. Denies are
  recovered from tool-result text, so a sibling whose reason does not open
  `<name>-guard:` under-counts them — don't read a low deny count for another
  guard as that guard rarely blocking.
- `--json` — machine-readable, if you'd rather parse it than read the table.

**Fall back gracefully.** If the script can't be found (`$CLAUDE_PLUGIN_ROOT`
unset — try the in-repo path `scripts/friction-report.py`), exits with "No
transcripts …", or prints "No … decisions found" (a fresh setup
with no recorded prompts yet), skip the data step and diagnose from the **most
recent foreground-guard prompts in this session** instead — each prompt's reason
text names the offending command and the fix. With neither, walk the user through
the category → fix map below against the commands they say keep prompting.

## Map categories to fixes

The report's category names are a stable contract; each maps to one fix. Tell the
user which categories dominate their report, then apply the matching fix:

1. **`watch`** (Class A) — a live watch/follow mode. **Reason:** "…runs in
   watch/follow mode…". Fix the behavior: take **one** non-blocking snapshot
   (`gh pr checks <pr>` without `--watch`, `gh run view <id>`, `tail -n 100`,
   `kubectl logs --tail=100`, `kubectl get` once) instead of streaming, and
   re-check next turn, or arm a Monitor that exits when the run reaches a
   terminal state. Re-running the same call with `run_in_background: true`
   does not quiet this category — the watch still occupies a task slot and the
   guard still prompts. If the flagged command is a **false positive** — a form
   you genuinely want to run live and don't want prompted — add a
   `poll.exempt_watch_patterns` regex (exemptions win over matches; this quiets
   just that form without disabling all of Class A). Conversely, if a real watch
   reached through an uncovered alias (`k logs -f …`) is *not* being caught but
   should be, that's a coverage gap → `poll.extra_watch_patterns`.
2. **`loop-sleep`** (Class A) — a `while`/`until`/`for` loop that polls with
   `sleep`. **Reason:** "…loop with `sleep` polls…". Fix the behavior: take one
   status check now and check again next turn — don't spin a poll loop, on the
   main thread or in the background (a backgrounded loop still prompts).
3. **`sandwich`** (Class A) — a chained repeat-with-sleep (`cmd; sleep N; cmd`).
   **Reason:** "…repeat-with-sleep chain…". Same fix as `loop-sleep`: one check
   now; defer the recheck to the next turn.
4. **`bare-sleep`** (Class A) — a long bare `sleep N` at/above the floor.
   **Reason:** "…parks the main thread for…" (backgrounded: "…parks a background
   task for…"). Skip the wait and do the follow-up check now (`sleep 300 && curl
   …` → just run the `curl` next turn). If the flagged sleeps are legitimately
   *short* startup-grace waits that sit just above the floor, raise
   `poll.sleep_floor_seconds` so they fall below it — but keep the floor low
   enough that real long waits still prompt.
5. **`slow-timeout`** (Class B) — a registered slow command about to be killed by
   an inadequate timeout. **Reason:** "…matches the slow-command pattern…" or
   "…matches the slow-command target…". Set an adequate `timeout:` on the Bash
   call (the reason names the minimum in ms), or run it with
   `run_in_background: true`. If a command is flagged slow but is *not* actually
   slow anymore, remove or lower its entry in `slow.commands`. If a **regex**
   entry with `.*` is firing on commands that merely *mention* the registered
   word in an argument, rewrite it in the target form —
   `"make": {"e2e*": 1800000}` — which matches whole argument words only.

The **Top flagged targets / commands** rankings tell you *which* commands to
target first — fix the highest-count rows for the biggest reduction. If the report
shows `FOREGROUND_GUARD_OVERRIDE downgrades`, an override is being leaned on
routinely; that command is a good candidate for a real fix (background it) rather
than a per-run override.

## Fix

Tell the user the cause(s) you found, then apply the habits that prevent them:

- **Don't wait on a poll at all — snapshot and re-check next turn.**
  Backgrounding a poll is not the fix: `run_in_background: true` only exempts
  Class B (a registered slow command), where it is exactly what the guard wants.
- **When the wait is unavoidable, arm a Monitor.** A Monitor whose script exits
  once the condition flips wakes the session with a dated event and holds no
  Bash task slot — the wait every Class A reason points at.
- **Take one snapshot, not a live stream.** `gh pr checks <pr>` (no `--watch`),
  `gh run view <id>`, `tail -n 100`, `kubectl logs --tail=100`, `kubectl get`
  once. Re-check on the next turn if you need fresher state.
- **Bound a deliberate wait with `timeout N …`.** A `timeout`-wrapped command is
  exempt from Class A (an explicit bound is the fix the guard teaches). Use it
  when you truly need to block briefly.
- **Set an adequate `timeout:` on known-slow Bash calls** so Class B stays quiet —
  or register the command in `slow.commands` so future under-timed runs get the
  reminder before they're killed.

Config lives in `.claude/foreground-guard.json` (per-repo) or
`~/.claude/foreground-guard.json` (user-level). The knobs that reduce prompts:

| Want to… | Knob |
| --- | --- |
| Quiet a specific built-in watch form that's a false positive | `poll.exempt_watch_patterns` (allowlist regexes over the command segment) |
| Stop short startup-grace sleeps from prompting | raise `poll.sleep_floor_seconds` (default 10) |
| Stop a slow command being flagged after it got fast | remove/lower its `slow.commands` entry |
| Downgrade a config `deny` back to a prompt | `poll.action: "ask"` |
| Add repo-specific context to Class A prompts | `hint` (e.g. name your own PR-watcher machinery) |

For a genuinely-intentional one-off foreground wait when the guard denies —
config escalated the class, or the session is in `auto` mode — prefix the
command with `FOREGROUND_GUARD_OVERRIDE=<why> …`. It downgrades **deny → ask**
(never a silent allow) and echoes the reason for the human reviewing it. In
`dontAsk` and `bypassPermissions` it is inert: no one is there to answer the
prompt it would buy, so take one of the three fixes instead.

**Don't suggest disabling the guard wholesale** (`poll.enabled: false`,
`slow.enabled: false`, or `FOREGROUND_GUARD_DISABLE=1`) to silence legitimate
prompts — a real foreground poll is friction worth keeping. Reach for those only
when the harness itself has subsumed the behavior.

## Make it stick

Offer to paste the playbook below into the user's `CLAUDE.md` (or `AGENTS.md`) so
future sessions follow these habits from the start — the guard can only attach
advice to a prompt, so habits that avoid the prompt entirely have to live in
project guidance. Only do so with the user's go-ahead.

```markdown
## Avoiding foreground-guard prompts

This repo uses foreground-guard, a hook that guards the session's main-thread
time. It prompts before a Bash call parks the main thread on a foreground wait or
runs a known-slow command that its timeout would kill. To keep work flowing:

- **Don't watch — snapshot.** Instead of streaming `gh run watch`, `gh pr checks
  --watch`, `kubectl logs -f`, `kubectl get -w`, `tail -f`, `journalctl -f`,
  `docker logs -f`, or `watch …`, take one non-blocking reading and check again
  next turn. `run_in_background: true` does not make a poll acceptable — it
  parks the same wait in a task slot and returns output you can't date.
- **Take one snapshot, not a live stream.** Prefer `gh pr checks <pr>` (no
  `--watch`), `gh run view <id>`, `tail -n 100`, `kubectl logs --tail=100`, and a
  single `kubectl get`. Re-check next turn if you need fresher state.
- **Don't poll with `sleep`.** Avoid `while true; do …; sleep N; done` loops and
  `cmd; sleep N; cmd` repeat-with-sleep chains. Take one status check now and
  defer the recheck to the next turn.
- **Don't block on a bare `sleep N`.** Do the follow-up check on the next turn
  instead. A short startup-grace `sleep` below the floor (default 10s) is fine.
- **Wait on a Monitor, not a Bash call.** When you truly need to be woken by a
  condition flipping elsewhere, arm a Monitor whose script exits at that point:
  the event arrives dated and no Bash task slot is held for the wait.
- **Bound a deliberate wait with `timeout N …`.** An explicit bound is exempt —
  and the Bash tool's own timeout still backstops it.
- **Set an adequate `timeout:` on known-slow Bash calls** (test suites, e2e runs,
  `-race` builds) so they aren't killed by the default 2-minute timeout — or run
  them with `run_in_background: true`.
```

The plugin also ships the **`/foreground-guard:friction-report`** slash command
for the "just show me the numbers" case — it runs the analyzer directly and prints
the ranked report with no diagnosis. It passes its arguments straight through, so
the same flags work:

```
/foreground-guard:friction-report                      # last 7 days
/foreground-guard:friction-report --since 24h --repo gateway
/foreground-guard:friction-report --json
```
