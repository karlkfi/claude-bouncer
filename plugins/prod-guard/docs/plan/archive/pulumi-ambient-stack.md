# Plan: resolve pulumi's ambient selected stack (Q8)

## Goal

Read pulumi's per-project **selected stack** from disk so a mutating `pulumi`
command with no `--stack`/`-s` can **deny** when the selection is prod — instead
of always prompting. Additive over today's behavior: nonprod/unknown/unresolved
still `ask` (the ambient prompt), only a resolved prod selection escalates to
`deny`. This is the follow-up deferred as a non-goal in
[`pulumi-ansible-coverage.md`](pulumi-ansible-coverage.md), and it mirrors the
AWS-default-profile precedent ([`aws-default-profile-classification.md`](aws-default-profile-classification.md)):
never read → always ask became read → prod denies, else ask.

## How pulumi stores the selection (verified against source)

`pkg/workspace/workspace.go` (`settingsPath`, `readSettings`) and
`sdk/go/common/workspace/paths.go`:

- The selected stack lives in a per-project **workspace settings file**:
  `<pulumi-home>/workspaces/<name>-<sha1hex(projectpath)>-workspace.json`,
  whose JSON body is `{"stack": "<selected>"}` (the `Settings` struct; `stack`
  is `omitempty`, so the key is absent when nothing is selected).
- **`<pulumi-home>`** = `$PULUMI_HOME` if set, else `~/.pulumi`
  (`GetPulumiHomeDir`; the env package names the var `PULUMI_HOME`).
- **`<name>`** = the project's `name:` field, read from the project file
  (`LoadProject`).
- **`<projectpath>`** = the **absolute, cleaned path to the project file** that
  `DetectProjectPathFrom` finds by walking **up** from cwd. At each directory it
  tries the markup extensions **in `encoding.Exts` order — `.json`, `.yaml`,
  `.yml`** — and stops at the first `Pulumi.<ext>` that exists (content isn't
  parsed during detection). The hash is `sha1` of that path string, hex-encoded.
- Path construction is `filepath.Abs(cwd)` then `filepath.Join` — **no symlink
  resolution**, so Python's `os.path.abspath` + `os.path.join` reproduce the
  exact bytes that get hashed.

### Known limitation (documented, not blocking)

Recent pulumi has an *agent-mode* relocation (`pulumiHomeDirForPath` →
`getAgentPulumiDir`) that moves the home dir when `~/.pulumi` isn't writable and
no explicit `PULUMI_HOME` is set. The guard resolves only the standard
`$PULUMI_HOME`/`~/.pulumi/workspaces` path; if pulumi relocated, the file simply
isn't found → resolve nothing → the existing ambient `ask`. Fail-open and
secure (never a false defer), so it's an acceptable gap.

## Security invariants

- **Additive-only.** Only a resolved **prod** selection changes the outcome
  (ask → deny). A nonprod/unknown/unresolved selection keeps today's
  `ask_ambient` — the selection is clobber-prone shared state (a parallel
  session in the same project can `pulumi stack select prod`), so even a
  nonprod-looking selection still asks. This matches gcloud/az/terraform-workspace
  ambient handling, **not** docker's defer-on-nonprod.
- **Fail OPEN on the read.** Missing project file, unreadable name, missing/
  malformed workspace JSON, absent `stack` key → `None` → the ambient `ask`.
  No crash, never a false defer.
- **Explicit `--stack`/`-s` path is unchanged** (prod deny / nonprod defer /
  unknown ask). `pulumi stack select <prod>` stays a deny.

## Design

New stdlib helpers in `scripts/bash-prod-guard.py` (add `import hashlib`):

- `_pulumi_home_dir(seg_env)` → `$PULUMI_HOME` (seg_env then process env), else
  `~/.pulumi`, else `None`.
- `_pulumi_find_project(cwd)` → `(projectpath, name)` for the nearest
  `Pulumi.{json,yaml,yml}` at/above `os.path.abspath(cwd)`, extensions tried in
  `.json`→`.yaml`→`.yml` order, stopping at the first directory that has one.
  Reads `name` (JSON: `data["name"]`; YAML: a top-level `^name:` scalar, quotes/
  comment stripped). `None` if no project file or no readable name.
- `pulumi_selected_stack(seg_env, cwd)` → the `stack` string from
  `<home>/workspaces/<name>-<sha1hex(projectpath)>-workspace.json`, or `None`.

Refactor `_pulumi_decide(action, explicit, seg_env, ctx)`: unchanged explicit
branch; the no-stack branch now calls `pulumi_selected_stack` and denies on a
prod selection, else `ask_ambient` naming the resolved stack (or "unresolved at
hook time"). Both `eval_pulumi` call sites pass `seg_env, ctx`.

## Tests (add to `PulumiTests` / a new e2e class in `tests/test_prod_guard.py`)

A fixture helper writes `Pulumi.yaml` (`name: <proj>`) in `cwd`, computes
`sha1(os.path.abspath(cwd)+"/Pulumi.yaml")`, and drops the workspace file under
`$HOME/.pulumi/workspaces/`. Cases:

- selected stack `acme/prod` + `pulumi up` (no `--stack`) → **deny** (ambient).
- selected stack `dev` + `pulumi up` → **ask** (ambient, additive: nonprod still asks).
- no workspace file / no `stack` key → **ask** (unresolved).
- `$PULUMI_HOME` override honored (workspace under the override dir) → deny.
- `Pulumi.json` project (name via JSON) selected prod → deny.
- explicit `--stack dev` beats a prod selection → defer (explicit wins).
- malformed workspace JSON → **ask** (fail open, no crash).
- selected prod but read-only verb (`pulumi preview`) → defer.

## Docs

- README **decision table**: change/annotate the `pulumi up` (no stack) row —
  add a "selected stack is prod → deny" row.
- README **Covered tools** pulumi row: it now reads the selected stack (prod
  denies), not "never read — always prompts".
- README **Limitations**: drop pulumi from the "not read from disk" bullet
  (leave doctl); note the agent-mode-home gap.
- `docs/design.md`: update the pulumi sentence ("ambient selected stack is not
  read" → now read; prod selection denies).
- STATUS.md: delete the Q8 row (isolated commit).
