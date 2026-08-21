# Cross-guard convention: name your plugin in the deny reason

Every `deny` a Claude Code guard emits opens its `permissionDecisionReason` with
the plugin's own name and a colon:

```
foreground-guard: a `while`/`until`/`for` loop with `sleep` polls the main thread …
```

The convention is a cross-repo contract, not a house style. Each of the sibling
plugins ships a `friction-report.py` with a `--plugin all` mode that counts every
guard's decisions found in the local transcripts, and the opener is the only way
a deny gets counted at all. A guard that words its reason some other way reports
zero denies, in its own report and in everyone else's.

This document is the spec for guards outside this repo. foreground-guard's
implementation of the reader is `scripts/friction-report.py` (`DENY_TEXT`,
`guard_name`).

## Why the opener carries the whole join

Claude Code records a hook's stdout only for a call it goes on to run. An `ask`,
an `allow`, and a silent defer all land in the transcript as `hook_success`
attachments carrying the decision JSON, joined to the command by `toolUseID`.

A deny does not. The call never runs, so nothing writes the attachment. Measured
over 601 local transcripts for issue #25: 48k allow/ask attachments and not one
deny. Re-measured 2026-08-21 over 956: 77,562 `PreToolUse:Bash` attachments —
70,215 `allow`, 1,920 `ask`, 5,427 error — and still not one `deny`.

That family-wide zero is weaker evidence than it looks, because it is equally
consistent with *no guard denied* and with *denies are unrecorded*. What tells
the two apart is a guard whose denies are visible by the other route. Three are,
counting attachment-stream asks against denies the shipped `deny_from_result`
recovers from error results: foreground-guard 262 and 151, prod-guard 61 and 41,
pr-sentinel 0 and 53. workspace-guard shows 1,448 asks and 0 recoverable denies,
and branch-guard 140 and 0 — not because they never denied, as the table below
shows, but because their reasons carry no opener for the reader to match. The
corpus-wide zero is corroboration, not the finding: it is the figure that looks
strongest while being weakest.

The reason still reaches the transcript, by the other route. The blocked call
hands the agent back an error whose text is verbatim what the hook printed,
joined to the command by `tool_use_id` like any other tool result. Recovering
denies means reading that error text — and telling it apart from every other
failed call in the same stream: a non-zero exit, a rejected permission prompt,
an MCP error, a Python traceback. The opener is what tells them apart.

The reader cannot be widened to compensate. An opener of `<any word>: ` matches
`error: `, `warning: `, `note: `, `fatal: ` and `Traceback: `, so every unrelated
failure arrives as a phantom deny credited to a plugin that does not exist. The
name is the signal; nothing weaker survives the noise.

## What it costs to skip

The count reads zero, and zero is indistinguishable from a guard that never
blocks. The gap is worst exactly where the friction is worst: in `auto`,
`dontAsk`, and `bypassPermissions` an ask is emitted as a deny, so a guard
running unattended reports no friction at all.

Measured 2026-08-21 over 939 local transcripts holding 2,725 error tool results.
Denies were counted by running the shipped `deny_from_result`; the misses were
counted by matching each guard's own reason wording.

| Plugin | Opens with its name | Denies read | Denies missed |
| --- | --- | --- | --- |
| foreground-guard | yes | 149 | — |
| pr-sentinel | foreground-poll path only | 53 | 108 |
| prod-guard | yes | 41 | — |
| exit-status-guard | no | 0 | 498 |
| workspace-guard | no | 0 | 35 |
| branch-guard | no | 0 | see below |

243 denies read, 641 missed. pr-sentinel is the instructive row: its
foreground-poll deny carries the prefix and its duplicate-PR overlap deny opens
with a backticked command instead, so one plugin lands on both sides of the
table. Adopting the convention means every deny path, not the first one you
reach for.

A further 29 denies open `` `git push` — origin/main has moved `` and belong to
branch-guard or to pr-sentinel; the wording is not in either plugin's current
release, so they are left unattributed rather than assigned to a guess.

## Coverage starts at the version that adopted the opener

Transcripts are immutable, so the reader's reach is bounded by what was written
at the time. A deny recorded by a build that predated the prefix carries no name,
and no widening of the reader recovers it, because there is nothing there to
match.

pr-sentinel's duplicate-PR deny is the worked example. All 108 of its misses in
the table above open like this:

```
`gh pr create` — an open PR already changes files this branch changes: #135
```

A backticked command and no name. They all fall in 2026-08, and no released
version of pr-sentinel has ever carried the check — all thirteen releases, v0.1.0
through v0.9.0, have none — so whatever ships next, those 108 stay unattributed
for as long as the transcripts do.

Read a zero accordingly: it means the guard did not deny *in a build that carried
the opener*, which is not the same as not denying. foreground-guard has carried
the opener since v0.1.0, so its window is the whole life of the plugin. A guard
adopting it today starts its window today.

## The rule

1. The reason **starts** with the plugin's name, then a colon and a space. A
   name quoted mid-message is somebody else's verdict, so the reader anchors at
   the start of the text.
2. The name is the plugin's own — the `name` in `.claude-plugin/plugin.json`.
   Not the hook's, not the script's, not a display title.
3. Use the same opener on **every** deny path, including the ones added later.
4. Lead with the name on everything else the guard emits, too — `ask` reasons,
   `PostToolUse` nudges, `additionalContext`, `systemMessage`. Claude Code
   attributes none of them: an ask prompt reaches the human, and a deny reaches
   the agent, with no plugin name attached either way, so the opener is the only
   attribution either one gets. Only denies need the *colon*, because only the
   reader keys on it — but a guard that always leads with its name has no path
   to get wrong.

A leading `Error: ` inserted by the harness is tolerated by the reader, so it
does not have to be stripped.

Rule 4 is satisfied by the name alone, so a string may lead with the name and no
colon where it can never be a deny. foreground-guard's override downgrade is the
one such path here: it opens `foreground-guard override acknowledged (…)` and
sets the decision to `ask` by construction, so `DENY_TEXT` is never asked to read
it. Measured 2026-08-21 over 956 local transcripts, that wording appears on 95
lines and not once inside an error tool result. A path that can also deny takes
the colon, and `tests/test_foreground_guard.py` asserts exactly that on every
end-to-end call — against the shipped reader, not a copy of its regex, so the two
halves cannot drift apart in silence.

## The hook script name has to agree

The two streams are counted together, so they have to resolve to the same word.
The attachment stream has no reason text to read; it labels a decision by the
`.py` file the hook command names, stripping a leading `bash-`:

| Hook command names | Label |
| --- | --- |
| `bash-foreground-guard.py` | `foreground-guard` |
| `branch-guard.py` | `branch-guard` |
| `pr-sentinel-guard.py` | `pr-sentinel-guard` — a word apart from its denies |

pr-sentinel is the case that breaks: its script is `pr-sentinel-guard.py` and its
reasons open `pr-sentinel: `, so one plugin arrives under two labels, and neither
`--plugin` value returns both halves of its record. foreground-guard's reader
folds that specific name back (`NON_GUARD_PLUGINS` in `friction-report.py`), but
the fold is a list someone has to edit.

Name the hook script `<plugin-name>.py` or `bash-<plugin-name>.py` and the
question does not come up.

## Checking your own

Run the report across guards and look for your plugin's denies:

```
python3 scripts/friction-report.py --plugin all --since all
```

If your plugin appears with asks and no denies, either it never denied in the
window or its reasons are unreadable. Force the second case apart from the first
by grepping the transcripts for a reason you know it printed:

```
python3 - <<'PY'
import glob, json, os
SIG = 'a phrase from your own deny reason'
for p in glob.glob(os.path.expanduser('~/.claude/projects/**/*.jsonl'), recursive=True):
    for line in open(p, encoding='utf-8'):
        if SIG in line:
            print(json.loads(line).get('timestamp'), p)
PY
```

A hit there with no deny in the report is the convention not being followed.
