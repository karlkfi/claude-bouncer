# foreground-guard

**Main-thread time guard rails for Claude Code Bash commands.**

[![tests](https://img.shields.io/github/actions/workflow/status/karlkfi/claude-foreground-guard/tests.yml?branch=main&label=tests)](https://github.com/karlkfi/claude-foreground-guard/actions/workflows/tests.yml) [![License: MIT](https://img.shields.io/github/license/karlkfi/claude-foreground-guard.svg)](LICENSE) [![Claude Code plugin](https://img.shields.io/badge/Claude_Code-plugin-7e57c2)](#install)

> The main thread is the one you're talking to. Don't let it sit and watch
> paint dry — and don't let a 10-minute test run get killed at minute 2.

Agents foreground-poll instead of backgrounding. In one repo's two-week
transcript sample, **~130 Bash calls were blocking polls** — `gh pr checks
--watch`, `gh run watch`, `tail -f`, `sleep`-loops — each one parking the
session's main thread until something killed it. In the same sample, **36
slow runs** (envtest suites, e2e runs, `-race` builds) were killed by the
Bash tool's **default 2-minute timeout**, wasting the entire run each time.
Repos carry prose rules in CLAUDE.md to prevent this ("never watch in the
foreground", "always set timeout on make test-race") — and agents still do
it. A hook enforces the rule mechanically and teaches the fix in the denial
message at the exact moment of violation.

foreground-guard is a `PreToolUse` hook for `Bash` that catches two classes
of main-thread time-wasters:

- **Class A — foreground poll/watch** (`ask` by default; config may escalate
  to `deny`): watch/follow modes (`gh run watch`, `kubectl logs -f`,
  `tail -f`, `watch ...`), shell loops that poll with `sleep`, chained
  repeat-with-sleep sequences, and bare `sleep N` waits at or above a
  configurable floor.
- **Class B — slow command with an inadequate timeout** (`ask` by default;
  config may escalate to `deny`): a command the repo has registered as
  needing more than the Bash call's timeout — about to be killed mid-run.
  The registry ships **empty**; slow-command knowledge is per-repo config.

Everything else passes through silently, so your normal permissions apply.

## Contents

- [What it does](#what-it-does)
- [Install](#install)
- [Keeping it updated](#keeping-it-updated)
- [Covered forms](#covered-forms)
- [Exemptions](#exemptions)
- [Configuration](#configuration)
- [Friction report](#friction-report)
- [Unattended permission modes](#unattended-permission-modes)
- [The override escape hatch](#the-override-escape-hatch)
- [Soundness: never `allow`](#soundness-never-allow)
- [Limitations](#limitations)
- [Companion plugins](#companion-plugins)
- [Privacy](#privacy)
- [License](#license)

## What it does

The hook produces one of three outcomes per Bash call:

- **ask** — Claude Code shows its standard permission prompt, with a reason
  that teaches the three fixes: take ONE non-blocking snapshot (`gh pr
  checks <n>` without `--watch`, `tail -n 100` instead of `tail -f`), re-run
  the same call with `run_in_background: true`, or bound the wait explicitly
  with `timeout N ...`. On a call that is already backgrounded the Class A
  reason drops that middle fix and names the other two. Class B asks name the
  exact minimum: "set `timeout: 600000` on this Bash call, or run it in the
  background."
- **deny** — in an [unattended permission mode](#unattended-permission-modes)
  (`auto`, `dontAsk`, `bypassPermissions`), or when config escalates a class
  to `deny`. Blocks identically to an ask, but the reason is fed back to the
  agent, so it self-corrects instead of handing you a prompt to answer.
  Downgradable to `ask` with a `FOREGROUND_GUARD_OVERRIDE=<reason>` prefix.
- **defer** — the hook stays silent; your normal permission settings apply.
  foreground-guard never emits `allow` (see
  [Soundness](#soundness-never-allow)).

The table below shows the attended-mode decision; every **ask** in it is a
**deny** in an unattended mode.

| Command | Decision |
| --- | --- |
| `gh pr checks 123` | defer |
| `gh pr checks 123 --watch` | **ask** |
| `gh run watch 456` | **ask** |
| `gh run watch 456` with `run_in_background: true` | **ask** (still a poll — see [Exemptions](#exemptions)) |
| `gh run watch 456 &` | defer (detached) |
| `timeout 30 gh run watch 456` | defer (explicitly bounded) |
| `kubectl logs -f pod/api` | **ask** |
| `kubectl get pods -w` | **ask** |
| `kubectl get pods -o wide` | defer |
| `tail -f app.log` | **ask** |
| `tail -n 50 app.log` | defer |
| `grep -f patterns.txt src/` | defer |
| `git log --follow -- README.md` | defer |
| `journalctl -f`, `docker logs -f c1`, `watch kubectl get pods` | **ask** |
| `while true; do gh pr checks 1; sleep 5; done` | **ask** (poll loop) |
| `gh pr checks 1; sleep 5; gh pr checks 1` | **ask** (repeat-with-sleep) |
| `sleep 300` | **ask** (≥ floor, default 10 s) |
| `sleep 2 && curl localhost:8080/health` | defer (below floor) |
| `./server > log 2>&1 & sleep 2; curl localhost` | defer (server detached, grace sleep short) |
| `sleep 30 & make build` | defer (sleep backgrounded) |
| `bash -c 'while true; do sleep 5; done'` | **ask** (recursed) |
| `bash poll-forever.sh` | defer (script files stay opaque) |
| `cat <<EOF` … `tail -f x` … `EOF` | defer (heredoc body is data) |
| `make test-race` (configured min 600000 ms, default timeout) | **ask** (names the minimum) |
| `make test-race` with `timeout: 600000` | defer |
| `make test-race` with `run_in_background: true` | defer |

## Install

Install on any Claude Code surface that runs plugin `PreToolUse` hooks — the
CLI, the IDE extensions, or **Claude Code for Claude Desktop**.

**Claude Code (CLI or IDE extension)** — run the slash commands:

```
/plugin marketplace add karlkfi/claude-foreground-guard
/plugin install foreground-guard@foreground-guard
```

**Claude Code for Claude Desktop** — use the **Customize** tab:

1. Open the **Customize** tab and go to its plugins / marketplaces section.
2. Add `karlkfi/claude-foreground-guard` as a marketplace.
3. Find **foreground-guard** in that marketplace, install it, and enable it.

After installing with either method:

- Requires `python3` on your PATH.
- Restart Claude Code (or `/reload-plugins`) so the hook is registered.
- **Turn on auto-update now.** A GitHub marketplace pins the version you
  installed and never refreshes on its own (see
  [Keeping it updated](#keeping-it-updated)). Add this to
  `~/.claude/settings.json` at install time, while you're thinking about it:
  ```json
  {
    "extraKnownMarketplaces": {
      "foreground-guard": {
        "source": { "source": "git", "url": "https://github.com/karlkfi/claude-foreground-guard.git" },
        "autoUpdate": true
      }
    }
  }
  ```
- **Register your repo's slow commands** — the Class B registry ships empty;
  it only helps once your repo's `.claude/foreground-guard.json` names the
  commands that outlive the default timeout. See
  [Configuration](#configuration).

To verify, ask Claude to run `gh run watch 123` — it should prompt with a
foreground-guard reason. `tail -n 50 some.log` should run without any
foreground-guard output.

## Keeping it updated

Claude Code auto-updates **official Anthropic marketplaces only**.
foreground-guard installs from a third-party GitHub marketplace, and those
**never refresh on their own** — the version you installed stays pinned
until you either enable auto-update or update it by hand.

**Recommended — set and forget.** Add `autoUpdate` for the marketplace in
`~/.claude/settings.json` and Claude Code refreshes it like an official one:

```json
{
  "extraKnownMarketplaces": {
    "foreground-guard": {
      "source": { "source": "git", "url": "https://github.com/karlkfi/claude-foreground-guard.git" },
      "autoUpdate": true
    }
  }
}
```

**Manual.** Update the marketplace clone, then the installed plugin, then
restart to apply:

```
claude plugin marketplace update foreground-guard
claude plugin update foreground-guard@foreground-guard
```

## Covered forms

**Class A — watch/follow registry** (built-in; extensible via config):

| Form | Snapshot alternative taught |
| --- | --- |
| `gh pr checks ... --watch` | `gh pr checks <pr>` once, without `--watch` |
| `gh run watch <id>` | `gh run view <run-id>` once |
| `kubectl`/`oc` `logs -f` / `--follow` | `kubectl logs --tail=100` |
| `kubectl`/`oc` `get -w` / `--watch` / `--watch-only` | `kubectl get` once |
| `tail -f` / `-F` / `--follow` (incl. combined `-fn50`) | `tail -n 100` |
| `journalctl -f` / `--follow` | `journalctl -n 100` |
| `docker`/`podman`/`nerdctl` `logs -f` / `--follow` | `--tail 100` |
| `watch <cmd>` | run the wrapped command once |

**Class A — loop and sleep forms:**

- `while`/`until`/`for` loops whose body runs `sleep` (any duration — the
  loop multiplies it).
- Chained repeat-with-sleep: `cmd; sleep N; cmd; ...` — a sleep sandwiched
  between commands is a poll regardless of `N`.
- Bare `sleep N` as a foreground segment with `N ≥` the floor (default
  10 s). `sleep $VAR` counts as long (unknown durations lean toward a
  prompt). Below-floor sleeps — startup grace like `sleep 2 && curl ...` —
  pass.

Matching is anchored to the tool name, so `grep -f patterns.txt`,
`git log --follow`, and `-f` flags on unrelated tools never match. The hook
recurses into quoted `bash -c '...'` and `eval ...` bodies (bounded), but
`bash some-script.sh` stays opaque — no script-file inspection. A repo that
hits a false-positive on a specific built-in watch form can quiet just that
one with a `poll.exempt_watch_patterns` allowlist entry (exemptions win over
matches) instead of turning off all of Class A — see
[Configuration](#configuration).

**Class B — slow-command registry** (config-only, ships empty): entries
mapped to a minimum timeout in ms. When a matched command would run in the
foreground with the Bash call's `timeout` below the minimum (or unset — the
2-minute default), the guard prompts (`ask` by default; `slow.action` may
escalate to `deny`) and names the exact fix. Two registration forms:

**Target form** — `"<command>": {"<target glob>": ms}` — for the common
"this command with this argument" case (`make e2e`, `go test -race`), where
the hook does the anchoring itself: the command word must equal `<command>`
(compared by basename, so `/usr/bin/make` counts), and the glob must match a
**whole argument word**. `{"make": {"e2e*": 1800000}}` fires on `make e2e`,
`make -C sub e2e-test`, and `git pull && make e2e`, but never on
`make -n help NOTE="a note mentioning e2e"` — a quoted argument is one word,
and `e2e*` doesn't match it from the start. This is the recommended shape:
no regex to get wrong.

**Regex form** — `"<regex>": ms` — matches at a **command position**: the
command is split into simple commands the same way Class A splits it
(heredoc bodies stripped, env prefixes and `nohup`/`time`/`timeout` wrappers
peeled, `bash -c '...'` and `eval` bodies recursed into, `bash gate.sh`
reduced to the script), and the pattern has to match starting inside a
segment's command word. So registering a bare path —
`"scripts/gate\\.sh": 3600000` — fires on `scripts/gate.sh`,
`./scripts/gate.sh --all`, `CI=1 nohup scripts/gate.sh`, and
`git fetch && bash scripts/gate.sh`, but not on `grep -n foo scripts/gate.sh`,
`wc -l scripts/gate.sh`, or a `git commit -m` whose message quotes the path.
To register an **argument** instead of a command, prefix the pattern with
`.*` (`".*-race\\b"`) — that opts back into matching anywhere in a segment,
still not across the whole command line. **A `.*` in a regex reaches into
quoted arguments**: `"make .*\\be2e"` fires on any `make` invocation whose
arguments mention e2e anywhere, including inside a quoted string. When the
thing being registered is a command plus an argument word, use the target
form instead.

## Exemptions

These pass untouched, by design:

- **`run_in_background: true`** on the Bash call — **Class B only.**
  Backgrounding is the fix Class B teaches, so a registered slow command
  passes. It does not fix a poll: a detached `gh run watch` or `sleep`-loop
  moves the wait off the main thread without removing it — it holds a task
  slot for the whole run and hands back output whose freshness the agent
  can't judge. Class A still prompts on a backgrounded call, with reasons
  worded for a detached wait (they teach the snapshot and the `timeout N`
  bound, not the `run_in_background` the call already used). A repo that
  wants a specific watch form quiet in every mode can list it in
  `poll.exempt_watch_patterns`.
- **A trailing `&`** that detaches the blocking command (including a
  backgrounded subshell or loop). A mid-command `& ` exempts just that
  segment: `sleep 30 & make build` passes.
- **A `timeout N ...` wrap** exempts the wrapped command from Class A —
  allow-through, not a downgraded ask. Rationale: an explicit bound is
  precisely the fix the guard teaches, and the Bash tool's own timeout still
  backstops it. `timeout 30 gh run watch 123` runs without friction.
- **Heredoc bodies** are stripped before analysis — `tail -f` inside a
  document you're writing is data, not a command.

## Configuration

Per-repo file: `.claude/foreground-guard.json` (also read from
`~/.claude/foreground-guard.json` for user-level defaults, and from a file
named by `$FOREGROUND_GUARD_CONFIG`). Scalars are last-present-wins (project
overrides user); pattern lists and the slow-command registry merge
additively.

```json
{
  "poll": {
    "enabled": true,
    "action": "ask",
    "extra_watch_patterns": ["^mytool\\s+follow\\b"],
    "exempt_watch_patterns": ["^gh\\s+run\\s+watch\\b"],
    "sleep_floor_seconds": 10
  },
  "slow": {
    "enabled": true,
    "action": "ask",
    "commands": {
      "make": {"test-race": 600000, "e2e*": 1800000},
      "go test ./\\.\\.\\..*-race": 600000
    }
  },
  "hint": "pr-sentinel watches PRs in this repo — don't poll gh yourself"
}
```

| Key | Default | Meaning |
| --- | --- | --- |
| `poll.enabled` | `true` | Class A on/off (switch off as the harness subsumes it) |
| `poll.action` | `"ask"` | `"deny"` escalates Class A to a hard block (override-able) |
| `poll.extra_watch_patterns` | `[]` | extra regexes matched against each wrapper-stripped command segment |
| `poll.exempt_watch_patterns` | `[]` | allowlist regexes over the same segment string; a match suppresses the watch/follow detection (exemptions win over matches) — quiet a false-positive built-in without disabling all of Class A |
| `poll.sleep_floor_seconds` | `10` | bare `sleep N` prompts at or above this |
| `slow.enabled` | `true` | Class B on/off |
| `slow.action` | `"ask"` | `"deny"` escalates Class B to a hard block (override-able) |
| `slow.commands` | `{}` | target form: command name → `{argument glob: minimum timeout ms}` (whole-word glob per argument — recommended); regex form: regex matched at a command position (prefix `.*` to match an argument) → minimum timeout ms |
| `hint` | `""` | repo-specific line appended to Class A prompts, naming your own watcher machinery |

Environment variables: `FOREGROUND_GUARD_DISABLE=1` turns the hook off for a
session; `FOREGROUND_GUARD_CONFIG=<path>` adds a config file;
`FOREGROUND_GUARD_DEBUG=1` re-raises instead of failing open (development).

## Friction report

Run `/foreground-guard:friction-report` to see where the guard's prompts land.
It re-reads the decisions Claude Code already recorded in your local session
transcripts (no telemetry — see [PRIVACY.md](PRIVACY.md)) and ranks them, so you
can tell in one command whether the friction is mostly foreground watching,
`sleep`-polling, or slow commands hitting an inadequate timeout.

```
/foreground-guard:friction-report                      # last 7 days
/foreground-guard:friction-report --since 24h --repo gateway
/foreground-guard:friction-report --json               # machine-readable
```

The `foreground-guard:` prefix is worth keeping: the companion guards
([prod-guard](#companion-plugins), workspace-guard) each ship their own
`friction-report`, so the bare `/friction-report` is ambiguous when more than
one is installed.

Prompts are grouped into a stable category taxonomy, each mapping to one fix:

| Category | Class | Fix it teaches |
| --- | --- | --- |
| `watch` | A | one non-blocking snapshot instead of streaming |
| `loop-sleep` | A | one status check now; check again next turn |
| `sandwich` | A | one status check now; check again next turn |
| `bare-sleep` | A | skip the wait; do the follow-up check now |
| `slow-timeout` | B | set an adequate `timeout:` on the call, or background it |

Denies take a second route into the report. Claude Code records a hook's stdout
only for a call it goes on to run, so a `deny` leaves no decision record at all
— counting that stream alone reports zero friction for a guard running in an
[unattended mode](#unattended-permission-modes), where every ask becomes a deny
and the friction is at its worst. The report recovers them from the error the
blocked call handed back, whose text is the reason the hook printed.

That recovery keys on the `foreground-guard: ` opener every reason here carries,
which makes it complete for this guard and partial across guards.
`python3 scripts/friction-report.py --plugin all` reports every sibling guard's
decisions found in the transcripts, and says so in a note: a guard wording its
reason differently still under-counts its denies, so a low deny count there is
not evidence that guard rarely blocks. The `FOREGROUND_GUARD_OVERRIDE downgrades`
count is this guard's alone, so `--plugin all` omits it (and its `overrides` JSON
key) rather than show one guard's statistic under an all-guards header.

For the "don't just show me the numbers, help me fix them" case, the
**`reduce-foreground-guard-prompts`** skill diagnoses the friction — grounding
itself in that same report — maps each category to its fix, and offers a
`CLAUDE.md` playbook. Ask Claude "why am I getting so many foreground-guard
prompts?" to trigger it.

## Unattended permission modes

In `auto`, `dontAsk`, and `bypassPermissions`, an `ask` is emitted as a
`deny` instead. Nothing is blocked that an ask would have let through — the
difference is where the fix lands:

| Mode | What Claude Code does with a hook `ask` |
| --- | --- |
| `default`, `acceptEdits`, `plan` | prompts you; reject it and the guard's fix goes nowhere — you paste it back to the agent by hand |
| `auto` | prompts you *anyway*, interrupting the run you chose not to babysit |
| `dontAsk` | converts it to its own generic deny, dropping the guard's reason |
| `bypassPermissions` | leaves the run stalled on a prompt no one can answer |

A `deny` blocks the same way but returns the reason to the agent, which
reads the fix and retries with `run_in_background: true`, one snapshot, or a
`timeout` bound — no human in the loop. Every deny also names the override
escape hatch and points at the [friction report](#friction-report), so a
wrong verdict gets reported rather than worked around.

Attended modes keep prompting: there is someone there to answer, and an ask
is the lighter interruption. This mirrors branch-guard's non-interactive
fail-safe, so the guards agree on which modes count as unattended.

## The override escape hatch

When the guard denies — because config escalated the class, or because the
session is in an unattended mode — a genuinely-intentional foreground wait
(or short-timeout run) can be downgraded to a confirmation prompt by
prefixing the command:

```
FOREGROUND_GUARD_OVERRIDE=demo-needs-live-tail tail -f app.log
```

Mirroring prod-guard's semantics: the override downgrades **deny → ask** —
it never silently allows. The reason string is echoed back in the downgrade
prompt for the human reviewing it (an audit trail, mirroring
workspace-guard); make it say why the foreground wait is required.

In `dontAsk` and `bypassPermissions` the override is inert and says so: a
downgrade to `ask` there buys a prompt no one will answer, so the deny
stands and the agent has to take one of the three fixes.

## Soundness: never `allow`

The hook only ever returns `ask`, `deny`, or nothing (defer). It **never**
emits `permissionDecision: "allow"` — an allow would bypass your permission
settings and override sibling guards evaluating the same command. This
invariant is asserted on every end-to-end call in the test suite. All
failure directions follow from the guard's job:

- **Infrastructure errors fail open** (bad JSON, unreadable config,
  unexpected exception → silent defer): a productivity guard must never
  break the session.
- **Unknown durations fail toward friction** (`sleep $N` prompts): a false
  positive costs one prompt; a false negative parks the main thread.
- **Parsing uncertainty defers**: unlike a security guard, a missed poll
  costs waiting, not an outage — so unparseable commands pass rather than
  guess.

## Limitations

- `bash some-script.sh` is opaque: a poll loop inside a script file is not
  seen (same policy as the sibling guards — no file inspection).
- Watch modes reached through uncovered wrappers or aliases
  (`k logs -f ...`) need an `extra_watch_patterns` entry.
- Class B matches the command text as written; a slow command constructed at
  runtime (`make $TARGET`) won't match, and neither will one reached through
  a wrapper the segmenter doesn't peel.
- The guard cannot start a background task for you — it can only teach the
  fix and let the agent retry with `run_in_background: true`.

## Companion plugins

- [workspace-guard](https://github.com/karlkfi/claude-workspace-guard) —
  keep Bash file operations inside the workspace.
- [branch-guard](https://github.com/karlkfi/claude-branch-guard) — keep
  commits off protected branches.
- [prod-guard](https://github.com/karlkfi/claude-prod-guard) — block
  mutating infrastructure commands aimed at production.

All four compose: none of them ever emits `allow`, so each can only add
friction, never remove another's.

## Privacy

Everything runs locally; the hook reads the command from stdin and local
config files, and writes a decision to stdout. No telemetry, no network.
See [PRIVACY.md](PRIVACY.md).

## License

[MIT](LICENSE)
