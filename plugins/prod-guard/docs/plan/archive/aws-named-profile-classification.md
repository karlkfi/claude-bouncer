# Plan: classify a named AWS profile's account for explicit targeting (Q9)

## Goal

An explicit `aws ... --profile NAME` (or `AWS_PROFILE=NAME` / `AWS_DEFAULT_PROFILE=NAME`)
whose name classifies `unknown` still `ask`s today — the guard only classifies
the profile *name*, not where it points. The `[profile NAME]` section in
`~/.aws/config` records the account it reaches (an `sso_start_url`, a `role_arn`,
an `sso_session`, an SSO account/session), exactly like the `[default]` section
Q7 already resolves. Reading it lets an explicit prod-account profile with an
innocuous name (`admin`, `ops`, `blue`) `deny` instead of `ask`.

This is the named-profile follow-up the Q7 plan
([aws-default-profile-classification.md](aws-default-profile-classification.md))
filed as a non-goal.

## Non-goals

- **Downgrading any decision.** Named-profile content resolution can only
  *escalate* an `unknown` name to `deny`. A profile name that already classifies
  prod (deny) or nonprod (defer) is untouched — the name is authoritative there.
- **Reading `~/.aws/credentials`.** Same as Q7: classifiable fields live in
  `~/.aws/config`; the credentials file holds only secret keys and is never read.
- **Overriding an explicit nonprod name from a shared field.** A profile the user
  named `dev` classifies nonprod and defers, even if it references an org-wide
  `sso_start_url` that happens to contain `prod`. The explicit name is a stronger,
  user-supplied signal than a possibly-org-wide SSO portal URL; unlike the
  ambient `[default]` case there is a name to trust. Content resolution is scoped
  to the `unknown` name only (see invariant).
- **Named profiles in `eksctl`.** `eval_eksctl` classifies an explicit
  `--profile`/`AWS_PROFILE` by name via its combined locator scan; extending
  content resolution there is a separate concern if it ever surfaces.

## Security invariant

Purely additive on the explicit-profile path, scoped to the `unknown` verdict:

- profile name classifies PROD → **deny** (unchanged).
- profile name classifies NONPROD → **defer** (unchanged).
- profile name UNKNOWN, region resolves the class → unchanged (existing region
  fallback runs first).
- profile name UNKNOWN, region did not decide → resolve `[profile NAME]`:
  - any resolved field classifies PROD → **deny** (the new catch).
  - else → **ask** (unchanged verdict; today's `ask_unknown` by name).
- no `~/.aws/config`, no `[profile NAME]` section, unreadable/malformed → resolve
  nothing → identical to today's `ask_unknown` (fail OPEN on the read).
- Credentials are never echoed: only the well-known safe identifiers are
  echoable; any other key's value (e.g. `credential_process`) is classify-only
  and, if prod, denies with a generic `aws profile 'NAME'` message.

## Design

### Resolver — generalize `aws_default_profile` to `aws_profile_fields`

`aws_default_profile(seg_env)` already parses `~/.aws/config` and returns
`(named, extra)` for the `[default]` section, following an `sso_session`
reference. Lift the section name out:

- `aws_profile_fields(name, seg_env) -> (named, extra) | None` — same body, but
  the section is `default` when `name == 'default'` else `profile <name>` (the
  AWS config convention: named profiles are `[profile NAME]`, only default is
  bare `[default]`).
- `aws_default_profile(seg_env)` becomes a thin alias
  `aws_profile_fields('default', seg_env)` so Q7's callers/tests are untouched.

### Escalator — `aws_named_profile_prod(name, seg_env) -> str | None`

Mirror `terraform_backend_prod` / the default-profile scan: resolve the fields,
return a deny description if any `named` field classifies prod (echoing that
field), else a generic `aws profile 'NAME'` if any `extra` field is prod, else
`None`.

### Integration — `eval_aws` explicit-profile branch

The explicit branch computes `cls` from the name (then region). After the
prod/nonprod returns, `cls` is `unknown`; insert one step before the final
`ask_unknown`: `aws_named_profile_prod(profile, seg_env)` → **deny** if it
returns a description, else fall through to today's `ask_unknown`.

## Tests (fixture `$HOME`, `~/.aws/config`; `make_home(aws_config=...)`)

- `[profile admin]` with prod `sso_start_url`, `aws ... --profile admin` → **deny**,
  reason names the resolved value.
- `[profile ops]` with `role_arn .../role/prod-admin` → **deny**.
- `AWS_PROFILE=admin` (env, not flag) with prod `[profile admin]` → **deny**.
- `[profile admin]` `credential_process` prod string (extra) → **deny**, value
  not echoed.
- `[profile admin]` follows `sso_session` into a prod `[sso-session NAME]` → **deny**.
- `[profile dev]` prod `sso_start_url` but nonprod *name* → **defer** (name wins;
  scoped-to-unknown regression).
- `--profile admin` with no `[profile admin]` section → **ask** (fail-open
  regression, unchanged).
- explicit `--profile prod` still **deny**; `--profile dev` still **defer**
  (name-only path regression).
- unit test for `aws_profile_fields('admin', ...)` (section name, sso follow).

## Docs

- README covered-tools `aws` row: note the named `[profile NAME]` is resolved the
  same way as `[default]`.
- README decision table: add a named-profile → prod-account deny row.
- design.md: extend the AWS profile line to cover the named-profile path.
- STATUS.md: delete the Q9 row (isolated commit).
