# Security notes: out-of-scope findings

A companion to the [backlog](../../../docs/queue/README.md). This file records security observations that came out of audits but are **not** going on the backlog — either because the impact is too low to warrant code changes, because they reflect the documented threat model, or because they can't be reproduced reliably. They're recorded here so a future audit doesn't repeat the investigation.

For known limitations that affect normal use, see the **Limitations** section of [`../README.md`](../README.md). For active work, see the [backlog](../../../docs/queue/README.md).

## ANSI escape injection in `permissionDecisionReason`

The hook builds its reason string by concatenating file tokens that originated in the agent's command:

```python
reason = "Outside-workspace path(s): " + ", ".join(sorted(set(outside)))
```

A token containing an ANSI escape (e.g. `\x1b[2J`) would be embedded verbatim and could in principle distort the confirmation prompt the user sees.

**Why not backlogged:** in practice the tokens flow through `shlex` and any embedded `$(...)` substitution is broken into separate tokens before the escape sequence can land verbatim in a single argument. Hand-tested with `cat /etc/$(printf "\033[2J")passwd` — the reason rendered as a benign `/etc/$`. Claude Code is also expected to render the reason as plain text in its prompt UI, not stream it raw to a terminal. The realistic blast radius is a confused confirmation prompt, not file disclosure, and the easy mitigations (stripping control chars from `outside` before join) can be added later if a real proof-of-concept appears.

## Case-insensitive filesystem bypass (macOS / Windows)

`os.path.realpath` returns canonical casing only for paths that exist on disk; for non-existent paths it normalizes lexically and preserves the input casing. The outside-workspace check is a case-sensitive prefix comparison:

```python
if rp != proj and not rp.startswith(proj + os.sep):
```

On a case-insensitive filesystem (default on macOS and Windows), a workspace at `/Users/karl/foo` could in theory be bypassed by a path like `/Users/karl/FOO/../../../etc/passwd`, since `realpath` of a non-existent path preserves `FOO` and the prefix check fails.

**Why not backlogged:** could not reproduce. On macOS APFS, `realpath` on existing intermediate components canonicalizes the casing, so the bypass requires the target path to be *fully* non-existent, which makes the read fail anyway. A real exploit would need a casing-mismatched workspace path *and* a partially-existing target outside it. If a reproducer surfaces, the fix is straightforward: compare with `os.path.normcase` on platforms where the filesystem is case-insensitive.

## Wrapper commands (`xargs`, `find -exec`, `bash -c`, `eval`)

A command of the form `xargs cat`, `find / -name passwd -exec cat {} +`, `bash -c 'cat /etc/passwd'`, or `eval 'cat /etc/passwd'` runs `cat` against arbitrary paths, but the *leading* command (`xargs`, `find`, `bash`, `eval`) is not in `SPEC`. Some of these embed `cat` as a non-leading token; the parser's `files_in_command` only consults the first token, so it never inspects the inner command.

**Why not backlogged:** this is the documented threat model, not a regression. The hook guards specific commands by name; it doesn't try to model every shell that can spawn a subshell. Plain permission rules (`Bash(bash:*)`, `Bash(eval:*)`, `Bash(xargs:*)`, `Bash(find:*)`) are the right layer for these — the hook intentionally defers and lets them apply. If a future change tries to extend coverage to wrappers, that's a substantial scope change and should land as a plan doc, not a one-row Queue item.

## Hook input trust

The hook trusts two values from its environment without cross-checking:

- `cwd` from the hook input JSON (used to resolve relative paths).
- `CLAUDE_PROJECT_DIR` from the process environment (used as the workspace root).

If either is wrong — e.g. `CLAUDE_PROJECT_DIR` is set to `/` — every outside-workspace check passes. This is correct behavior: the hook is a tool the user installs into their own environment, and an attacker who can rewrite the user's env vars or the harness's hook payload has already won.

**Why not backlogged:** there's nothing to fix at the hook level. The README's install section already requires the plugin be installed by the user; that's the trust boundary.

## Headless / full-auto `ask` is fail-closed (not fail-open)

A plausible-sounding claim — that a `PreToolUse` hook's `ask` (and `defer`) is remapped to `allow` in non-interactive runs because "there's no one to prompt" — would mean this hook fails *open* in full-auto, silently allowing outside-workspace reads. That claim is **false** at CLI 2.1.159.

Verified end-to-end with a forced-decision hook and sentinel file (Q17):

| Mode | Hook decision | Command runs? |
|---|---|---|
| headless `-p` (`default`) | `ask` | **no** — "not approved" |
| `--dangerously-skip-permissions` (`bypassPermissions`) | `ask` | **no** — "needs your approval" |
| `bypassPermissions` | `allow` (control) | yes |
| `bypassPermissions` | `deny` (control) | no |

So `ask` blocks in every unattended mode; the boundary holds regardless of `permission_mode`. The remaining difference is *recoverability*, not security: a blocking `ask` only hands the model an unanswerable approval prompt it stalls on, whereas `deny` feeds the reason back so it routes around the path. That is why the hook emits `deny` (not `ask`) for outside paths when `permission_mode == "bypassPermissions"` — see the **Configuration** and **Limitations** sections of [`../README.md`](../README.md).

**Lesson for future audits:** don't trust a documented runtime behavior for a security property — exercise it. The hook input JSON also can't distinguish interactive `default` from headless `-p` `default` (both report `permission_mode: "default"`), so `bypassPermissions` is the only reliable "no human present" signal available to the hook. The behavior matrix is locked in by `tests/test_workspace_guard.py` (`test_outside_bypass_permissions_deny` and siblings).

## When to promote a note to the Queue

Move an item from this file to the [backlog](../../../docs/queue/README.md) when any of the following changes:

- A reproducer lands that demonstrates real file disclosure or write (not just a distorted prompt).
- The threat model expands — e.g. the plugin starts being installed by a system administrator rather than the end user, raising the bar on env-var trust.
- Claude Code's prompt UI changes in a way that makes the ANSI-escape concern exploitable.

If you add a row to the Queue, delete the corresponding section here so the two files don't drift.
