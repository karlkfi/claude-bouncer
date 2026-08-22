# Session-scoped override (Q12, issue #26)

**Goal:** Let a human approve an intentional production-target override once
per session per target instead of once per command, so a sanctioned batch of
commands against the mixed prod/dev dogfood cluster costs one prompt, not N.

**Approach:** A new `PROD_GUARD_SESSION_OVERRIDE=<reason>` inline prefix
downgrades a deny to an ask exactly like `PROD_GUARD_OVERRIDE` — but a new
PostToolUse hook (same script, branching on `hook_event_name`) records a
target-scoped grant after the command actually ran, which is the only reliable
signal that the human approved the ask. Subsequent prefixed commands in the
same session whose production findings are all covered by grants defer
silently. Everything else — unprefixed commands, ambient-context denies,
shared-state switches, unknown targets — is unaffected.

## Semantics

| Case | Decision |
|---|---|
| `PROD_GUARD_SESSION_OVERRIDE=r <cmd targeting explicit prod X>`, no grant for X | **ask** (message: approving records a session grant for X) |
| Same command after the ask was approved and the command ran | defer (silent; normal permissions still apply) |
| Same target, same session, **no prefix** | **deny** (the prefix is required on every command — the reason stays on the record in the command line) |
| Same prefix, different session / expired grant (8 h TTL) | **ask** again |
| Prefix + ambient-context deny or switch deny | **ask** every time (not grantable) |
| Prefix + unknown target | **ask** every time (classify it in config instead) |
| `bypassPermissions` mode | unchanged: ask becomes deny, so no grant can ever be minted without a human |

Grant scope rules (all must hold for a finding to be suppressed):

- The command segment carries the `PROD_GUARD_SESSION_OVERRIDE` inline prefix.
- The finding is a **deny** whose production classification came from an
  **explicit** target (flag/env pin), never from ambient resolution — ambient
  state can be repointed mid-session, which is exactly threat model 2.
- Every target the finding classified as prod is granted (a multi-locator
  gcloud command needs all its prod locators granted — they are recorded
  together from the first approval).
- Shared-state switch denies (`kubectl config use-context`, `kubectx`,
  `gcloud config set`, `az account set`, `docker context use`,
  `terraform workspace select`, `pulumi stack select`, argocd context) are
  never grantable: their blast radius is every parallel session.

## Mechanics

1. **Findings become 3-tuples** `(severity, reason, grant_targets)`. Only
   `deny_prod` at explicit-target call sites passes a non-None targets tuple;
   ambient/switch sites and the other constructors pass None.
2. **Grant store**: `$HOME/.claude/prod-guard/session-grants/<session-id>.json`
   holding `{"grants": [{"target", "reason", "ts"}]}`. Exact-string target
   match. TTL 8 h from first grant (no sliding refresh — conservative).
   Atomic write (`os.replace`), opportunistic cleanup of sibling files older
   than 7 days. All store errors fail toward *more* prompts (unreadable → no
   grants; unwritable → nothing recorded), never fewer.
3. **PreToolUse**: after evaluation, if the session prefix is present, load
   grants for `session_id` and drop deny findings fully covered by them. If
   nothing remains → silent defer. Otherwise the existing downgrade applies
   (deny→ask when either override var is present), with a message that says
   approving will record a session grant for the named targets (or that
   nothing here is grantable). The message keeps the literal phrase
   `override acknowledged` so friction-report categorization keeps working.
4. **PostToolUse** (new hooks.json entry, same script): fires only if the
   command ran, i.e. the ask was approved (or everything already deferred).
   Re-evaluates the command; records grants for the explicit prod targets of
   its deny findings under (`session_id`, reason = the var's value).
5. No `session_id` in the hook input → grants can neither load nor record; the
   prefix degrades gracefully to per-command `PROD_GUARD_OVERRIDE` behavior.

## Security review

- The first command per (session, target) still hits a human — the speed bump
  the issue explicitly keeps. A grant can only be minted by an executed
  command, which requires an approved ask (PostToolUse never fires for a
  denied one) — and in `bypassPermissions` the ask becomes deny, so unattended
  modes cannot mint grants.
- Suppression yields *silence*, never `allow` — normal permission settings
  still apply, and the hook still composes with sibling guards.
- Prompt-injected commands gain nothing new: an attacker-crafted
  `PROD_GUARD_SESSION_OVERRIDE=x` command still asks on first use, and an
  existing grant only covers the exact targets a human already approved
  mutating this session.
- Fail directions preserved: store infrastructure errors → more prompts
  (fail-closed on the security decision); JSON/parse errors → silent defer
  (fail-open on infrastructure), unchanged.

## Deliverables

- [x] `scripts/bash-prod-guard.py`: 3-tuple findings, grant store,
      PostToolUse branch, pre-decision suppression + messages.
- [x] `hooks/hooks.json`: PostToolUse Bash entry.
- [x] Tests: unit (grant store, TTL, tuple shape) + e2e (grant lifecycle:
      ask → record → defer; prefix required; wrong session; expiry; ambient
      and switch not grantable; bypassPermissions; multi-locator gcloud) +
      wiring (PostToolUse entry).
- [x] Docs: README decision table + override section + configuration,
      `docs/design.md` rationale, PRIVACY.md (local state file).
- [x] `docs/STATUS.md`: revive Q12 → complete it (isolated commit,
      `session-backlog` skill).
