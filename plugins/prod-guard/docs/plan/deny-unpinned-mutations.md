# Plan: deny mutating commands with no explicit target (issue #10)

## Goal

A mutating infrastructure command that does not pin its target explicitly is
**denied** with a self-healing fix-it message (add the pin flag and retry),
instead of prompting. The remaining `ask` prompts already machine-generate the
parsed target; extend the kube tools to also echo the namespace.

## Why (from issue #10)

Two downstream conventions were being enforced by written CLAUDE.md rule, not by
hook: (1) pin `--context`/`--project` on every mutating command, because shared
`~/.kube/config` / gcloud config is clobber-prone across parallel sessions; (2)
echo the resolved target before a destructive verb so the human sees where it
lands. Conventions relying on agent discipline degrade; a hook doesn't.

This is a **strict tightening** (secure-by-default): today an unpinned mutation
rides whatever the ambient context happens to be and prompts a human; after this
it cannot run until the target is explicit. Friction *drops* for the agent (a
deny-with-reason self-heals in one round trip: the agent adds `--context <ctx>`
and the re-run defers), while the human-facing prompts that remain always carry
the true parsed target.

## Scope decision: flip `ask_ambient`, uniformly

The existing shared abstraction for "mutating verb, no explicit pin" is the
`ask_ambient` helper (called by `policy()` and every ambient-resolving
evaluator). The minimal, uniform change is to make that helper a **deny**. This
automatically covers kubectl/helm/flux/gcloud/aws/az/terraform/argocd/doctl/
pulumi — every tool whose ambient path currently prompts — without special-casing
the four tools the issue names. It reuses the model rather than adding a new one
(per CLAUDE.md), and it is the secure default.

**Deliberate carve-outs (unchanged):**
- `ask_switch` (context-*switching* commands like `use-context`, `config set`,
  `kubectx`) stays **ask** — you cannot "pin" a command whose whole purpose is to
  repoint shared state.
- `ask_unknown` (explicit-but-unclassified target) stays **ask** — the target
  *is* pinned (not clobber-prone), just unclassified; fail-closed confirm.
- Tools that already **defer** on a non-prod ambient target and never call
  `ask_ambient` keep deferring: docker local-daemon contexts, docker non-prod
  *remote* contexts, and ansible non-prod inventories (its inventory is cwd/file
  pinned, not clobber-prone shared state — closer to gh's worktree-pinned repo).
  Only their *unknown*-ambient path (which does call `ask_ambient`) flips to deny.
- `PROD_GUARD_OVERRIDE` still downgrades any deny → ask, so an unpinnable ambient
  command remains runnable in one deliberate, auditable step.

## Point 2: machine-generate the target line

The remaining `ask` messages (`ask_unknown`, `ask_switch`) already embed the
parsed target (context/project/server/subscription). The only gap vs. the issue's
"context/project/**namespace**" is the kube namespace. Resolve `-n`/`--namespace`
in `_kube_target` and append it to both the explicit and ambient descs, so
kubectl/helm/flux deny/ask messages name the namespace the mutation lands in.

## Edits

1. **`scripts/bash-prod-guard.py`**
   - Rename `ask_ambient` → `deny_ambient`; return `DENY` with a pin-required,
     self-heal message that names the ambient target and the override hatch.
     Keep the recognizable signature substring "shared mutable state that a
     parallel session can repoint" for the friction report.
   - Update all call sites (policy ×2, gcloud, az, terraform, docker, argocd,
     doctl, pulumi, ansible, `ambient_aws_default_profile_decision` ×2).
   - Add namespace resolution to `_kube_target`; append to descs.
   - Update the module docstring decision-semantics block (ambient → deny).
2. **`scripts/friction-report.py`** — rename category `ask-ambient` →
   `deny-ambient`; update the hint (signature regex unchanged).
3. **`README.md`** — threat-model-2 wording (ask → deny-with-fixit); "What it
   does" ask/deny bullets; decision-table rows (ambient kube / terraform / aws /
   pulumi-unresolved: ask → deny); Covered-tools "otherwise prompts" → "otherwise
   denies (pin required)".
4. **`docs/design.md`** — decision matrix (ambient → deny); new rationale on why
   deny-with-fixit beats ask for the unpinned case; update the "block parallel
   sessions" alternative's closing line.
5. **Tests** — flip every ambient-ask assertion to deny (`test_prod_guard.py`);
   add a test asserting the deny message carries the pin hint + self-heal wording;
   add a kube namespace-in-message test; rename the `ask-ambient` category cases
   in `test_friction_report.py` to `deny-ambient`.

## Verification

- `python3 -m unittest discover tests`
- Hand-exercise the README decision table (subprocess-only for any bypass form).
