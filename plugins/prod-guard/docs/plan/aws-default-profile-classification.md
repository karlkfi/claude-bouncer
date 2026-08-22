# Plan: classify the AWS default profile for ambient targeting (Q7)

## Goal

A mutating `aws` command with no `--profile`/`AWS_PROFILE` runs against the
`[default]` profile. Today the guard can't see where that profile points, so it
always `ask`s. The `[default]` profile in `~/.aws/config` records where it
reaches — an SSO start URL, an assumed `role_arn`, an SSO account/session — and
those classify the target. Reading them lets an ambient-prod default profile
`deny` and an ambient-nonprod one keep asking with a message that names what it
resolved.

## Non-goals

- **Named `--profile NAME` content resolution.** An explicit `--profile admin`
  whose name classifies `unknown` still `ask`s by name; we do not read
  `[profile admin]` to escalate it. Same INI machinery, but out of the Queue
  item's scope — filed as a follow-up. (When added it must be *additive*:
  profile content escalates `unknown → deny`, never `unknown → defer`.)
- **`eksctl`'s ambient AWS profile.** `eval_eksctl` shares the AWS
  profile/region fallback but keeps its combined `ask_ambient`; resolving it is
  a parallel follow-up.
- **Reading `~/.aws/credentials`.** The classifiable fields (`sso_start_url`,
  `role_arn`, …) live in `~/.aws/config`; the credentials file holds only the
  secret access keys, which must never be classified into a message. We read
  only `~/.aws/config` (or `$AWS_CONFIG_FILE`).
- **Downgrading any prompt.** Profile resolution can only *escalate* the ambient
  decision to `deny`; a nonprod/unknown/unresolvable default profile still
  `ask`s (see invariant).

## Security invariant

Purely additive on the ambient (no-profile) path, which already `ask`s today:

- default profile classifies PROD → **deny** (the new catch).
- default profile classifies NONPROD or UNKNOWN → **ask** (unchanged verdict;
  the message now names the resolved identifier).
- no `~/.aws/config`, no `[default]` section, unreadable/malformed file → resolve
  nothing → identical to today's generic `ask_ambient` (fail OPEN on the read).
- The explicit-profile path (`--profile` / `AWS_PROFILE` / `AWS_DEFAULT_PROFILE`)
  is untouched: prod name → deny, nonprod name → defer, unknown name → ask.
- Credentials are never echoed: only well-known safe identifiers
  (`sso_start_url`, `role_arn`, `sso_session`, …) are echoable; any other key's
  value (e.g. a `credential_process` command) is classify-only and, if it
  matches prod, denies with a generic "the default aws profile" message.

## Design

### Resolver — `aws_default_profile(seg_env) -> (named, extra) | None`

Locate the config file: `AWS_CONFIG_FILE` (seg env → process env) else
`~/.aws/config`. Minimal INI parse (`[section]` headers, `key = value` lines,
`#`/`;` full-line comments skipped). Take the `[default]` section, and if it has
`sso_session = NAME`, merge in the `[sso-session NAME]` section.

- `named` — echoable identifiers, in a fixed preference order so `named[0]` is
  the most meaningful present field: `sso_start_url`, `role_arn`, `sso_session`,
  `sso_account_id`, `sso_role_name`, `source_profile`, `sso_region`, `region`.
- `extra` — every other key's value (classify-only, never echoed).

`None` when the file or the `[default]` section is missing/unreadable.

### Integration — `eval_aws` ambient tail

Only the final branch (no explicit profile resolved) changes:

1. resolve `aws_default_profile(seg_env)`.
2. any `named` value classifies PROD → **deny**, echoing that value + `(ambient)`.
3. any `extra` value classifies PROD → **deny**, generic desc + `(ambient)`.
4. else if a `named` identifier exists → `ask_ambient` naming it.
5. else (no config / nothing resolved) → today's generic `ask_ambient`.

## Tests (fixture `$HOME`, `~/.aws/config`; extend `make_home(aws_config=...)`)

- default profile `sso_start_url` prod, `aws ec2 terminate-instances` (no
  profile) → **deny**.
- default profile `role_arn` `.../role/prod-admin` → **deny**.
- default profile `sso_session = acme-prod` (session name prod) → **deny**.
- default profile references `[sso-session acme]` whose `sso_start_url` is prod
  → **deny** (reference followed).
- `credential_process` (extra) containing a prod string → **deny**, message says
  "the default aws profile" with no raw value echoed.
- default profile `sso_start_url` dev → **ask**, reason names the resolved value
  (nonprod still asks).
- default profile all-unknown (`bluefin`) → **ask**, reason names it.
- no `~/.aws/config` → **ask** (unchanged generic fallback;
  `test_s3_rm_no_profile_asks` regression).
- `$AWS_CONFIG_FILE` override honored.
- explicit `--profile prod` still **deny**; `AWS_PROFILE=dev` still defers
  (explicit path regression).
- malformed config → generic **ask** (fail-open regression).
- unit test for `aws_default_profile` (parse, sso-session follow, prod scan).

## Docs

- README covered-tools table: split the combined `aws, eksctl` row; `aws` now
  reads the `[default]` profile's `sso_start_url`/`role_arn`/… from
  `~/.aws/config`; `eksctl` still never-read.
- README decision table: add an `aws ec2 terminate-instances` (default profile →
  prod SSO account) → deny row.
- README Limitations: drop AWS from the "not read from disk" bullet.
- design.md: add a line under the classification rationale; fix the pulumi
  sentence that cites "the AWS default profile" as an example of not-reading.
- STATUS.md: delete the Q7 row; add follow-up rows for named-`--profile` content
  and eksctl profile resolution (isolated commit).
