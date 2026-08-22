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

| Mode | `allow` | defer (no output) | `ask` | `deny` |
|---|---|---|---|---|
| `manual` | runs | **blocked** | blocked | blocked |
| `dontAsk` | runs | **blocked** | blocked | blocked |
| `plan` | runs | **blocked** | blocked | blocked |
| `auto` | runs | **RUNS** | blocked | blocked |
| `acceptEdits` | runs | **RUNS** | blocked | blocked |
| `bypassPermissions` | runs | **RUNS** | blocked | blocked |

Two facts carry everything else on this page:

1. **`ask` and `deny` block in every mode, including `bypassPermissions`.** The
   boundary holds unattended. (This re-confirms the Q17 finding at 2.1.220; it
   was first measured at 2.1.159, before `auto`, `manual`, and `dontAsk`
   existed as named modes.)
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
suppression, the interpreter suppression — is inert in `auto`, `acceptEdits`,
and `bypassPermissions`. Those mechanisms restore the operator's own permission
rules, which is worth exactly what those rules are worth in that mode.

This also qualifies a claim in [`design.md`](design.md): defer is described as
net-neutral, leaving the operator "no worse off than without the hook." True —
but in a pre-approving mode, *without the hook* means the command runs. Defer is
neutral, not safe, and it is safe only where the fallback has teeth.

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

## `ask` blocks, but it does not always teach

`ask` and `deny` are equally blocking. They differ in what the *agent* learns,
and that differs by how the session is running:

- **Unattended (headless `-p`)** — measured: both `ask` and `deny` surface the
  hook's `permissionDecisionReason` to the agent, which can then route around it.
- **Interactive** — the reason is rendered for the *human* in the approval
  prompt. When the human accepts or denies, the agent does not receive that text;
  operators report having to copy the hint and paste it back to change the
  agent's behavior for the rest of the session.

So in an interactive session an `ask` is a wall the agent cannot learn from,
while a `deny` carrying a reason is a **gate**: it blocks, explains itself, and
the agent either corrects course or takes the documented override. That is the
reasoning already applied to the sibling-checkout write and the unanchored kill
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

Do not infer a mode's behavior from its name. `dontAsk` blocks a deferred
command rather than waving it through, which is the opposite of what the name
suggests.
