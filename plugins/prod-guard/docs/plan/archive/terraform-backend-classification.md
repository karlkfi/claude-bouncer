# Plan: classify the terraform backend state location (Q3)

## Goal

A terraform workspace named innocuously — `default`, `main`, `blue` — is a weak
proxy for what a mutating `apply`/`destroy` actually touches. The real target is
the **backend state location**: an S3/GCS bucket, or a Terraform Cloud (TFC)
organization/workspace. `terraform init` records that location in
`.terraform/terraform.tfstate`. Classifying it alongside the workspace name
catches a prod state location regardless of the selected workspace's name.

## Non-goals

- **JSON-completeness of every backend type.** We extract the well-known
  state-location fields for the common backends (`s3`, `gcs`, `azurerm`,
  `remote`, `cloud`) and fall back to classifying every string leaf for any
  other/future type. Local backend (`type: local`) has no remote target and is
  ignored.
- **`-chdir=DIR` resolution.** The existing `.terraform/environment` lookup
  already resolves relative to `ctx.cwd`, not the `-chdir` directory; backend
  resolution stays consistent with it. Honoring `-chdir` for both is a possible
  follow-up (Queue item), out of scope here.
- **Reading HCL `backend {}` blocks from `.tf` source.** We read the
  post-`init` JSON that names the *resolved* backend; un-`init`ed dirs resolve
  nothing → unchanged behavior.
- **Downgrading any prompt.** Backend classification can only *escalate* a
  decision to `deny`; it never silences an existing `ask` (see invariant).

## Security invariant

The change is **purely additive**: a prod backend can only move a decision
toward `deny`, never toward silence.

- If the workspace already classifies PROD → `deny` (unchanged; backend not
  consulted).
- If the backend classifies PROD, upgrade the decision to `deny`. This is the
  new catch — it fires on the explicit-nonprod path (`TF_WORKSPACE=dev` today
  defers silently; now denies if the backend bucket is prod), the
  explicit-unknown path, and the ambient path.
- A backend that classifies NONPROD or UNKNOWN changes nothing — no existing
  `ask` is downgraded to silence, no `deny` is relaxed.
- Any parse/read failure, a missing/`local`/un-`init`ed backend → resolves to
  None → behavior identical to today (fail OPEN on the infra read).
- Credentials are never classified into a user-visible message: known backend
  types echo only the state-location value (bucket/key/org/workspace); the
  unknown-type catch-all classifies every string but reports only the backend
  *type*, never a raw value.

## Design

### Resolver — `terraform_backend(cwd) -> (type, named, extra) | None`

Read `<cwd>/.terraform/terraform.tfstate` (JSON). Return None on missing file,
bad JSON, non-dict, no `backend` object, or `type: local`.

- `named` — echoable state-location strings for known types:
  - `s3`: `bucket`, `key`
  - `gcs`: `bucket`, `prefix`
  - `azurerm`: `storage_account_name`, `container_name`, `key`
  - `oss`/`cos`: `bucket`, `prefix`
  - `remote`/`cloud`: `organization`, `workspaces.name`, `workspaces.prefix`,
    each of `workspaces.tags`
- `extra` — for any other backend type, every string leaf in `config`
  (classify-only, never echoed). `named` is empty in this case.

### Prod check — `terraform_backend_prod(cwd) -> str | None`

Returns a deny description if any `named` (echoing the matched location) or any
`extra` (type only) string classifies PROD, else None. Only the prod verdict is
needed — the invariant means nonprod/unknown never affect policy.

### Integration — `eval_terraform`

Insert `terraform_backend_prod(cwd)` before each non-deny return on the mutating
tail, after the workspace-prod check:

1. explicit `TF_WORKSPACE` prod → deny (unchanged)
2. explicit nonprod / unknown → **backend-prod → deny**, else current behavior
3. ambient workspace prod → deny (unchanged)
4. ambient otherwise → **backend-prod → deny**, else current `ask_ambient`

## Tests (fixture `$HOME`, `.terraform/terraform.tfstate` under cwd)

- s3 backend, prod bucket, `TF_WORKSPACE=dev apply` → **deny** (the weak-proxy
  catch; today defers).
- s3 backend, prod bucket, ambient `apply` (no workspace) → **deny**.
- gcs backend, prod bucket → **deny**.
- `remote`/`cloud` backend, prod TFC workspace name → **deny**.
- Backend key path `env/prod/…` with nonprod bucket → **deny** (key classified).
- Nonprod backend + `TF_WORKSPACE=dev` → defers (no false escalation).
- Unknown-type backend with a prod string → **deny**, message names the type
  only (no raw value).
- `type: local` / missing file / malformed JSON → unchanged name-only behavior
  (regression guard).
- Prod workspace + nonprod backend → still deny (workspace wins, unchanged).

## Docs

- README Covered-tools row for terraform: note backend-location classification.
- README decision table: add a `TF_WORKSPACE=dev terraform apply` (prod S3
  backend) → deny row.
- design.md: one line under the classification rationale.
- STATUS.md: delete the Q3 row (isolated commit).
