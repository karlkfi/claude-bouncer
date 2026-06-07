# branch-guard

A Claude Code plugin that lets routine git work flow freely on feature branches
while keeping a human in the loop for anything that touches a protected branch
or is destructive — cutting the approval prompts you'd otherwise click through,
especially in `acceptEdits` and non-interactive modes.

It installs a single `PreToolUse` hook that classifies each `git`/`gh` command
and **auto-approves the safe ones**:

- **read-only git** — `status`, `diff`, `log`, `show`, `branch` (list), `fetch`,
  `rev-parse`, `remote -v`, `stash list`, `config --get`, … (on any branch),
- **safe mutations** — `add`, `restore --staged`, `switch -c` / `checkout -b`,
  `worktree add`, branch/tag creation (on any branch),
- **`git commit`**, and a `git push` of the worktree's own branch — including
  force pushes — when the branch is *not* protected,
- **read-only `gh`** — `gh pr view`/`list`/`status`, `gh repo view`, …

It **prompts you (`ask`)** before anything that needs a human:

- a `git commit`, file edit (`Edit`/`Write`/`MultiEdit`), or `git push` to a
  **protected branch** (`main`/`master`) — or, under the default `strict` push
  policy, a push to *any* branch other than the worktree's own,
- destructive commands on any branch — `reset --hard`, `clean -f`, `branch -D`,
  `restore` (worktree), `worktree remove`, `stash drop`, `config --global`,
  `filter-branch`, `gc`,
- a `git merge`/`rebase`/`cherry-pick`/`pull` that targets a protected branch.

Everything else is left untouched — the hook stays silent and the normal Claude
Code permission flow applies.

A command is auto-approved only when **every** segment in it is a
recognized-safe `git`/`gh` invocation, so a non-git command can't ride along
into an approval (`git status && rm -rf foo` defers; it isn't allowed).

In a **non-interactive permission mode** (`auto`, `dontAsk`, `bypassPermissions`)
there is no human present to answer a prompt, so any decision that would be an
`ask` is emitted as a **`deny`** instead — the guard fails safe rather than
letting the action through unconfirmed.

Commands are tokenized with Python's `shlex` (not substring matching), so the
hook recognises subcommands through env-var prefixes
(`GIT_AUTHOR_NAME=x git commit`), global flags (`git -C path commit`), combined
short flags (`git clean -fd`), and command chains (`git add -A && git commit`).
An inline-config escape hatch (`git -c core.pager='!sh …' log`) blocks
auto-approval (the command defers) but never weakens a protective `ask`.

> **Scope:** branch-guard reasons about git/branch *semantics*. The filesystem
> boundary — commands reading or writing files outside the workspace — is the
> companion **workspace-guard** plugin's job; the two are complementary and
> don't overlap.

## Why

In a worktree-based workflow you want zero friction committing to throwaway
`claude/*` branches, but a deliberate pause before you commit to — or edit files
checked out on — `main`/`master`. branch-guard encodes exactly that policy so
you don't have to approve every routine feature-branch commit by hand, yet can't
silently mutate a protected branch.

The edit check resolves the branch of **the file's own repository**
(`git -C <dir-of-file>`), not the session's working directory. That deliberately
catches edits made through a checkout sitting on `main` (for example a parent
repo path) even while your session's cwd is a feature-branch worktree.

## Behavior

For a **Bash** command, each segment is classified and the command-level
decision is: any segment needs an `ask` → **ask**; else every segment is
recognized-safe → **allow**; else → defer.

| `git` / `gh` command (Bash) | Current branch | Decision |
|---|---|---|
| read-only git (`status`, `diff`, `log`, `branch`, `fetch`, `rev-parse`, …) | any | **allow** (auto) |
| safe mutations (`add`, `restore --staged`, `switch -c`, `checkout -b`, `worktree add`, branch/tag create) | any | **allow** (auto) |
| read-only gh (`gh pr view`/`list`/`status`, `gh repo view`, …) | any | **allow** (auto) |
| `git commit`; `git merge`/`rebase`/`cherry-pick`/`stash` | non-protected (e.g. `claude/x`) | **allow** (auto) |
| `git commit`; `git merge`/`rebase`/`cherry-pick`/`stash` | `main` / `master` | **ask** |
| `git push` of the worktree's own branch, incl. force | non-protected, `strict` policy | **allow** (auto) |
| `git push` elsewhere — see [Push guard](#push-guard) | any | **ask** / defer (per policy) |
| destructive (`reset --hard`, `clean -f`, `branch -D`, `restore`, `worktree remove`, `stash drop`, `config --global`, `filter-branch`, `gc`) | any | **ask** |
| `git pull` without `--ff-only` | any | **ask** |
| anything mixing in a non-git/gh command, or an unrecognized subcommand | any | no decision (defer) |
| `Edit` / `Write` / `MultiEdit` | file's repo on `main` / `master` | **ask** |
| `Edit` / `Write` / `MultiEdit` | file's repo on non-protected branch | no decision (defer) |
| any other tool | any | no decision (defer) |

"No decision" means the hook emits nothing and Claude Code applies its usual
permission rules. In a non-interactive permission mode every **ask** above
becomes a **deny** (see [the note at the top](#branch-guard)).

A command is auto-approved (**allow**) only when **every** segment in it is a
recognized-safe `git`/`gh` invocation. A chain that mixes in a non-git command
(e.g. `git commit && rm -rf foo`, or `git push && rm -rf foo`) is *not*
auto-approved — it defers to the normal permission prompt — so a trailing
command can't ride along into a silent approval. When in doubt (an unknown
subcommand, an inline-config escape hatch, an ambiguous `git checkout <name>`)
the hook **defers** rather than guessing.

## Push guard

`git push` is evaluated according to the `BRANCH_GUARD_PUSH_POLICY` environment
variable:

| Policy | Behavior |
|---|---|
| `strict` *(default)* | **allow** (auto) a push of the worktree's own current branch, including a force push of it. **ask** before any other push — a *different* branch (`git push origin other`), foreign refspecs (`git push origin HEAD:other`), wildcards, `--all`/`--mirror`, or a protected target. |
| `protected` | **ask** before a push whose target is `main`/`master` (including `git push origin main`, `git push origin HEAD:main`, deleting `main`, and `--all`/`--mirror`). Any other push defers. Never auto-approves. |
| `off` | Pushes are not guarded at all. |

A bare `git push` / `git push origin` pushes the current branch to its
same-named upstream: under `strict` it is auto-approved (it's the worktree
branch); under `protected` it defers.

Set it in `~/.claude/settings.json` (or a project's `.claude/settings.json`):

```json
{ "env": { "BRANCH_GUARD_PUSH_POLICY": "protected" } }
```

The push guard is **best-effort**: it parses the Bash command Claude runs and so
only governs Claude's `Bash` tool, and unusual refspecs may not be classified
(in which case it asks under `strict` / defers under `protected`, never silently
allowing). For a hard guarantee that no push reaches a protected branch —
regardless of how it's invoked or from which machine — pair it with a git
`pre-push` hook and/or server-side branch protection.

### Known limitations

- The guard only governs Claude's `Bash`/`Edit`/`Write`/`MultiEdit` tools. It
  does **not** intercept file mutations done through other Bash commands —
  e.g. `sed -i`, `>` redirects, or `rm` — on a protected branch.
- It auto-approves a *safe* set of `git`/`gh` subcommands and asks on a
  *destructive* set; anything outside both (an unknown subcommand, `git config`
  reads/writes it can't classify, most `gh` mutations) **defers** to the normal
  permission flow rather than guessing. Auto-approval is a convenience layer, not
  a security boundary — for hard guarantees use a git `pre-push` hook and/or
  server-side branch protection.

## Requirements

- `python3` (standard library only) and `git` on `PATH`.

## Test

```bash
./test/run.sh
```

Spins up a throwaway git repo under `tmp/` and asserts the decision for each
tool/branch combination. The test harness additionally needs `jq` on `PATH` to
read the hook's JSON output.

## Activation

The plugin self-hosts as a single-plugin marketplace, so you can install it
straight from this directory.

### Interactive

```text
/plugin marketplace add ~/workspace/claude-branch-guard
/plugin install branch-guard@claude-branch-guard
```

(CLI equivalents: `claude plugin marketplace add ~/workspace/claude-branch-guard`
then `claude plugin install branch-guard@claude-branch-guard`.)

### Via settings.json

Add to `~/.claude/settings.json` (all projects) or a project's
`.claude/settings.json` (this project only):

```json
{
  "extraKnownMarketplaces": {
    "claude-branch-guard": {
      "source": { "source": "directory", "path": "~/workspace/claude-branch-guard" }
    }
  },
  "enabledPlugins": {
    "branch-guard@claude-branch-guard": true
  }
}
```

To point at a remote instead of a local path, replace the `source` with a
`github`/`git`/`url` form (see the Claude Code plugin-marketplace docs).
