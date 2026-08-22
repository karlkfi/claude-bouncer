# Plan: classify kube contexts by cluster server URL (Q2)

## Goal

A production kube-context named innocuously — `blue-2`, `cluster-7` — evades the
name-based prod patterns and is classified UNKNOWN (prompted, not denied). The
kubeconfig already maps that context to a cluster to a `server:` URL like
`https://api.prod.example.com`. Matching the existing patterns against the
**server URL** too catches the prod cluster regardless of what the context is
named.

## Non-goals

- YAML-completeness. kubeconfig is YAML; we parse the block-style subset that
  `kubectl config` writes with a line-oriented reader. Flow-style or exotic
  layouts fail to resolve a server and fall back to name-only classification.
- Classifying an explicit `--server <url>` / `--cluster <name>` flag. Possible
  follow-up (a Queue item), out of scope here.
- Resolving contexts that live only in a KUBECONFIG file we don't read. We read
  every `:`-separated KUBECONFIG path (or `~/.kube/config`); anything outside
  that set stays unresolved → name-only.

## Security invariant

The change is **purely additive**: it can only move a target toward *more*
prod-leaning, never less.

- `classify_kube(name)` = max(classify(name), classify(server)) on the
  prod > nonprod > unknown lattice.
- A name that classifies PROD today still classifies PROD (server never
  consulted once name is prod — prod wins).
- A server that classifies PROD upgrades an unknown/nonprod **name** to PROD
  (deny). This is the new catch.
- A server that classifies NONPROD downgrades an **unknown** name to nonprod
  only on the *explicit-pin* path (`--context blue-2`), where nonprod defers.
  On the *ambient* path nonprod still asks (threat model 2 — clobbering — is
  unchanged), so no ambient prompt is silently dropped.
- Any parse/read failure → server is None → behavior identical to today.

## Design

### Parser

`_parse_kubeconfig(path) -> (ctx_to_cluster, cluster_to_server)` — line
oriented:

- Track the current top-level section (`clusters:` / `contexts:` at column 0).
- Within a section, split into list items on lines whose first non-space char
  is `-`.
- Per item, extract inline `key: value` fields with
  `^\s*(?:- )?<key>:\s*(.+)$` (quotes stripped). Within a single well-formed
  list item each field name (`name`, `server`, `cluster`) appears once, so
  section-tracking is the only disambiguation needed (`cluster:` is a nested
  field in a context item but the block key in a cluster item; `name:` appears
  in both sections).
- clusters item → `cluster_to_server[name] = server`
- contexts item → `ctx_to_cluster[name] = cluster`

`_kubeconfig_paths(seg_env)` — `$KUBECONFIG` split on `:` (all paths), else
`~/.kube/config`. Merge maps across files first-wins (kubectl merge order).

`kube_context_server(name, seg_env)` — resolve `name → cluster → server`
across the merged maps; None if unresolved.

### Classification hook

`classify_kube(name, seg_env)` returns the max class. `policy()` grows an
optional `classify_fn=classify` parameter; the kubeconfig evaluators pass a
closure binding `seg_env`. The switch verbs that classify a context name
inline (`kubectl config use-context`, `kubectx <name>`) call `classify_kube`
directly so a prod-by-server context denies on switch too.

Touched evaluators: `eval_kubectl` (target + `config use-context`),
`eval_helm`, `eval_flux`, `eval_kubectx`.

## Tests

- Fixture kubeconfig: context `blue-2` → cluster whose server is
  `https://api.prod.example.com` → mutating `--context blue-2` denies;
  read-only defers.
- Ambient current-context `blue-2` with prod server → mutating denies.
- `kubectx blue-2` (prod server) denies.
- Nonprod server downgrades unknown explicit context to defer.
- Unresolvable server (name not in kubeconfig, flow-style, missing file) →
  name-only behavior unchanged (regression guard).
- Multi-path KUBECONFIG where the cluster lives in the second file resolves.

## Docs

- README "Classification is by name" caveat → note server-URL resolution.
- README Covered-tools row for kubectl/oc/flux.
- README decision table: add a `--context blue-2` (prod server) → deny row.
- design.md: short note under the classification rationale.
- STATUS.md: delete the Q2 row (isolated commit).
