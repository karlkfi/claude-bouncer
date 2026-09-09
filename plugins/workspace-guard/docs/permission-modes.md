# Permission modes: what each guard decision actually does

The hook returns one of four things — `allow`, `ask`, `deny`, or nothing at all
(*defer*). What each one **does** depends on the session's permission mode, and
the differences are large enough to change which decision is the right one.

This page records the measured behavior. Read it before changing any decision
in [`../scripts/bash-workspace-guard.py`](../scripts/bash-workspace-guard.py):
a decision that protects in one mode can be a no-op in another.

## The measured matrix

Claude Code 2.1.220 offers six permission modes (`claude --permission-mode`).
Each cell below is an end-to-end run: a hook forced to one decision, a session
asked to run `touch sentinel.txt`, and a check for whether the file appeared.
Nothing answered the prompt in any of these runs, so the `ask` column is the
unanswered outcome.

| Mode | `allow` | defer (no output) | `ask` | `deny` |
|---|---|---|---|---|
| `manual` | runs | **blocked** | blocked | blocked |
| `dontAsk` | runs | **blocked** | blocked | blocked |
| `plan` | runs | **blocked** | blocked | blocked |
| `auto` | runs | **RUNS** | blocked | blocked |
| `acceptEdits` | runs | **RUNS** | blocked | blocked |
| `bypassPermissions` | runs | **RUNS** | blocked | blocked |

Two facts carry everything else on this page:

1. **No mode auto-approves an `ask` or a `deny`, `bypassPermissions` included.**
   Neither runs on the hook's decision alone, so the boundary holds unattended.
   An `ask` the operator approves does run — that is what an `ask` is for. What
   no mode does is waive the interruption. (This re-confirms the Q17 finding at
   2.1.220; it was first measured at 2.1.159, before `auto`, `manual`, and
   `dontAsk` existed as named modes.)
2. **Defer is not neutral.** In `auto`, `acceptEdits`, and `bypassPermissions`
   the command simply runs. Deferring hands control back to the permission
   system, and in those three modes that system's answer is *yes*.

## What the plugin's job is, per mode

The modes differ in their **baseline** — what happens to a command the hook says
nothing about. That baseline decides which lever does any work.

| Baseline | Modes | The plugin's job | Levers that work |
|---|---|---|---|
| Everything prompts | `manual`, `dontAsk`, `plan` | **Reduce friction.** `allow` in-workspace work so the operator isn't approving every in-repo `grep`. | `allow` (friction), `ask`/`deny` (safety) |
| Bash is pre-approved | `auto`, `acceptEdits` | **Downgrade the auto-approval.** The operator has opted out of prompts; the hook is the only thing that can put one back. | `ask`, `deny` only |
| Everything runs, no human | `bypassPermissions` | **Block and explain.** No one can answer a prompt, so the decision has to steer the agent. | `deny` (and `ask`, which blocks but strands the agent) |

The consequence worth internalizing: **`defer` is a protective decision only in
`manual`, `dontAsk`, and `plan`.** Every "the hook declines to vouch, so it
defers" mechanism — the signalling-command suppression, the shell `-c`
suppression, the interpreter suppression — restores the operator's own
permission rules, which is worth exactly what those rules are worth in that
mode. In `auto`, `acceptEdits`, and `bypassPermissions` they are worth nothing:
the fallback is the pre-approval the suppression was trying to withhold.

Q74 closed that for the two suppressions naming a construct this guard already
judges. A `sh -c` body it could not reach, and a kill it could not scope, now
**escalate** in those three modes rather than deferring — `ask` in `auto` and
`acceptEdits`, where a prompt reaches somebody, and `deny` under
`bypassPermissions`, where it would not. The interpreter suppression stays
inert by default and is the one this paragraph still describes unchanged:
escalating it costs 4.67% of all commands against 0.09% for the other two
(measured 2026-09-09 over 89,133 corpus commands), and an interpreter's own
file access is a documented non-goal. `WORKSPACE_GUARD_ESCALATE` moves that
line in either direction; see the README's Configuration section.

**Do not read the escalation as a reason to treat the three modes alike.** They
share one property — a defer runs — and differ on the one that picks the
verdict: `auto` and `acceptEdits` have a person at the prompt and
`bypassPermissions` does not. Collapsing them denies in a mode where somebody
was there to say yes, which removes the human rather than protecting them.

This also qualifies a claim in [`design.md`](design.md): defer is described as
net-neutral, leaving the operator "no worse off than without the hook." True —
but in a pre-approving mode, *without the hook* means the command runs. Defer is
neutral, not safe, and it is safe only where the fallback has teeth.

## `dontAsk` blocks an `ask` without swallowing its reason

Q84 asked whether a supervising posture had to route around `dontAsk`, on the
reading that an `ask` there strands the agent behind a prompt nobody can answer.
Six cells at CLI **2.1.261**, same probe as the matrix (`touch sentinel.txt`),
nothing answering any prompt:

| Cell | Ran? | What reached the agent |
|---|---|---|
| `manual` × defer | no | Claude Code's own message |
| `dontAsk` × defer | no | Claude Code's own *don't ask mode* message |
| `auto` × defer | **yes** | — |
| `dontAsk` × `allow` | **yes** | — |
| `dontAsk` × `ask` | no | the hook's `permissionDecisionReason`, verbatim |
| `dontAsk` × `deny` | no | the hook's `permissionDecisionReason`, verbatim |

The first four reproduce the matrix, which is what makes the last two worth
reading — a harness that cannot show a command running cannot show one blocked.
In `dontAsk` an unanswered `ask` blocks and feeds its reason back exactly as a
`deny` does, so there is nothing to route around: the posture ships with no
`dontAsk` case, and `bypassPermissions` keeps the forced deny it already had.

**The generic message that loses the hook's reason belongs to defer, not to
`ask`.** That is the distinction to hold onto, because the two are easy to
attribute to each other from a transcript: both block, and only one of them is
the hook's own doing.

These were headless `-p` runs, so nothing could answer a prompt in any mode.
They establish that the reason survives — not whether an interactive `dontAsk`
session renders a prompt at all.

Q84 also answers the re-measure trigger it fired. It adds no new mode
condition: `decide()` reads the `bypassPermissions` term it already had, and the
posture reads underneath it. So the matrix carries this change without moving.

## Do not assume the operator configured an allowlist

The plugin's rationale in [`design.md`](design.md) starts from an operator who
pre-approves `Bash(grep:*)` and friends, so the hook's job is to *narrow* a
pre-approval that already exists. That is not universal. An operator who
deliberately runs without permission rules — because a glob-matched allowlist is
too blunt to trust — gets the opposite relationship: the hook's `allow` is not
narrowing anything, it is **granting** access that no rule granted.

Both configurations are legitimate and the hook cannot tell them apart. The safe
reading is the second one: treat `allow` as a grant, and spend it only where the
hook genuinely understands the whole command string.

## `ask` interrupts, but it does not always teach

Neither `ask` nor `deny` lets a command run on the hook's decision alone. They
differ in who can lift them and in what the *agent* learns, and the second
differs by how the session is running:

- **Unattended (headless `-p`)** — measured: both `ask` and `deny` surface the
  hook's `permissionDecisionReason` to the agent, which can then route around it.
- **Interactive** — the reason is rendered for the *human* in the approval
  prompt. When the human accepts or denies, the agent does not receive that text;
  operators report having to copy the hint and paste it back to change the
  agent's behavior for the rest of the session.

So in an interactive session an `ask` interrupts the human without teaching the
agent anything, while a `deny` carrying a reason is a **gate**: it blocks,
explains itself, and the agent either corrects course or takes the documented
override. That is the reasoning already applied to the sibling-checkout write,
the cross-session scratch write, and the unanchored kill
(see [`design.md`](design.md)), and it generalizes: prefer `deny` + override
wherever the correct response is "change the command," and reserve `ask` for
cases where "approve this one, unchanged" is genuinely the right answer.

`ask` remains the default for an outside-workspace path because approving a
one-off read (`cat /etc/os-release`) *is* the right answer often enough that a
deny would be wrong. The override exists for the cases where it isn't.

## When re-measuring is required

The matrix is a property of Claude Code, not of this plugin, and it has already
changed once — three of the six modes above did not exist under their current
names when the hook's mode handling was written. Re-run the matrix when:

- the CLI's `--permission-mode` choices change (`claude --permission-mode` with
  an invalid value prints the current list);
- a new decision type appears in the hook API;
- any change makes a decision conditional on `permission_mode`.

Q74 fired the third trigger and answered the first, which is the half that
decides whether the mode names in the code are still the whole set. At CLI
**2.1.261** the list is `acceptEdits, auto, bypassPermissions, manual, dontAsk,
plan` — the same six the matrix above was measured on at 2.1.220, so
`DEFER_RUNS_MODES` names every mode that exists and no seventh is silently
unguarded. The cells themselves were **not** re-run: the escalation emits `ask`
and `deny`, two rows the matrix already carries, so it depends on the matrix
without changing it. A CLI that altered what those two rows do would move this
guard's behaviour everywhere, not only here.

Do not infer a mode's behavior from its name. `dontAsk` blocks a deferred
command rather than waving it through, which is the opposite of what the name
suggests.
