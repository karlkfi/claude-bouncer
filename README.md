# branch-guard

A Claude Code plugin that lets commits flow freely on feature branches while
keeping a human in the loop for anything that touches a protected branch.

It installs a single `PreToolUse` hook that:

- **auto-approves `git commit`** when the current branch is *not* protected
  (e.g. `claude/*` or any feature branch), and
- **prompts you (`ask`)** before a `git commit` **or** a file edit
  (`Edit`/`Write`/`MultiEdit`) that targets a **protected branch** (`main` or
  `master`).

Everything else is left untouched — the hook stays silent and the normal Claude
Code permission flow applies.

The Bash command is tokenized with Python's `shlex` (not a substring match), so
the hook recognises `git commit` through env-var prefixes
(`GIT_AUTHOR_NAME=x git commit`), global flags (`git -C path -c k=v commit`),
and command chains (`git add -A && git commit -m x`), while `git log --grep=commit`
and similar non-commit invocations are correctly ignored.

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

| Tool call | Current branch | Decision |
|---|---|---|
| `git commit …`, or an all-git chain like `git add -A && git commit …` (Bash) | non-protected (e.g. `claude/x`) | **allow** (auto) |
| any command containing a `git commit` (Bash) | `main` / `master` | **ask** |
| `git commit … && <non-git command>` (Bash) | non-protected | no decision (defer) |
| any other Bash command | any | no decision (defer) |
| `Edit` / `Write` / `MultiEdit` | file's repo on `main` / `master` | **ask** |
| `Edit` / `Write` / `MultiEdit` | file's repo on non-protected branch | no decision (defer) |
| any other tool | any | no decision (defer) |

"No decision" means the hook emits nothing and Claude Code applies its usual
permission rules.

A `git commit` is auto-approved on a feature branch only when **every** command
in the chain is itself a `git` invocation. A chain that mixes in a non-git
command (e.g. `git commit -m x && rm -rf foo`) is *not* auto-approved — it falls
back to the normal permission prompt — so a trailing command can't ride along
into a silent approval.

### Known limitation

The hook guards `git commit` (via Bash) and edits made through the
`Edit`/`Write`/`MultiEdit` tools. It does **not** intercept file mutations done
through other Bash commands — e.g. `sed -i`, `>` redirects, or `rm` — on a
protected branch.

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
