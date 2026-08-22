# Plan: cover `pulumi` and `ansible`/`ansible-playbook` (Q4)

## Goal

Extend the guard to two more infrastructure CLIs, each with its own target
model:

- **pulumi** — the target is the **stack** (`dev`, `myorg/prod`,
  `myorg/proj/prod`). Explicitly pinned by `--stack`/`-s`; otherwise the
  currently-selected stack (ambient, per-project workspace state).
- **ansible** / **ansible-playbook** — the target is the **inventory** (which
  hosts a play runs against). Pinned by `-i`/`--inventory` and narrowed by
  `-l`/`--limit` (and, for ad-hoc `ansible`, the host-pattern positional);
  otherwise the ambient inventory (`ANSIBLE_INVENTORY` / `ansible.cfg`).

Both slot into the existing `EVALUATORS` model: a new evaluator each, a
read-only allowlist, and the shared `policy`/`deny_prod`/`ask_*` helpers. No
parser changes.

## Non-goals

- **Resolving pulumi's ambient selected stack from disk.** Pulumi records the
  per-project selection in `~/.pulumi/workspaces/<proj>-<sha1(path)>-workspace.json`,
  keyed by a hash of the `Pulumi.yaml` path — fragile to reproduce in stdlib
  and error-prone. v1 classifies the explicit `--stack`/`-s` and **prompts**
  when no stack is pinned (the AWS-default-profile precedent: never read →
  always ask). `pulumi stack select prod` is still denied, so the moment prod
  is chosen is caught. Reading the ambient stack is deferred to a new Queue
  item (parallel to Q7 for the AWS default profile).
- **Pulumi ESC environments (`pulumi env ...`).** Different target model (an
  environment name, not a stack); left uncovered (falls through to the ambient
  stack prompt). Note as future work if it proves noisy.
- **Extra ansible binaries** (`ansible-pull`, `ansible-vault`,
  `ansible-inventory`, `ansible-galaxy`, `ansible-config`, `ansible-doc`).
  Only `ansible` and `ansible-playbook` — the two that run plays against hosts
  — are registered; the read-only/local siblings defer (they aren't in
  `EVALUATORS`, so they're uncovered, not misclassified).
- **`--check` / `-C` as a read-only exception for playbooks.** Ansible check
  mode can be overridden per task (`check_mode: no`), so a `--check` run can
  still mutate. Treated as mutating (unsure ⇒ mutating). `--syntax-check`,
  `--list-hosts`, `--list-tasks`, `--list-tags` — which never connect — *are*
  read-only fast-paths.

## Security invariants

- **Fail closed on the decision.** Mutating verb + unknown/unresolved target →
  `ask`; + prod target → `deny`. Neither tool ever emits `allow`.
- **Inventory is authoritative for ansible; pattern/limit can only escalate.**
  A `-l`/pattern that classifies PROD upgrades to `deny`; an *unknown*
  pattern/limit never forces an `ask` when the inventory itself resolves
  nonprod (so `ansible webservers -i inventories/dev/hosts ...` defers instead
  of prompting on the unknown word `webservers`).
- **Read-only defers even against prod** (consistent with the whole design):
  `pulumi preview --stack prod` and `ansible prod -m ping` defer.
- Fail OPEN on infra reads: an unreadable `ansible.cfg` / missing env resolves
  to None → the ambient prompt, never a crash.

## Design

### `eval_pulumi(argv, seg_env, ctx)`

`verb = words[0]` (via `words_of` with pulumi value-flags stripped).

- Read-only / local verbs → defer:
  `preview, about, version, whoami, logs, console, plugin, convert, install,
  schema, help, completion`.
- `login` / `logout` → `ask_switch` (shared credentials/backend).
- `stack <sub>`:
  - `ls`/`output`/`export`/`history`/`graph`/bare → defer.
  - `select`/`unselect` → shared-state switch: name (positional or `--stack`)
    prod → `deny`; else `ask_switch`.
  - other subs (`rm`/`init`/`rename`/`tag`/`import`/`change-secrets-provider`)
    → mutating; classify the positional stack name or `--stack` via the tail.
- `config <sub>`: `get`/bare → defer; else mutating (target = the stack).
- Everything else (`up`/`update`, `destroy`, `refresh`, `import`, `cancel`,
  `watch`, `state ...`, unknown) → mutating.

Mutating tail — classify `first_flag_value(--stack, -s)` (or the explicit stack
name for `stack rm/init/rename`): prod → `deny`, nonprod → defer, unknown →
`ask_unknown`; **no stack pinned → `ask_ambient`** (pin `--stack`).

### `eval_ansible(argv, seg_env, ctx)` (shared by `ansible` + `ansible-playbook`)

1. `--syntax-check`/`--list-hosts`/`--list-tasks`/`--list-tags` present → defer.
2. Ad-hoc only: `-m`/`--module-name` in `{ping, setup, debug, gather_facts}`
   (bare or `ansible.builtin.`-prefixed) → defer.
3. Collect `invs = all -i/--inventory values`, `limits = all -l/--limit
   values`, and (ad-hoc) `pattern = first positional`. `escalators = limits +
   [pattern]`.
4. Any of `invs + escalators` classifies PROD → `deny` (name it).
5. Else if `invs` non-empty: any UNKNOWN inv → `ask_unknown`; else (all
   nonprod) defer.
6. Else (inventory ambient): resolve `ANSIBLE_INVENTORY` env, then
   `ANSIBLE_CONFIG`/`./ansible.cfg` `[defaults] inventory =`. prod → `deny`
   (ambient); nonprod → defer; unknown/unresolved → `ask_ambient` (pin `-i`).

Ambient reader `ansible_ambient_inventory(seg_env, cwd)` + a tiny
`[defaults] inventory =` INI scanner — a local file read, same shape as the
other ambient resolvers.

### Registration

`EVALUATORS`: `'pulumi': eval_pulumi`, `'ansible': eval_ansible`,
`'ansible-playbook': eval_ansible`. `COVERED_TOOLS` derives automatically.

## Tests (add to `tests/test_prod_guard.py`)

**Pulumi:** `up --stack prod` → deny; `up --stack dev` → defer; `preview
--stack prod` → defer; `up` (no stack) → ask; `destroy -s prod` → deny;
`stack select prod` → deny; `stack select dev` → ask; `stack rm prod` → deny;
`config set foo bar --stack prod` → deny; `config get foo --stack prod` →
defer; `whoami`/`version` → defer; `login` → ask; RO/mutating sweep.

**Ansible:** `ansible-playbook -i inventories/prod site.yml` → deny;
`-i inventories/dev/hosts` → defer; `-i inventories/ site.yml` (unknown) → ask;
`site.yml --limit prod-web` → deny; `ansible prod -m shell -a reboot` → deny;
`ansible prod -m ping` → defer; `ansible webservers -i inventories/dev/hosts -m
service ...` → defer (unknown pattern, nonprod inv); ambient via `ansible.cfg`
prod → deny / dev → defer / none → ask; `ANSIBLE_INVENTORY=…prod` → deny;
`--syntax-check` / `--list-hosts` → defer.

## Docs

- README **Covered tools** table: two rows (pulumi → `--stack` / selected
  stack; ansible → `-i`/`--limit` / `ANSIBLE_INVENTORY`+`ansible.cfg`).
- README **decision table**: a `pulumi up --stack prod` → deny and an
  `ansible-playbook -i inventories/prod` → deny row.
- README **Limitations**: drop `pulumi`/`ansible` from the "uncovered CLI"
  example; note pulumi's ambient stack is not read (like the AWS default
  profile).
- README **agent-guidance** snippet: add `pulumi --stack <name>` and
  `ansible -i <inventory>` to the pin list.
- `.claude-plugin/plugin.json`: add `pulumi`, `ansible` keywords.
- design.md: one line noting the two new target models.
- STATUS.md: delete the Q4 row; add a Queue item for reading pulumi's ambient
  selected stack (isolated commit).
