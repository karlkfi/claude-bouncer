# prod-guard

**Production-target guard rails for Claude Code Bash commands.**

[![release](https://img.shields.io/github/v/release/karlkfi/claude-prod-guard)](https://github.com/karlkfi/claude-prod-guard/releases) [![tests](https://img.shields.io/github/actions/workflow/status/karlkfi/claude-prod-guard/tests.yml?branch=main&label=tests)](https://github.com/karlkfi/claude-prod-guard/actions/workflows/tests.yml) [![License: MIT](https://img.shields.io/github/license/karlkfi/claude-prod-guard.svg)](LICENSE) [![Claude Code plugin](https://img.shields.io/badge/Claude_Code-plugin-7e57c2)](#install)

> Let Claude mutate kind and dev clusters all day. Stop it at prod.

You ask Claude to "clean up the failed rollout." It runs `kubectl delete ns
app-staging` — against whatever context `~/.kube/config` currently points at.
Or `terraform destroy` in a directory whose selected workspace is `prod`. Or
`gcloud compute instances delete` against the active gcloud project, which a
*parallel* Claude session just repointed with `gcloud config set project`.
The default `Bash(kubectl:*)` permission rules can't tell these apart from
the dozens of harmless dev-cluster commands Claude runs every session.

prod-guard is a `PreToolUse` hook for `Bash` that resolves where each
infrastructure command will actually land — the explicit `--context` /
`--project` / `--profile` flag if present, otherwise the tool's ambient
config — classifies that target against configurable patterns, and:

- **blocks** mutating commands aimed at a **production** target, mutating
  commands that don't pin their target at all — relying on **shared ambient
  state** a parallel session can silently repoint — and the **switch commands
  that do the repointing** (`kubectl config use-context`, `gcloud config set`,
  …), each with a fix-it that tells the agent which flag to add;
- **prompts** for mutating commands whose explicit target is **unknown**;
- **stays silent** for everything else, so your normal permissions apply.

## Contents

- [The two threat models](#the-two-threat-models)
- [What it does](#what-it-does)
- [Install](#install)
- [Keeping it updated](#keeping-it-updated)
- [Covered tools](#covered-tools)
- [Configuration](#configuration)
- [The override escape hatch](#the-override-escape-hatch)
- [Agent guidance: avoiding prompts](#agent-guidance-avoiding-prompts)
- [Measuring friction](#measuring-friction)
- [Limitations](#limitations)
- [Companion plugins](#companion-plugins)
- [Design](#design)
- [Privacy](#privacy)
- [Contributing](#contributing)
- [License](#license)

## The two threat models

**1. Prod blast-radius.** A mutating verb whose *resolved* target is a
production environment. Explicit targeting doesn't prevent this —
`kubectl --context prod delete ns x` is perfectly explicit and perfectly
catastrophic. Only a guard that classifies the target can. prod-guard
hard-blocks (`deny`) these, with a deliberate [override](#the-override-escape-hatch)
for the rare intentional prod operation.

**2. Ambient-context clobbering.** A mutating command that doesn't pin its
target relies on shared mutable state: the kubeconfig `current-context`, the
active gcloud configuration, the docker context, the default AWS profile.
When several Claude Code sessions run in parallel — increasingly the normal
way to work — any of them can repoint that state between the moment a command
is written and the moment it runs. prod-guard **denies** these with a
self-healing fix-it (`deny`) that names the resolved target and the flag to
add: the command can't run until the target is explicit, and the agent
self-corrects in one round trip by adding `--context`/`--project`/… and
retrying. A `deny` beats an `ask` here because it's machine-actionable — it
doesn't stall waiting on a human, and it makes the [override](#the-override-escape-hatch)
available if pinning genuinely isn't possible.

## What it does

The hook produces one of three outcomes per command (worst segment wins
across `&&`/`|`/`;`/`$( )` chains):

- **deny** — the command is blocked with a reason naming the resolved target
  and the override. This is the outcome for *mutating verb + production
  target*, however the target was resolved (explicit flag or ambient state);
  for *mutating verb + no explicit target at all* (the clobbering risk) —
  with a fix-it naming the flag to add, so the agent re-runs pinned instead
  of stalling a human; and for the *switch commands that repoint the shared
  target itself* (`kubectl config use-context`, `kubectx`,
  `gcloud config set`, `az account set`, `docker context use`) — regardless
  of what they switch to, since the repoint clobbers every parallel session
  and a per-command pin flag makes it unnecessary.
- **ask** — Claude Code shows its standard permission prompt. This is the
  outcome for *mutating verb + unknown explicit target* (fail closed: unknown
  is never silently allowed), and for shared-state writers that have no
  per-command pin alternative (`gcloud auth login`, `oc login`,
  `aws configure`, `kubens`, kubeconfig edits, …).
- **defer** — the hook stays silent; your normal permission settings apply.
  This is the outcome for read-only verbs, non-production targets, uncovered
  tools, any segment that only asks a CLI for its usage text (`--help`, `-h`,
  or `help` as the first word), and any segment whose sole argument is a
  version flag (`--version`, `-version`, `-V`, `-v`). prod-guard never emits
  `allow`, so it composes with other guards instead of overriding them.

Every `deny` and `ask` reason opens `prod-guard: `. Claude Code names the plugin
in neither the permission prompt nor the error text handed back to the agent, so
the opener is what identifies which guard decided when several are installed.

| Command | Decision |
| --- | --- |
| `kubectl --context kind-ci delete pod x` | defer |
| `kubectl --context prod-us get pods` | defer |
| `helm template api ./chart` | defer |
| `terraform plan` | defer |
| `gcloud compute instances list --project acme-prod` | defer |
| `docker rm -f app` (local daemon) | defer |
| `kubectl --context prod-us delete ns x` | **deny** |
| `kubectl --context blue-2 delete ns x` (cluster server is `api.prod…`) | **deny** |
| `kubectl scale deploy x --replicas=0` (current-context is prod) | **deny** |
| `TF_WORKSPACE=prod terraform apply` | **deny** |
| `TF_WORKSPACE=dev terraform apply` (S3 backend bucket is `acme-prod-tfstate`) | **deny** |
| `gcloud compute instances delete vm1 --project acme-prod` | **deny** |
| `docker push registry.prod.acme.io/app:1` | **deny** |
| `ssh deploy@prod-web-1 uptime` | **deny** |
| `ssh dev-box uptime` | defer |
| `helm upgrade --help` | defer |
| `docker run -h box nginx` (`-h` is `--hostname`, not help) | judged as a run |
| `ssh prod-web-1 --help` (OpenSSH runs it on the remote) | **deny** |
| `ansible --version` (sole argument: no target in the command) | defer |
| `helm install api ./chart --version 2.0.0` (a chart version) | judged as an install |
| `pulumi up --stack acme/prod` | **deny** |
| `pulumi up` (no stack; selected stack is `acme/prod`) | **deny** |
| `pulumi up` (no stack pinned, selection unresolved) | **deny** (pin `--stack`) |
| `ansible-playbook -i inventories/prod site.yml` | **deny** |
| `ansible prod-db -m ping` (read-only module) | defer |
| `echo done && kubectl --context prod-us delete ns x` | **deny** |
| `bash -c 'kubectl --context prod-us delete ns x'` | **deny** |
| `kubectl delete pod x` (ambient kind context, no `--context`) | **deny** (pin `--context`) |
| `kubectl --context bluefin apply -f m.yaml` (unclassified) | **ask** |
| `terraform apply` (no workspace pinned) | **deny** (pin `TF_WORKSPACE`) |
| `aws s3 rm s3://bucket/key` (no profile pinned) | **deny** (pin `--profile`) |
| `aws ec2 terminate-instances …` (default profile → prod SSO account) | **deny** |
| `aws s3 rm … --profile admin` (`[profile admin]` → prod SSO account) | **deny** |
| `kubectl config use-context kind-ci` | **deny** (pin `--context`) |
| `gcloud config set project acme-dev` | **deny** (pin `--project`) |
| `kubectx prod-us` | **deny** |
| `gcloud auth login` | **ask** |
| `PROD_GUARD_OVERRIDE=incident-42 kubectl --context prod-us delete ns x` | **ask** |
| `PROD_GUARD_SESSION_OVERRIDE=drill-7 kubectl --context prod-us delete ns x` (first use) | **ask** (approval grants `prod-us` for the session) |
| `PROD_GUARD_SESSION_OVERRIDE=drill-7 kubectl --context prod-us cordon n1` (after that approval, same session) | defer |
| `kubectl --context prod-us drain n1` (grant exists, but no prefix) | **deny** |

## Install

Install on any Claude Code surface that runs plugin `PreToolUse` hooks — the
CLI, the IDE extensions, or **Claude Code for Claude Desktop**.

**Claude Code (CLI or IDE extension)** — run the slash commands:

```
/plugin marketplace add karlkfi/claude-prod-guard
/plugin install prod-guard@prod-guard
```

**Claude Code for Claude Desktop** — use the **Customize** tab:

1. Open the **Customize** tab and go to its plugins / marketplaces section.
2. Add `karlkfi/claude-prod-guard` as a marketplace.
3. Find **prod-guard** in that marketplace, install it, and enable it.

After installing with either method:

- Requires `python3` on your PATH.
- Restart Claude Code (or `/reload-plugins`) so the hook is registered.
- **Turn on auto-update now.** A GitHub marketplace pins the version you
  installed and never refreshes on its own (see
  [Keeping it updated](#keeping-it-updated)) — so a stale classifier silently
  misses later false-negative fixes. Add this to `~/.claude/settings.json` at
  install time, while you're thinking about it, so the guard keeps itself
  current:
  ```json
  {
    "extraKnownMarketplaces": {
      "prod-guard": {
        "source": { "source": "git", "url": "https://github.com/karlkfi/claude-prod-guard.git" },
        "autoUpdate": true
      }
    }
  }
  ```
- **Add your real production identifiers to the config** — the built-in
  patterns catch names containing `prod`/`production`/`prd`/`live`, but your
  GCP project ids, cluster names, and subscriptions deserve explicit
  patterns. See [Configuration](#configuration).

To verify, ask Claude to run `kubectl --context fake-prod delete ns test` —
it should be blocked with a prod-guard reason. A `kubectl --context kind-...
get pods` should run without any prod-guard output.

## Keeping it updated

Claude Code auto-updates **official Anthropic marketplaces only**. prod-guard
installs from a third-party GitHub marketplace, and those **never refresh on
their own** — the version you installed stays pinned until you either enable
auto-update or update it by hand. For a guard plugin that pin has teeth: a
stale classifier is missing later false-negative fixes, not just conveniences.
(The author's own machine ran 1.1.0 while the repo shipped 2.x — a full major
version behind, missing the shell-variable-expansion classifier and more.)

**Recommended — set and forget.** Add `autoUpdate` for the marketplace in
`~/.claude/settings.json` and Claude Code refreshes it like an official one:

```json
{
  "extraKnownMarketplaces": {
    "prod-guard": {
      "source": { "source": "git", "url": "https://github.com/karlkfi/claude-prod-guard.git" },
      "autoUpdate": true
    }
  }
}
```

**Manual.** Update the marketplace clone, then the installed plugin, then
restart to apply:

```
claude plugin marketplace update prod-guard
claude plugin update prod-guard@prod-guard
```

These `claude` CLI commands work headlessly and share Claude Desktop's plugin
state, so they update a Desktop install too (Desktop has no `/plugin` slash
command). On the CLI or an IDE extension you can equivalently run
`/plugin marketplace update prod-guard` then `/plugin install prod-guard@prod-guard`.
Compare the installed version against the
[latest release](https://github.com/karlkfi/claude-prod-guard/releases) after
updating.

## Covered tools

For each tool the guard knows the **explicit target flag**, the **ambient
state** consulted when the flag is absent, and which verbs mutate. Verbs the
tables have never heard of are treated as **mutating** — when unsure, the
guard errs toward friction (a prompt, or a deny when the target isn't pinned),
because a missed destructive form is the failure mode that matters.

| Tool | Explicit target | Ambient fallback |
| --- | --- | --- |
| `kubectl`, `oc`, `flux` | `--context` | `current-context` in `$KUBECONFIG` / `~/.kube/config`; `oc login` / `oc project` prompt as kubeconfig writers. A context resolves to its cluster's `server:` URL in the kubeconfig, which is classified alongside the context name — so a prod cluster reached through an innocuously named context is still denied |
| `helm` | `--kube-context` | same kubeconfig `current-context` |
| `gcloud` | `--project` / `--zone` / `--region` / `--account`, `CLOUDSDK_CORE_PROJECT` | project of the active gcloud configuration |
| `aws` | `--profile` / `--region`, `AWS_PROFILE`. An unknown-named profile is resolved to its `[profile NAME]` account (`sso_start_url` / `role_arn` / `sso_session`) in `~/.aws/config`, so a prod account behind an innocuous name still denies | the `[default]` profile's `sso_start_url` / `role_arn` / `sso_session` in `~/.aws/config` (or `$AWS_CONFIG_FILE`) — a prod account denies; an unpinned mutation denies with a pin-`--profile` fix-it either way, naming what resolved |
| `eksctl` | `--profile` / `--region`, `AWS_PROFILE` / `AWS_DEFAULT_PROFILE` | the `[default]` profile in `~/.aws/config`, resolved exactly as for `aws` — a prod account denies; an unpinned mutation denies (pin `--profile`) |
| `az` | `--subscription` | default subscription in `~/.azure/azureProfile.json` |
| `terraform` / `tofu` | `TF_WORKSPACE` | `.terraform/environment` in the working dir; the initialized backend's state location (S3/GCS bucket, Terraform Cloud org/workspace) from `.terraform/terraform.tfstate` is classified too, so a prod state location denies even behind an innocuous workspace name; `apply`/`destroy` are always treated as mutating |
| `docker`, `podman`, `nerdctl`, `docker-compose` | `--context` / `--connection`, `DOCKER_HOST` / `CONTAINER_HOST` | `currentContext` in `~/.docker/config.json`; a local-daemon context defers. `docker push` classifies the image ref's registry; `build --push` classifies **every** `-t` tag; `compose push` fails closed (the registry lives in the compose file, which the hook doesn't parse) |
| `gh` | `-R`/`--repo`, `GH_REPO` | the cwd repo's `origin` remote (denylist-only — see [Limitations](#limitations)). Mutating verbs are tiered: collaboration metadata (`issue`/`pr` create/edit/comment/review/close/reopen/ready, label ops, `gist create`) always defers — reversible, no path to an outage — while the strict tier (`pr merge`, `repo delete`/`edit`, `release`, `secret`/`variable set`, `workflow`/`run`, `api` writes) denies on a prod repo. Set `"gh_strict": true` to fold the collaboration tier back into strict |
| `ssh` | destination host (`user@host`, `-J` jump host) | n/a — denylist-only: a prod destination is denied, everything else defers (see [Limitations](#limitations)) |
| `argocd` | `--server` | `current-context` in `~/.config/argocd/config` |
| `doctl` | `--context` | the doctl auth context (never read — an unpinned mutation denies, pin `--context`) |
| `pulumi` | `--stack` / `-s` | the per-project selected stack, read from `~/.pulumi/workspaces/` (a prod selection denies; an unpinned mutation denies either way, pin `--stack`); `pulumi stack select` prompts (denies for a prod stack) |
| `ansible`, `ansible-playbook` | `-i` / `--inventory`, `--limit`, and (ad-hoc) the host pattern | `ANSIBLE_INVENTORY`, then `[defaults] inventory` in `ansible.cfg`. The inventory is the target; `--limit`/pattern can only *escalate* to a prod deny. `--syntax-check`/`--list-hosts`/`--list-tasks`/`--list-tags` and ad-hoc `-m ping`/`setup`/`debug` are read-only |
| `kubectx` / `kubens` | n/a | switching contexts *is* the shared-state mutation: `kubectx <ctx>` (and `-`) is denied outright — pin `kubectl --context <ctx>` per command instead; `kubectx -d` and `kubens` still prompt |
| `kustomize` | n/a | local-only tool; always defers (the `kubectl apply` it pipes into is guarded separately) |

Ambient resolution is **pure local string work** — the guard parses the flag
or reads the tool's local config file. It never runs the tool, never makes a
network call, never touches a cluster.

Compound commands are split and each segment evaluated: `&&`, `||`, `|`,
`;`, `&`, newlines, subshells, `$(...)`, backticks, plus `bash|sh|zsh -c
'...'` bodies, `eval`, and wrappers (`sudo`, `env`, `timeout`, `xargs`,
`nohup`, `time`). A guard that only inspected the first token would be
trivially bypassed by `echo hi && kubectl --context prod delete ns x`.

Simple shell variables in a resolved target are expanded before
classification, so the common `CTX=<ctx> kubectl --context $CTX …` /
`P=…; CTX=gke_${P}_… kubectl --context $CTX …` pattern classifies by the
context's *value*, not the literal `$CTX`. Expansion resolves inline
(`A=x cmd`) and `export`/bare (`P=x; …`) assignments left-to-right, following
shell scope (a bare, unexported var does not expand inside a nested
`bash -c` body). It is deliberately conservative: an **undefined** variable is
left literal (so the target stays unknown and still prompts, never silently
allowed), and non-trivial `$` forms — command substitution `$(…)`, arithmetic,
and every `${…}` operator (`${V:-default}`, `${#V}`, …) — are not expanded, so
they too fall back to a prompt rather than a guessed value.

## Configuration

Targets are classified by two regex lists (Python syntax, case-insensitive,
searched — not anchored):

- **prod** patterns — a match means PROD. Built-ins: word-bounded
  `prod`/`production`/`prd`/`live`.
- **nonprod** patterns — a match means NON-PROD. Built-ins: `kind-`/`k3d-`
  prefixes, `minikube`, `docker-desktop`, `colima`, `rancher-desktop`,
  `orbstack`, `localhost`, `127.0.0.1`, and word-bounded
  `dev`/`test`/`qa`/`e2e`/`ci`/`sandbox`/`stage`/`staging`/`stg`/`local`/`demo`/`lab`/`preview`/`scratch`.

Patterns are checked in **precedence order** — config beats the built-ins, and
within each tier prod beats nonprod:

1. **config prod** → PROD (an explicit denylist entry wins outright)
2. **config nonprod** → NON-PROD (a vetted allowlist entry can clear a built-in
   prod heuristic — this is the escape hatch for false positives like a repo
   slug that merely contains `prod`, e.g. `karlkfi/claude-prod-guard`)
3. **built-in prod** → PROD (the word-bounded heuristic)
4. **built-in nonprod** → NON-PROD

So a name matching *both* built-in lists (`prod-staging-mirror`) still classifies
PROD — but a name you add under config `nonprod` classifies NON-PROD even when
a built-in prod pattern also matches it. A config `prod` entry always wins, so
clearing a heuristic this way can never mask a target you have explicitly vetted
as production.

**No match means UNKNOWN, and unknown + mutating prompts.** The default is
fail-closed: the guard never silently allows a mutation to a target it can't
classify. Classify your environments instead of approving the same prompt
repeatedly.

Patterns are merged from all of these (config is additive to the built-in
*patterns* — it can add regexes but never delete one — and takes precedence
over them per the order above; narrowing the boundary is always an explicit,
reviewable config line, never a silent default):

| Source | Purpose |
| --- | --- |
| `~/.claude/prod-guard.json` | user-level patterns (your orgs' naming) |
| `<project>/.claude/prod-guard.json` | project-level patterns — **versioned and reviewable**, commit it |
| `PROD_GUARD_CONFIG=<path>` | an extra config file |
| `PROD_GUARD_PROD_PATTERNS` / `PROD_GUARD_NONPROD_PATTERNS` | inline patterns, `;`- or newline-separated |

Config file schema:

```json
{
  "prod": ["^gke_acme-platform-prod_", "^acme-(platform|data)-prod", "argocd\\.acme\\.io"],
  "nonprod": ["^gke_acme-platform-dev_", "^acme-sandbox-"],
  "gh_strict": false
}
```

`gh_strict` (boolean, default `false`) opts out of `gh`'s collaboration-metadata
tier: with it on, every mutating `gh` verb — including `issue`/`pr`
create/comment/edit — prod-classifies again, for orgs that wire issue or PR
comments to deployments. It can also be set with `PROD_GUARD_GH_STRICT=1`.
Like the pattern lists it is strengthening-only: any source can turn it on and
none can turn it off (loosening the boundary is a code change, not config drift).

A malformed config file loses only itself — the built-in patterns still
apply, so a typo never removes the boundary (fail-open on infrastructure,
fail-closed on the decision).

`PROD_GUARD_DISABLE=1` in the session environment disables the hook entirely
(for CI runs that manage their own credentials scoping, for example).

## The override escape hatch

An intentional production operation should be one deliberate step, not
impossible. Prefixing the command with `PROD_GUARD_OVERRIDE=<reason>`
downgrades a **deny** to an **ask**, so a human still confirms it:

```
PROD_GUARD_OVERRIDE=incident-4711-approved kubectl --context prod-us delete pod stuck-pod
```

The deny message names this escape hatch, so the agent can propose the
override — with its reason on the record in the command itself — and the
human approves or rejects the prompt. The override never turns a deny into a
silent allow, and it has no effect on plain asks.

It must be an inline prefix on the command itself. Exporting
`PROD_GUARD_OVERRIDE` into the session environment does nothing — a deny stays
a deny — so it cannot be armed once and left set for every command after it.

### Session-scoped override

A sanctioned *batch* of production commands (an incident, a planned node-pool
rebuild) shouldn't re-confirm the same justification N times.
`PROD_GUARD_SESSION_OVERRIDE=<reason>` asks exactly like `PROD_GUARD_OVERRIDE`
on first use — but once you approve that prompt and the command runs, the
explicit production target(s) it named are **granted for the rest of the
session**: further commands *prefixed the same way* against those targets
defer silently (your normal permission settings still apply — the guard never
emits `allow`).

```
PROD_GUARD_SESSION_OVERRIDE=pool-rebuild-approved kubectl --context prod-us delete node old-1   # ask once
PROD_GUARD_SESSION_OVERRIDE=pool-rebuild-approved kubectl --context prod-us cordon old-2        # defers
```

The scope is deliberately narrow:

- **Per session, per exact target, 8-hour cap.** Another session, another
  target, or an expired grant asks again.
- **The prefix is required on every command** — the reason stays on the
  record in the command line, and an unprefixed prod mutation still denies.
- **Only explicit targets are grantable.** Ambient-context denies,
  shared-state switch denies, and unknown-target asks re-prompt every time.
- **A grant can only be minted by a human approval.** The recording hook
  fires only if the command actually ran, and in `bypassPermissions` mode
  the first use is a hard deny — unattended sessions can't self-grant.

Grants live in `~/.claude/prod-guard/session-grants/<session-id>.json`
(local only, see [PRIVACY.md](PRIVACY.md)); delete a file to revoke its
session's grants early.

## Agent guidance: avoiding prompts

Paste this into your project's `CLAUDE.md` (or `AGENTS.md`) so the agent
avoids the prompts instead of triggering them:

```markdown
## Avoiding prod-guard permission prompts

This machine uses prod-guard, a hook that blocks mutating infrastructure
commands aimed at production, and denies a mutating command that doesn't pin
its target (relying on clobber-prone ambient context) with a fix-it naming the
flag to add. To keep work flowing:

- **Pin the target on every mutating command**: `kubectl --context <ctx>`,
  `helm --kube-context <ctx>`, `gcloud --project <id>`,
  `aws --profile <name>`, `az --subscription <id>`,
  `TF_WORKSPACE=<ws> terraform ...`, `docker --context <ctx>`,
  `pulumi --stack <name>`, `ansible-playbook -i <inventory>`. A mutating
  command that relies on the current context/config is **denied** — the deny
  names the flag to add, so just re-run with it pinned. (A parallel session
  can repoint that shared state between writing the command and running it,
  which is why an explicit target is required.)
- **Never switch shared context** (`kubectl config use-context`, `kubectx`,
  `gcloud config set project`, `az account set`, `docker context use`) —
  these are **denied** outright: they repoint every parallel session. Pin
  per command instead.
- **Unknown targets prompt.** If a legitimate non-prod target keeps
  prompting, add it to `.claude/prod-guard.json` under `"nonprod"` instead
  of approving repeatedly.
- **Production mutations are blocked.** If one is genuinely intended, ask
  the user; with their sign-off, prefix the command with
  `PROD_GUARD_OVERRIDE=<reason>` and they will confirm via the prompt. For a
  sanctioned *batch* of commands against the same target, use
  `PROD_GUARD_SESSION_OVERRIDE=<reason>` on every command instead: the user
  confirms once and the rest of the batch flows without re-prompting.
```

## Measuring friction

To see which prod-guard prompts actually cost you time — and which unknown
targets prompt over and over — run the friction report:

```
/prod-guard:friction-report              # last 7 days
/prod-guard:friction-report --since 24h
/prod-guard:friction-report --json       # machine-readable
```

It is a **read-only** analyzer: it re-reads the decisions Claude Code already
recorded in your local session transcripts and adds no telemetry (see
[PRIVACY.md](PRIVACY.md)). The report ranks prompts by category (`deny-prod`,
`deny-ambient`, `deny-switch`, `ask-unknown`, `ask-switch`), by tool, and — most usefully — by
**unclassified target**: the non-prod names that keep landing in `ask-unknown`
are exactly the ones to add under `"nonprod"` in `.claude/prod-guard.json` (see
[Configuration](#configuration)). Vet each before adding — never reclassify a
real production target as non-prod just to silence a prompt.

The report also warns when your installed prod-guard lags the version in the
local marketplace clone (`prod-guard 1.1.0 installed, 2.4.1 available`) — some
of the friction it just ranked may already be fixed upstream. See
[Keeping it updated](#keeping-it-updated). The comparison is local-only, so no
warning means "no lag against the clone you have", not "up to date": refresh the
clone with `claude plugin marketplace update prod-guard` first if it has been a
while.

You can also run the script directly:

```
python3 scripts/friction-report.py --since 30d --repo gateway --top 20
```

## Limitations

- **False negatives are possible; treat the guard as a net, not a wall.**
  Only the listed tools are covered — a mutation through an uncovered CLI
  (a vendor CLI, `pulumi env`, `ansible-pull`) or through a
  script/Makefile that the command merely names (`make deploy`,
  `./scripts/release.sh`) is invisible to the hook. Wrapped commands that
  resolve their own targets are usually the *safe* path — the guard exists
  for the ad-hoc commands.
- **Classification is by name — plus, for kube-contexts, the cluster's
  server URL.** A production context charmingly named `blue-cluster-2` is
  caught if its kubeconfig `server:` URL matches a prod pattern (e.g.
  `https://api.prod-us.example.com`); if the URL is also unremarkable it stays
  UNKNOWN (prompted, not denied) until you add a pattern. The fail-closed
  default means unknown never silently passes, but the hard block needs your
  patterns to know what "prod" means in your org. Server resolution covers
  `kubectl`/`oc`/`flux`/`helm` and context switches (`kubectx`,
  `kubectl config use-context`); other tools classify by name only.
- `gh` and `ssh` are denylist-only: their target is pinned on the command
  line (gh's repo remote, ssh's destination host), not clobber-prone shared
  state, and prompting on every `gh pr create` or `ssh dev-box` would be pure
  noise. Each is only blocked when the resolved target matches a prod pattern
  — an unknown host or repo defers rather than prompting. Any `ssh` into a
  prod host is treated as mutating: an interactive prod shell is the blast
  radius, and a read-only remote command can't be distinguished from a
  destructive one.
- `gh`'s mutating verbs are **tiered** (see the table above): collaboration
  metadata — `gh issue`/`gh pr` create/edit/comment/review/close/reopen/ready,
  label operations, `gh gist create` — never prod-classifies, because it's
  reversible and has no path to an outage, so a deny would buy no safety while
  costing a prompt. Only the strict tier (`pr merge`, `repo delete`/`edit`,
  `release`, `secret`/`variable` writes, `workflow`/`run`, `api` POST/PUT/
  PATCH/DELETE, and anything not recognized as collaboration) denies on a prod
  repo. Orgs that wire issue/PR comments to deployments can opt the whole
  collaboration tier back into strict with `"gh_strict": true` in
  `.claude/prod-guard.json` (or `PROD_GUARD_GH_STRICT=1`).
- The doctl auth context is not read from disk; a mutating command relying on
  it is denied (pin `--context`) rather than resolving the ambient value.
- pulumi's selected stack is read from `~/.pulumi/workspaces/` (or `$PULUMI_HOME`)
  to deny an ambient prod selection. Recent pulumi can relocate that directory in
  "agent mode" when `~/.pulumi` isn't writable and no `PULUMI_HOME` is set; the
  guard doesn't follow that relocation, so the file simply isn't found and an
  unpinned mutation is denied with a pin-`--stack` fix-it (fail-open on the
  read still lands on the unpinned deny) rather than resolving the selection.
  `pulumi stack select <prod>` is still denied regardless.
- Ambient state is read at *hook* time; a race remains between the hook's
  check and the command's execution. Pinning the target with a flag — which
  the unpinned-mutation deny steers toward — is the real fix; the deny exists
  to force that choice before the command can run.
- Command parsing is intentionally conservative: an operator inside a quoted
  string can split a segment and produce a spurious prompt (never a missed
  one). Heredoc bodies are the one exception, and only when the delimiter is
  **quoted** (`<<'EOF'`, `<<"EOF"`, `<<\EOF`): the shell expands nothing and
  runs nothing from such a body, so it is skipped — which is what lets a
  commit message or doc edit quote `helm upgrade` without being blocked. The
  skip needs the closing delimiter to actually appear; an unterminated
  heredoc, and any unquoted `<<EOF` (which does expand), keep their bodies
  under inspection.
- Variable expansion covers simple `$VAR`/`${VAR}` references only. A target
  assembled with a `${VAR:-default}` operator, command substitution
  (`--context $(kubectl config current-context)`), or an indirection is left
  unexpanded and classified as its literal text — which errs toward a prompt,
  except in the corner case where that literal happens to contain a
  prod/nonprod word. A variable exported into the session before Claude Code
  started is visible to the hook and expands as the shell would.
- In full-auto `bypassPermissions` mode there is no one to answer an ask, so
  asks are emitted as denies — equally blocking, but the agent gets the
  reason and can re-route instead of stalling.

## Companion plugins

prod-guard watches the **infrastructure blast-radius** boundary. Two sibling
plugins guard different axes with the same secure-by-default design:

- [**workspace-guard**](https://github.com/karlkfi/claude-workspace-guard) —
  the **filesystem** boundary: prompts before guarded file commands
  (`grep`/`sed`/`cat`/`cp`/`rm`/…) read or write paths outside the project
  root.
- [**branch-guard**](https://github.com/karlkfi/claude-branch-guard) — the
  **git history** boundary: auto-approves safe git on feature branches,
  pauses commits/pushes to `main` and destructive git.

All three run side by side; each defers to normal permissions outside its
own axis.

```
/plugin marketplace add karlkfi/claude-workspace-guard
/plugin install workspace-guard@workspace-guard
/plugin marketplace add karlkfi/claude-branch-guard
/plugin install branch-guard@claude-branch-guard
```

## Design

For the rationale behind the approach (why deny for prod when the siblings
default to ask, why fail-closed on unknown targets, why local file reads
instead of asking the tools, what alternatives were rejected), see
[`docs/design.md`](docs/design.md).

## Privacy

The hook runs entirely on your machine and has no network access, telemetry,
or analytics. It reads the pending Bash command and, when needed, single
values from local CLI config files (kubeconfig `current-context`, gcloud
active config, docker `currentContext`, azure default subscription, the
`origin` remote URL), decides in memory, and writes nothing to disk. See
[`PRIVACY.md`](PRIVACY.md) for the full policy.

## Contributing

Bugs, ideas, and questions go in
[GitHub Issues](https://github.com/karlkfi/claude-prod-guard/issues).
For the development backlog, see [`docs/STATUS.md`](docs/STATUS.md).

## License

MIT — see [LICENSE](LICENSE).
